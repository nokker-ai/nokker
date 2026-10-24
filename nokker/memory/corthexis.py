"""`MemoryPort` : rappel CortHeXis par tour (PLAN-P1 §5.2, chantier A-2).

Deux implémentations du même contrat (`MemoryPort` + `ToolSource`, `core/ports.py`), choisies
PAR CONFIG (`build()` en fin de fichier), jamais basculées au runtime :

  (i)  `CorthexisMemory` — MCP `sokkan-memory` existant (instance interne), en
       lecture seule (S1-S2, INCHANGÉ : c'est le service de prod, `infra/sokkan/memory/` de
       le dépôt interne — on ne le réécrit pas, on s'y connecte comme le canari,
       `infra/nokker-canary/config.yaml`). C'est le défaut de `nokker.api.server.build_kernel`.
  (ii) `DirectCorthexisMemory` — paquet `corthexis` 2.1 EN DIRECT (bibliothèque Python en
       process, PAS un second serveur MCP) sur le Postgres + l'embed CPU du compose
       (`deploy/compose/`), reranker optionnel (S2-S3, ce chantier).

Les deux appliquent la MÊME politique de seuil, reprise À L'OCTET du hook de prod
(`infra/sokkan/memory/hook_prompt_recall.py` du dépôt interne, recalée le 03.10.2026 sur 150
questions du banc : 53 % -> 87 % d'injections justes — note mémoire `sokkan-v3-moteur-memoire`,
§« Seuil du hook de prod recalé ») :

    score >= 0.40
    OU (rerank n'est pas None ET rerank >= 0.5 ET score >= 0.25)
    OU le NOM de la note est cité dans le message (nom complet, ou >= 2 segments de 4+ lettres
       du nom comme mots entiers du message — `named()`, port de `hook_prompt_recall._nomme`)

`propose()` (écriture) reste un no-op journalisé pour les DEUX modes : la quarantaine réelle
(revue humaine avant toute écriture en mémoire de prod) est le chantier A-3, pas A-2.

`DirectCorthexisMemory` importe `corthexis` PARESSEUSEMENT (dans les méthodes, pas au chargement
du module) : un déploiement qui ne configure que le mode (i) n'a pas besoin du paquet `corthexis`
ni de ses dépendances (psycopg, pgvector, numpy) installées. Côté install : `corthexis` est tiré
avec `--no-deps` (même pattern que `hermes-agent`, voir `pyproject.toml`) — son `pyproject.toml`
amont épingle `mcp>=1.12,<2`, incompatible avec le `mcp==2.0.0` de ce dépôt, mais aucun des
modules utilisés ici (`corthexis.service`, `.store`, `.embed`, `.search`) n'importe `mcp` (seuls
`corthexis.server`/`corthexis.cli mcp`, non utilisés en mode direct, le font).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from nokker.core.ports import Recall, ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.tools.mcp import _SyncMcp, text_of

logger = logging.getLogger("nokker.memory.corthexis")

DEFAULT_URL = ""  # aucune instance par défaut : `memory_mcp_url` de la config la nomme

# ───────────────────── seuils du hook de prod (partagés par les deux modes) ─────────────────────
# Port EXACT de `infra/sokkan/memory/hook_prompt_recall.py` (dépôt interne) : SEUIL / SEUIL_RERANK /
# RERANK_OK, et `_nomme()`. Ne pas re-calibrer ici : tout changement de modèle d'embedding se
# recale SUR LE BANC (règle de la note `sokkan-v3-moteur-memoire`), côté hook de prod, et se
# reporte ici en changeant ces trois constantes — jamais à l'œil.
RECALL_SCORE_THRESHOLD = 0.40   # = SEUIL
RECALL_SEUIL_RERANK = 0.25      # = SEUIL_RERANK
RECALL_RERANK_OK = 0.5          # = RERANK_OK
_MIN_SEG_LEN = 4

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    """Port de `memory_search_server._tokens` : mots alphanumériques en minuscules."""
    return {w for w in _WORD_RE.findall((text or "").lower())}


def named(note_name: str, message: str) -> bool:
    """Port de `hook_prompt_recall._nomme` : nom complet cité (slug, "_"="-"), ou >= 2 segments
    de 4+ lettres du nom présents comme mots entiers (4+ lettres) du message. Un seul mot ne
    suffit plus depuis le 03.10.2026 (trop de rappels hors sujet — note `corthexis`)."""
    slug = (note_name or "").lower().replace("_", "-")
    if not slug:
        return False
    msg_norm = (message or "").lower().replace("_", "-")
    if slug in msg_norm:
        return True
    qtok = {t for t in _tokens(message) if len(t) >= _MIN_SEG_LEN}
    segs = {s for s in slug.split("-") if len(s) >= _MIN_SEG_LEN}
    return len(segs & qtok) >= 2


def kept(score: float, rerank: float | None, note_name: str, message: str, *,
         threshold: float = RECALL_SCORE_THRESHOLD,
         rerank_floor: float = RECALL_SEUIL_RERANK,
         rerank_ok: float = RECALL_RERANK_OK) -> bool:
    """Règle d'injection du hook de prod, les trois critères combinés (un seul suffit)."""
    if score >= threshold:
        return True
    if rerank is not None and rerank >= rerank_ok and score >= rerank_floor:
        return True
    return named(note_name, message)


def _tool_specs(source: str) -> list[ToolSpec]:
    return [
        ToolSpec(name="memory_search", description="Cherche dans la mémoire long terme (notes).",
                  parameters={"type": "object", "properties": {"query": {"type": "string"},
                              "top_k": {"type": "integer"}}, "required": ["query"]},
                  effect=Effect.READ, core=True, source=source),
        ToolSpec(name="memory_get", description="Lit le corps complet d'une note par son nom.",
                  parameters={"type": "object", "properties": {"note_name": {"type": "string"}},
                              "required": ["note_name"]},
                  effect=Effect.READ, core=True, source=source),
    ]


def _log_propose(prefix: str, thread_id: str, facts: list[str]) -> None:
    """Quarantaine réelle = A-3. v0/A-2 : journalise seulement, n'écrit JAMAIS en mémoire de prod."""
    if not facts:
        return
    logger.info("%s: proposition en quarantaine (noop, fil=%s, %d fait(s)) : %r",
                prefix, thread_id, len(facts), facts)


# ═══════════════════════════ (i) MCP sokkan-memory — INCHANGÉ ═══════════════════════════

@dataclass
class CorthexisMemory:
    """Mode (i) : client du MCP existant `sokkan-memory`. Sert DEUX rôles avec la même connexion
    MCP (pas deux clients) : `MemoryPort.recall()` (rappel automatique) et `ToolSource`
    (`memory_search`/`memory_get` appelables par le modèle). C'est le défaut de
    `nokker.api.server.build_kernel` — ne pas en changer la signature sans le mettre à jour."""
    url: str = DEFAULT_URL
    token: str = ""
    score_threshold: float = RECALL_SCORE_THRESHOLD
    timeout_s: float = 20.0
    _headers_extra: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        headers.update(self._headers_extra)
        self._io = _SyncMcp(self.url, headers, self.timeout_s)

    # ───────────────────────────── MemoryPort ─────────────────────────────

    def recall(self, query: str, k: int = 5) -> list[Recall]:
        if not query.strip():
            return []

        async def _search():
            session = await self._io.session_async()
            return await session.call_tool("memory_search", {"query": query, "top_k": k})

        try:
            result = self._io.run(_search())
        except Exception:
            logger.warning("corthexis(mcp): memory_search indisponible", exc_info=True)
            return []
        return _parse_mcp_recall(_rows_from_mcp_result(result), query, self.score_threshold)

    def propose(self, thread_id: str, facts: list[str]) -> None:
        _log_propose("corthexis(mcp)", thread_id, facts)

    # ───────────────────────────── ToolSource ─────────────────────────────
    # memory_search / memory_get comme outils cœur (appel explicite du modèle, en plus du rappel
    # automatique ci-dessus). Même connexion MCP, pas de second client.

    def specs(self) -> list[ToolSpec]:
        return _tool_specs("mcp:sokkan-memory")

    def call(self, name: str, arguments: dict[str, Any], _ctx: ToolContext) -> ToolResult:
        async def _call():
            session = await self._io.session_async()
            return await session.call_tool(name, arguments)

        try:
            result = self._io.run(_call())
        except Exception as e:
            logger.warning("corthexis(mcp): %s en échec", name, exc_info=True)
            return ToolResult(call_id="", name=name, content=f"erreur mémoire ({name}): {e}", is_error=True)
        return ToolResult(call_id="", name=name, content=text_of(result),
                           is_error=bool(getattr(result, "is_error", False)))

    def warm_up(self) -> None:
        """Ouvre la session MCP en arrière-plan dès le démarrage : le premier rappel ne paie plus la poignée de main
        (≈ 0,6 s de plus mesurés à froid) et ne se chevauche plus avec elle. Un échec se voit dans le journal, pas au
        démarrage : la mémoire indisponible ne bloque jamais le noyau."""
        def _go() -> None:
            try:
                self._io.session()
            except Exception:  # noqa: BLE001
                logger.warning("corthexis(mcp): préchauffage de la session en échec", exc_info=True)
        threading.Thread(target=_go, name="memory-warmup", daemon=True).start()

    def close(self) -> None:
        self._io.close()


def _rows_from_mcp_result(result: Any) -> list[dict]:
    """Lignes `memory_search` d'un `CallToolResult`, quel que soit la forme choisie par le serveur :

    - `result.structured_content` (sortie structurée MCP 2.0, `{"result": [...]}`) — la source la
      plus sûre quand le serveur la fournit ;
    - un SEUL bloc texte `{"result": [...]}` (format des fakes de test, et de la doc historique) ;
    - UN BLOC TEXTE PAR NOTE, chacun l'objet de la note lui-même (PAS enveloppé) — **le format
      RÉELLEMENT observé contre le `sokkan-memory` interne le 17.10.2026 (chantier A-2)** : avec ce
      format, `text_of()` (qui joint les blocs par `\\n`) produisait plusieurs objets JSON concaténés,
      donc invalides → `json.loads` échouait → **0 résultat sur 300 questions du banc A-2, MRR 0,000,
      alors que le service répond correctement** (bug réel trouvé en mesurant A1, pas dans un fake).
    Les trois formes sont tolérées en même temps : un bloc texte qui ne parse pas est ignoré (résultat
    partiel plutôt qu'un échec global), jamais une exception levée par cette fonction."""
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and isinstance(structured.get("result"), list):
        return [r for r in structured["result"] if isinstance(r, dict)]
    out: list[dict] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("result"), list):
            out.extend(r for r in data["result"] if isinstance(r, dict))
        elif isinstance(data, dict):
            out.append(data)
        elif isinstance(data, list):
            out.extend(r for r in data if isinstance(r, dict))
    return out


def _parse_mcp_recall(rows: list[dict], message: str, threshold: float) -> list[Recall]:
    """Applique la règle d'injection du hook de prod (`kept()`) aux lignes `memory_search` déjà
    extraites par `_rows_from_mcp_result` — au lieu du simple `score >= seuil` d'avant A-2 (un nom
    de note cité, ou un rerank fort à score faible, compte maintenant aussi)."""
    out: list[Recall] = []
    for item in rows:
        if "error" in item or "info" in item or "note_name" not in item and "path" not in item:
            continue
        score = float(item.get("score") or 0.0)
        note = item.get("note_name") or item.get("path") or item.get("note") or "?"
        rerank = item.get("rerank")
        rerank = float(rerank) if rerank is not None else None
        if not kept(score, rerank, str(note), message, threshold=threshold):
            continue
        # Description d'abord (ce qu'EST la note), puis l'extrait qui a matché : un extrait seul, pris au milieu d'une
        # note, a fait dire au modèle « Nokker est un moteur de recherche souverain » (vu en réel le 20.10.2026).
        desc = " ".join(str(item.get("description") or "").split())
        snippet = " ".join(str(item.get("snippet") or "").split())
        text = f"{desc} — extrait : {snippet}" if desc and snippet and snippet not in desc else (desc or snippet)
        out.append(Recall(note=str(note), text=text, score=score, rerank=rerank))
    return out


# ═══════════════════════════ (ii) paquet corthexis 2.1 en direct ═══════════════════════════

@dataclass
class DirectCorthexisMemory:
    """Mode (ii) : paquet `corthexis` 2.1 EN DIRECT — bibliothèque Python en process
    (`corthexis.service.search`/`.get`), PAS un second serveur MCP. Connexion au Postgres +
    à l'embed CPU du compose (`deploy/compose/` : services `postgres` + `embed`, trivial — mesure
    MRR/injections/RAM dans la note mémoire `nokker-p0`, § « A-2 »).

    Configuration : `corthexis` lit ses propres variables `CORTHEXIS_*` au premier appel
    (voir `corthexis.config.env`) — `__post_init__` les pose dans `os.environ` si elles sont
    données ici, sans jamais écraser une valeur déjà posée par l'opérateur (12-factor : le
    compose/l'environnement du process gagne toujours sur les valeurs par défaut du port).
    """
    database_url: str = ""          # CORTHEXIS_DATABASE_URL — "" = déjà dans l'environnement
    embed_urls: str = ""            # CORTHEXIS_EMBED_URLS (ex. "http://embed:8080")
    memory_profile: str = "leger"   # CORTHEXIS_MEMORY_PROFILE : leger (pas de reranker) | standard | gpu
    rerank_url: str = ""            # CORTHEXIS_RERANK_URL ; "off" désactive explicitement
    deep: bool = False              # force le reranker même en profil `standard` (async) — ignoré en `leger`
    projects: tuple[str, ...] | None = None   # scope 2.1 (None = pas de projets, mémoire autonome)
    score_threshold: float = RECALL_SCORE_THRESHOLD

    def __post_init__(self) -> None:
        for env_name, value in (
            ("CORTHEXIS_DATABASE_URL", self.database_url),
            ("CORTHEXIS_EMBED_URLS", self.embed_urls),
            ("CORTHEXIS_MEMORY_PROFILE", self.memory_profile),
            ("CORTHEXIS_RERANK_URL", self.rerank_url),
        ):
            if value:
                os.environ.setdefault(env_name, value)

    # ───────────────────────────── MemoryPort ─────────────────────────────

    def recall(self, query: str, k: int = 5) -> list[Recall]:
        if not query.strip():
            return []
        try:
            from corthexis import service as cx_service
            rows = cx_service.search(query, top_k=max(k, 10), deep=self.deep, projects=self.projects)
        except Exception:
            logger.warning("corthexis(direct): recherche indisponible", exc_info=True)
            return []
        out: list[Recall] = []
        for row in rows:
            if not isinstance(row, dict) or "error" in row or "info" in row:
                continue
            score = float(row.get("score") or 0.0)
            note = row.get("note_name") or "?"
            rerank = row.get("rerank")
            rerank = float(rerank) if rerank is not None else None
            if not kept(score, rerank, str(note), query, threshold=self.score_threshold):
                continue
            text = row.get("snippet") or row.get("description") or ""
            out.append(Recall(note=str(note), text=str(text), score=score, rerank=rerank))
            if len(out) >= k:
                break
        return out

    def propose(self, thread_id: str, facts: list[str]) -> None:
        _log_propose("corthexis(direct)", thread_id, facts)

    # ───────────────────────────── ToolSource ─────────────────────────────
    # Même bibliothèque que recall() ci-dessus, appel direct de `corthexis.service` (pas de
    # filtrage par seuil ici : un appel EXPLICITE du modèle voit tout, comme en mode MCP).

    def specs(self) -> list[ToolSpec]:
        return _tool_specs("corthexis:direct")

    def call(self, name: str, arguments: dict[str, Any], _ctx: ToolContext) -> ToolResult:
        try:
            from corthexis import service as cx_service
            if name == "memory_search":
                rows = cx_service.search(arguments.get("query", "") or "",
                                         int(arguments.get("top_k") or 8),
                                         deep=self.deep, projects=self.projects)
                return ToolResult(call_id="", name=name,
                                  content=json.dumps({"result": rows}, ensure_ascii=False, default=str),
                                  is_error=bool(rows and isinstance(rows[0], dict) and "error" in rows[0]))
            if name == "memory_get":
                note_name = arguments.get("note_name", "") or ""
                body = cx_service.get(note_name, projects=self.projects, via="nokker")
                if body is None:
                    return ToolResult(call_id="", name=name,
                                      content=f"note introuvable : {note_name}", is_error=True)
                return ToolResult(call_id="", name=name, content=body, is_error=False)
            return ToolResult(call_id="", name=name, content=f"outil mémoire inconnu : {name}", is_error=True)
        except Exception as e:
            logger.warning("corthexis(direct): %s en échec", name, exc_info=True)
            return ToolResult(call_id="", name=name, content=f"erreur mémoire ({name}): {e}", is_error=True)

    def close(self) -> None:
        try:
            from corthexis import service as cx_service
            cx_service.reset()
        except Exception:
            logger.debug("corthexis(direct): reset ignoré à la fermeture", exc_info=True)


# ═══════════════════════════ choix par config (i)/(ii) ═══════════════════════════

def build(mode: str | None = None, **kwargs: Any) -> CorthexisMemory | DirectCorthexisMemory:
    """Construit le `MemoryPort` actif. `mode` : "mcp" (i, défaut) ou "direct" (ii) ; si absent,
    lu dans `NOKKER_MEMORY_MODE` (défaut "mcp" — ne change jamais le comportement existant sans
    configuration explicite). `kwargs` passés au constructeur choisi (ex. `url=`/`token=` pour
    "mcp", `database_url=`/`embed_urls=`/`memory_profile=` pour "direct").

    Builder, pas branchement runtime : la session qui construit le noyau (aujourd'hui
    `nokker.api.server.build_kernel`, via son paramètre `memory=`) appelle ceci UNE fois au
    démarrage ; voir le compose (`deploy/compose/docker-compose.yml`, `NOKKER_MEMORY_MODE`)."""
    mode = (mode or os.environ.get("NOKKER_MEMORY_MODE") or "mcp").strip().lower()
    if mode == "mcp":
        return CorthexisMemory(**kwargs)
    if mode == "direct":
        return DirectCorthexisMemory(**kwargs)
    raise ValueError(f"NOKKER_MEMORY_MODE={mode!r} inconnu (attendu : mcp | direct)")


__all__ = [
    "DEFAULT_URL",
    "RECALL_RERANK_OK",
    "RECALL_SCORE_THRESHOLD",
    "RECALL_SEUIL_RERANK",
    "CorthexisMemory",
    "DirectCorthexisMemory",
    "build",
    "kept",
    "named",
]
