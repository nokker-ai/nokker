"""`MemoryPort` : rappel CortHeXis par tour (PLAN-P1 §5.2, chantier A-2).

Deux implémentations du même contrat (`MemoryPort` + `ToolSource`, `core/ports.py`), choisies
PAR CONFIG (`from_config()` en fin de fichier, appelée par `build_kernel`), jamais basculées au runtime :

  (i)  `CorthexisMemory` — MCP `sokkan-memory` existant (instance interne), en
       lecture seule (S1-S2, INCHANGÉ : c'est le service de prod du
       dépôt interne — on ne le réécrit pas, on s'y connecte comme le canari,
       `infra/nokker-canary/config.yaml`). C'est le défaut de `nokker.api.server.build_kernel`.
  (ii) `DirectCorthexisMemory` — paquet `corthexis` 2.1 EN DIRECT (bibliothèque Python en
       process, PAS un second serveur MCP) sur le Postgres + l'embed CPU du compose
       (`deploy/lokkal/` : Nokker Lokkal, lot X1b), reranker optionnel (S2-S3).

Les deux appliquent la MÊME politique de seuil, reprise À L'OCTET du hook de prod
(`hook_prompt_recall.py` du dépôt interne, recalée le 03.10.2026 sur 150
questions du banc : 53 % -> 87 % d'injections justes) :

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
import time
from dataclasses import dataclass, field
from typing import Any

from nokker.core.ports import Recall, ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.tools.mcp import _SyncMcp, text_of

logger = logging.getLogger("nokker.memory.corthexis")

DEFAULT_URL = ""  # aucune instance par défaut : `memory_mcp_url` de la config la nomme

# ───────────────────── seuils du hook de prod (partagés par les deux modes) ─────────────────────
# Port EXACT de `hook_prompt_recall.py` (dépôt interne) : SEUIL / SEUIL_RERANK /
# RERANK_OK, et `_nomme()`. Ne pas re-calibrer ici : tout changement de modèle d'embedding se
# recale SUR LE BANC (règle interne), côté hook de prod, et se
# reporte ici en changeant ces trois constantes — jamais à l'œil.
RECALL_SCORE_THRESHOLD = 0.40   # = SEUIL
RECALL_SEUIL_RERANK = 0.25      # = SEUIL_RERANK
RECALL_RERANK_OK = 0.5          # = RERANK_OK
_MIN_SEG_LEN = 4
# Démarrage : nouvel essai du contrôle de la mémoire longue directe (installation neuve : embed qui télécharge son modèle).
STARTUP_RETRY_S = 600
STARTUP_RETRY_EVERY_S = 15

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    """Port de `memory_search_server._tokens` : mots alphanumériques en minuscules."""
    return {w for w in _WORD_RE.findall((text or "").lower())}


def named(note_name: str, message: str) -> bool:
    """Port de `hook_prompt_recall._nomme` : nom complet cité (slug, "_"="-"), ou >= 2 segments
    de 4+ lettres du nom présents comme mots entiers (4+ lettres) du message. Un seul mot ne
    suffit plus depuis le 03.10.2026 (trop de rappels hors sujet)."""
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


def _no_credentials(text: str) -> str:
    """Une erreur de connexion peut citer le DSN : jamais le mot de passe dans un journal ni dans une route d'état."""
    return re.sub(r"(://[^:/@\s]+):[^@\s]+@", r"\1:***@", text)


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
    à l'embed CPU du compose (`deploy/lokkal/` : services `postgres` + `embed` — mesure
    MRR/injections/RAM dans les notes internes, § « A-2 »).

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
    # Règle de rappel. None (défaut) : celle du hook de prod (`kept()`, seuil 0,40 calibré AVEC reranker). Posé (Nokker
    # Lokkal, profil `leger` sans reranker) : celle de l'index local, comme la mémoire de projet (score ≥
    # `score_threshold` ET ≥ `relative_floor` × le meilleur, OU rerank ≥ `min_rerank`, OU nom de note cité).
    relative_floor: float | None = None
    min_rerank: float = RECALL_RERANK_OK
    _last_error: str = field(default="", init=False, repr=False)
    _last_warn: float = field(default=0.0, init=False, repr=False)
    _last_recall_s: float | None = field(default=None, init=False, repr=False)
    _status: tuple[float, dict] | None = field(default=None, init=False, repr=False)

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
        started = time.monotonic()
        try:
            from corthexis import service as cx_service
            rows = cx_service.search(query, top_k=max(k, 10), deep=self.deep, projects=self.projects)
        except Exception as e:  # noqa: BLE001 — la mémoire en panne ne casse jamais un tour, mais se voit
            self._failed(f"recherche indisponible : {e}", exc_info=True)
            return []
        # `corthexis.service.search` ne lève jamais : une panne est une LIGNE `{"error": …}` (base injoignable), un
        # embed tombé une ligne `degraded` (classement par mots seuls). Les deux se journalisent et entrent dans l'état.
        rows = [r for r in rows if isinstance(r, dict)]
        error = next((str(r["error"]) for r in rows if "error" in r), "")
        if error:
            self._failed(error)
            return []
        degraded = next((str(r["degraded"]) for r in rows if r.get("degraded")), "")
        if degraded:
            self._failed(degraded)
        else:
            self._last_error = ""
        out = self._keep([r for r in rows if "info" not in r], query, k)
        self._last_recall_s = time.monotonic() - started
        return out

    def _keep(self, rows: list[dict], query: str, k: int) -> list[Recall]:
        best = max((float(r.get("score") or 0.0) for r in rows), default=0.0)
        out: list[Recall] = []
        for row in rows:
            score = float(row.get("score") or 0.0)
            note = str(row.get("note_name") or "?")
            rerank = row.get("rerank")
            rerank = float(rerank) if rerank is not None else None
            if self.relative_floor is None:
                ok = kept(score, rerank, note, query, threshold=self.score_threshold)
            else:
                ok = (rerank is not None and rerank >= self.min_rerank) or named(note, query) or (
                    score >= self.score_threshold and score >= self.relative_floor * best)
            if not ok:
                continue
            # Description d'abord (ce qu'EST la note), puis l'extrait qui a matché — comme en mode (i).
            desc = " ".join(str(row.get("description") or "").split())
            snippet = " ".join(str(row.get("snippet") or "").split())
            text = f"{desc} — extrait : {snippet}" if desc and snippet and snippet not in desc else (desc or snippet)
            shown = score if self.relative_floor is None else max(score, rerank or 0.0)
            out.append(Recall(note=note, text=text, score=shown, rerank=rerank))
            if len(out) >= k:
                break
        return out

    def _failed(self, message: str, *, exc_info: bool = False) -> None:
        """Panne du rappel : gardée pour l'état (`status`, `doctor`) et journalisée, au plus une fois par minute."""
        self._last_error = _no_credentials(message)[:300]
        self._status = None
        now = time.monotonic()
        if now - self._last_warn >= 60.0:
            self._last_warn = now
            logger.warning("corthexis(direct): %s — le rappel de la mémoire longue est vide ou dégradé",
                           self._last_error, exc_info=exc_info)

    # ───────────────────────────── état (démarrage, doctor) ─────────────────────────────

    def check(self) -> dict:
        """État réel : paquet, base (notes, génération d'index active), serveur d'embeddings (un vrai plongement).
        Ne lève jamais ; `ok` False = la mémoire longue ne répond pas, avec la raison dans `error`."""
        out: dict[str, Any] = {"mode": "direct", "ok": False, "projects": list(self.projects or []) or None,
                               "profile": os.environ.get("CORTHEXIS_MEMORY_PROFILE") or self.memory_profile}
        try:
            from corthexis import embed as cx_embed
            from corthexis import service as cx_service
        except Exception as e:  # noqa: BLE001
            out["error"] = f"paquet corthexis absent de l'image ({e})"
            return out
        try:
            stats = cx_service.get_store().stats()
            out["store"] = {"ok": True, "notes": stats.get("notes"), "chunks": stats.get("chunks"),
                            "generation": stats.get("generation"), "model": stats.get("model")}
        except Exception as e:  # noqa: BLE001
            out["store"] = {"ok": False}
            out["error"] = f"base CortHeXis injoignable : {_no_credentials(str(e))}"[:300]
            return out
        try:
            t0 = time.monotonic()
            vec = cx_embed.get().embed_query("ping", timeout=5.0)
            out["embed"] = {"ok": bool(vec), "dim": len(vec or []), "ms": round((time.monotonic() - t0) * 1000)}
        except Exception as e:  # noqa: BLE001
            out["embed"] = {"ok": False}
            out["error"] = f"serveur d'embeddings injoignable : {e}"[:300]
            return out
        out["ok"] = True
        if self._last_error:
            out["last_error"] = self._last_error
        if self._last_recall_s is not None:
            out["last_recall_ms"] = round(self._last_recall_s * 1000)
        return out

    def status(self, max_age_s: float = 15.0) -> dict:
        """`check()` gardé `max_age_s` secondes : `/healthz` est sondé toutes les 15 s par le compose."""
        cached = self._status
        if cached is not None and time.monotonic() - cached[0] < max_age_s:
            return cached[1]
        st = self.check()
        self._status = (time.monotonic(), st)
        return st

    def warm_up(self) -> None:
        """Contrôle de démarrage en arrière-plan (le noyau ne l'attend pas) : une base ou un embed injoignable se voit
        dans le journal en ERREUR, et dans `status()` — jamais une mémoire vide silencieuse."""
        def _go() -> None:
            # Installation neuve : l'embed télécharge encore son modèle quelques minutes. Avertissement et nouvel essai
            # pendant STARTUP_RETRY_S ; ERREUR seulement si la mémoire est toujours injoignable ensuite.
            deadline = time.monotonic() + STARTUP_RETRY_S
            while True:
                st = self.status(max_age_s=0.0)
                if st.get("ok"):
                    store = st.get("store") or {}
                    logger.info("mémoire longue CortHeXis (direct) prête : %s note(s), projets %s, embed %s ms",
                                store.get("notes"), st.get("projects") or "tous", (st.get("embed") or {}).get("ms"))
                    return
                if time.monotonic() >= deadline:
                    logger.error("mémoire longue CortHeXis (direct) INDISPONIBLE au démarrage : %s — rappel vide tant "
                                 "que ce n'est pas réparé (état : GET /v1/local/memory, /healthz)", st.get("error"))
                    return
                logger.warning("mémoire longue CortHeXis (direct) pas encore prête : %s — nouvel essai dans %d s",
                               st.get("error"), STARTUP_RETRY_EVERY_S)
                time.sleep(STARTUP_RETRY_EVERY_S)
        threading.Thread(target=_go, name="memory-check", daemon=True).start()

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
    démarrage ; le noyau passe par `from_config()` ci-dessous (`memory_mode`, `NOKKER_MEMORY_MODE`)."""
    mode = (mode or os.environ.get("NOKKER_MEMORY_MODE") or "mcp").strip().lower()
    if mode == "mcp":
        return CorthexisMemory(**kwargs)
    if mode == "direct":
        return DirectCorthexisMemory(**kwargs)
    raise ValueError(f"NOKKER_MEMORY_MODE={mode!r} inconnu (attendu : mcp | direct)")


MODES = ("mcp", "direct", "off")
DIRECT_DEFAULT_PROJECTS = ("u-local",)  # = project_memory.USER_PROJECT : les notes de portée user, tous fils confondus


def memory_mode(config: Any) -> str:
    """`memory_mode` de la config, sinon `NOKKER_MEMORY_MODE`, sinon `mcp`. Inconnu : erreur au démarrage."""
    mode = (getattr(config, "memory_mode", "") or os.environ.get("NOKKER_MEMORY_MODE") or "mcp").strip().lower()
    if mode not in MODES:
        raise ValueError(f"memory_mode / NOKKER_MEMORY_MODE = {mode!r} inconnu (attendu : {' | '.join(MODES)})")
    return mode


def from_config(config: Any) -> CorthexisMemory | DirectCorthexisMemory | None:
    """Mémoire longue d'un noyau (`nokker.api.server.build_kernel`), UNE fois au démarrage.

    - `mcp` : le MCP `memory_mcp_url` (inchangé) ; URL vide = aucune mémoire longue (bêta, voulu : None).
    - `direct` : `DirectCorthexisMemory` sur le Postgres + l'embed du compose (Nokker Lokkal), scope `projects`
      (défaut `u-local`, jamais « tout » : les fils `s-…` et les dépôts `p-…` ont leur propre rappel, cloisonné), seuils
      de l'index local. Construite même si la base ou l'embed ne répondent pas : la panne se voit (journal en erreur,
      `status()`), elle ne devient jamais une mémoire nulle silencieuse.
    - `off` : None, explicitement."""
    mode = memory_mode(config)
    if mode == "off":
        logger.info("mémoire longue : désactivée (memory_mode=off)")
        return None
    if mode == "mcp":
        if not config.memory_mcp_url:
            return None
        mem = CorthexisMemory(url=config.memory_mcp_url, token=config.memory_token())
        mem.warm_up()
        return mem
    d = dict(getattr(config, "memory_direct", None) or {})
    projects = d.get("projects", list(DIRECT_DEFAULT_PROJECTS))
    mem = DirectCorthexisMemory(
        database_url=os.environ.get(str(d.get("database_url_env") or "CORTHEXIS_DATABASE_URL"), ""),
        embed_urls=str(d.get("embed_urls") or ""), memory_profile=str(d.get("profile") or "leger"),
        rerank_url=str(d.get("rerank_url") or ""), deep=bool(d.get("deep", False)),
        projects=tuple(str(p) for p in projects) if projects is not None else None,
        score_threshold=float(d.get("min_score", 0.15)), relative_floor=float(d.get("relative_floor", 0.6)),
        min_rerank=float(d.get("min_rerank", 0.35)))
    if not os.environ.get("CORTHEXIS_DATABASE_URL"):
        logger.error("mémoire longue CortHeXis (direct) : CORTHEXIS_DATABASE_URL absente — rappel vide")
    mem.warm_up()
    return mem


def unwrap(port: Any) -> Any:
    """La mémoire longue sous ses enveloppes (`TenantMemory.internal`, `ProjectRecallMemory.inner`)."""
    for _ in range(5):
        if port is None or hasattr(port, "status") or isinstance(port, CorthexisMemory):
            return port
        port = getattr(port, "internal", None) or getattr(port, "inner", None)
    return port


def status_of(port: Any) -> dict:
    """État de la mémoire longue d'un noyau, pour `/healthz` et `GET /v1/local/memory`. `ok` None : pas de contrôle
    (mode `mcp` : le service distant a le sien ; aucune mémoire : `mode` off)."""
    mem = unwrap(port)
    if mem is None or type(mem).__name__ == "NullMemory":
        return {"mode": "off", "ok": None}
    if not hasattr(mem, "status"):
        return {"mode": "mcp", "ok": None}
    try:
        return mem.status()
    except Exception as e:  # noqa: BLE001 — un état illisible est une panne, pas une exception de route
        return {"mode": "direct", "ok": False, "error": _no_credentials(str(e))[:300]}


__all__ = [
    "DEFAULT_URL",
    "DIRECT_DEFAULT_PROJECTS",
    "MODES",
    "RECALL_RERANK_OK",
    "RECALL_SCORE_THRESHOLD",
    "RECALL_SEUIL_RERANK",
    "CorthexisMemory",
    "DirectCorthexisMemory",
    "build",
    "from_config",
    "kept",
    "memory_mode",
    "named",
    "status_of",
    "unwrap",
]
