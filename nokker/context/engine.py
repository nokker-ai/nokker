"""Context Engine v0 (`core.ports.ContextEngine`) — PLAN-P1 §2.4. Le v1 complet (réservoir réel, frontières
d'épisode, consolidation) est le chantier A-1 ; ceci respecte le contrat minimal :

  [1 préfixe statique] [2 profil] [3 épisodes clos (vide en v0 — pas de frontières)] [4 tours bruts]
  [5 bloc du tour : message + rappel + index court des outils non cœur + schémas chargés]

Invariant vérifié par construction (et testé) : le préfixe NE dépend ni du palier ni de l'heure — il est
calculé une fois depuis le fichier + les schémas des outils cœur, jamais recalculé par appel. `layer_tokens`
est une estimation `len(texte) // 4` (documentée partout où elle est utilisée) en attendant un vrai
tokenizer ; ce n'est pas la mesure qui a produit les chiffres du PLAN (ceux-là viennent du tokenizer réel
du modèle, cf. note mémoire `nokker-p0` §F4).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from nokker.tools.select import compact_index
from nokker.core.ports import Assembled, Recall, ToolRegistry, ToolSpec
from nokker.core.turn import Tier, ToolResult, Turn
from nokker.tenancy.context import INTERNAL_TENANT, current_tenant

logger = logging.getLogger("nokker.context.engine")

DEFAULT_PREFIX_PATH = str(Path(__file__).resolve().parent.parent / "prompts" / "prefix-nokker.md")
ZURICH = ZoneInfo("Europe/Zurich")

_FR_WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_FR_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
              "octobre", "novembre", "décembre"]


def estimate_tokens(text: str) -> int:
    """`len // 4` — placeholder documenté (PLAN §2.4), en attendant un tokenizer réel (A-1)."""
    return max(0, len(text) // 4)


def format_now_fr(now: datetime) -> str:
    """« mardi 20 octobre 2026, 14:05 (Europe/Zurich) — ISO 2026-10-20T14:05:00+02:00 » — jour de la
    semaine et mois en français écrits à la main (pas de dépendance à la locale du système, qui peut
    être absente ou différente selon l'hôte), + l'ISO 8601 pour toute résolution programmatique."""
    weekday = _FR_WEEKDAYS[now.weekday()]
    month = _FR_MONTHS[now.month - 1]
    return (f"{weekday} {now.day} {month} {now.year}, {now:%H:%M} (Europe/Zurich) — "
            f"ISO {now.isoformat(timespec='seconds')}")


@dataclass
class ContextEngineV0:
    tools: ToolRegistry  # pour l'index court des outils non cœur (couche 5) — pas dans le Protocol
    prefix_path: str = DEFAULT_PREFIX_PATH
    profile_path: str | None = None
    reservoir_char_limit: int = 4000  # au-delà : tronqué + mention (réservoir réel = A-1)
    recall_text_limit: int = 1200  # caractères max par note rappelée dans le bloc du tour
    # Horloge injectable (P1-S1 itération 3, 17.10.2026) : le modèle n'avait AUCUNE date, donc ne
    # pouvait pas résoudre « mardi prochain »/« le 24 »/« vendredi » en date absolue, et appelait
    # `clarify` pour demander — mesuré en réel sur `kanban-ajout-tache`/`calendrier-type-entite`.
    # Défaut = horloge réelle (Europe/Zurich) ; un appelant (banc, test) peut figer une date précise.
    # TOUJOURS dans le bloc du tour (couche 5), JAMAIS dans le préfixe (A4 : hash stable entre tours
    # et paliers — un préfixe qui daterait changerait de hash à chaque appel, cassant le cache).
    now_fn: Callable[[], datetime] = field(default=lambda: datetime.now(ZURICH))
    # Préfixe produit (docs/TENANCY.md §5). Posé (`build_kernel`) : préfixe et profil suivent le locataire courant —
    # interne → `prefix_path` + `profile_path`, testeur → ce fichier, sans profil ; hors d'un `tenant_scope`, NoTenant.
    # None : un seul préfixe pour tous (tests, bancs qui construisent l'engine eux-mêmes).
    product_prefix_path: str | None = None

    def __post_init__(self) -> None:
        self._layers = {"internal": (self._load(self.prefix_path),
                                     self._load(self.profile_path) if self.profile_path else "")}
        if self.product_prefix_path is not None:
            self._layers["product"] = (self._load(self.product_prefix_path), "")
        # Hash par locataire, figé au premier assemblage du locataire : ne dépend d'aucun tour, palier ou horloge
        # (invariant testé). Sans préfixe produit : calculé à la construction, comme avant.
        self._sha_by_tenant: dict[str, str] = {}
        self._sha_lock = threading.Lock()
        if self.product_prefix_path is None:
            self._sha_by_tenant[""] = _compute_prefix_sha256(self._prefix_text, self.tools.core())

    def _layer(self) -> tuple[str, str]:
        """(préfixe, profil) du locataire courant."""
        if self.product_prefix_path is None or current_tenant() == INTERNAL_TENANT:
            return self._layers["internal"]
        return self._layers["product"]

    @property
    def _prefix_text(self) -> str:
        return self._layer()[0]

    @property
    def _profile_text(self) -> str:
        return self._layer()[1]

    @property
    def _prefix_sha256(self) -> str:
        key = "" if self.product_prefix_path is None else current_tenant()
        sha = self._sha_by_tenant.get(key)
        if sha is None:
            sha = _compute_prefix_sha256(self._prefix_text, self.tools.core())
            with self._sha_lock:
                sha = self._sha_by_tenant.setdefault(key, sha)
        return sha

    @staticmethod
    def _load(path: str | None) -> str:
        if not path:
            return ""
        p = Path(path)
        if not p.exists():
            logger.warning("context engine: fichier absent, couche vide : %s", path)
            return ""
        return p.read_text(encoding="utf-8")

    def tools_index_block(self) -> str:
        """Index des outils non cœur, noms par famille (`tools/select.py::compact_index`). Dans le message système,
        après le préfixe : stable d'un tour à l'autre tant que les sources d'outils ne changent pas, et indépendant
        des schémas chargés pour le tour (les descriptions arrivent avec le schéma, ou par tool_find)."""
        return compact_index([name for name, _ in self.tools.index()])

    def system_parts(self, index_block: str) -> list[str]:
        """Message système unique, du plus stable au moins stable (cache de préfixe) : préfixe, index d'outils,
        profil. V1 y ajoute la mémoire de session, qui change à la clôture d'un épisode."""
        parts = [self._prefix_text]
        if index_block:
            parts.append(index_block)
        if self._profile_text:
            parts.append(self._profile_text)
        return parts

    @staticmethod
    def turn_head(turn: Turn, compute: Callable[[], list[str]]) -> str:
        """En-tête du tour, calculé au premier assemblage puis figé sur le tour (`Turn.head`) : les appels suivants
        du même tour et l'historique des tours suivants le reprennent à l'identique."""
        if not turn.head:
            turn.head = "\n\n".join(compute())
        return turn.head

    @staticmethod
    def past_user(past: Turn, with_head: bool = True) -> str:
        """Message `user` d'un tour tel qu'il a été envoyé : en-tête figé + texte. Sans en-tête (tour d'avant
        `Turn.head`, ou en-tête délesté par le never clear) : contexte reçu + texte, comme avant."""
        if past.head and with_head:
            return "\n\n".join([past.head, past.input.text])
        if past.injected:
            return "\n\n".join([render_injected(past.injected), past.input.text])
        return past.input.text

    def assemble(self, turn: Turn, history: list[Turn], recall: list[Recall], loaded_tools: list[ToolSpec],
                 tier: Tier) -> Assembled:
        core_tools = self.tools.core()
        messages: list[dict] = []

        # Couche 1+2 — préfixe stable + profil (optionnel), FUSIONNÉS en un seul message « system »,
        # EN TÊTE. Mesuré en réel le 17.10.2026 (P1-S1, contre le Router SOKKAN en prod) : une requête
        # qui porte un message `system` qui n'est pas `messages[0]`, ou plus d'un message `system`,
        # échoue systématiquement en `502 Bad Gateway` amont (reproduit en isolant chaque combinaison
        # de rôles : `sys,user`→200, `user,sys`→502, `sys,sys`→502, `sys,user,sys`→502,
        # `sys,user,user`→200, `sys,user,assistant,user`→200 — aucune des deux docs, OpenAI ni Router,
        # ne documente cette limite ; elle n'est pas dans le contrat de ce module, qui n'a pas à la
        # deviner, mais elle casse la couche 5 d'origine, qui ajoutait le rappel et l'index d'outils
        # comme des messages `system` SUPPLÉMENTAIRES après le tour humain). Un seul message `system`
        # en tête + tout le reste en rôle `user`/`assistant`/`tool` contourne la limite sans changer ce
        # que le modèle reçoit comme INFORMATION. Le hash du préfixe (A4, `_compute_prefix_sha256`) ne
        # change pas : il porte sur `self._prefix_text` seul, pas sur ce message combiné.
        index_block = self.tools_index_block()
        messages.append({"role": "system", "content": "\n\n".join(self.system_parts(index_block))})

        # Couche 3 — épisodes clos : vide en v0 (aucune frontière d'épisode, cf. docstring). Budget 0.
        episodes_text = ""

        # Couche 4 — tours bruts de l'épisode courant, append-only, chacun avec SON en-tête d'origine (`past_user`).
        for past in history:
            messages.append({"role": "user", "content": self.past_user(past)})
            messages.extend(m.to_openai() for m in past.messages)

        # Couche 5 — en-tête du tour (date, rappel CortHeXis, contexte reçu) + message courant, REGROUPÉS DANS LE MÊME
        # message `user` (même raison que ci-dessus : un message `system` qui n'est pas en tête fait échouer l'appel
        # en 502 ; ce tour reste le DERNIER message, rôle `user`, ce que tout amont accepte).
        # FORMAT = CONTRAT avec l'entraînement de Nina-70B (JUPITER, `infra/sokkan/jupiter/py/nokker_tasks.py::with_recall`) :
        # `[Rappel mémoire]\n…\n[/Rappel]\n\n<message>` — rappel AVANT le message, balise fermante. Ne pas changer l'un
        # sans l'autre (cf docs/CONTEXT-FORMAT.md). L'index des outils non cœur est dans le message système depuis le
        # 20.10.2026 (option A, cache entre tours) ; l'en-tête est figé par tour et rejoué tel quel par l'historique.
        def head_parts() -> list[str]:
            # Date FIGÉE avec l'en-tête du tour (`turn_head` ne calcule qu'une fois par tour) : recalculée à chaque appel,
            # elle changeait le bloc du tour et tout ce qui le suit (préfixe réutilisable 75-85 % au lieu de ~100 %,
            # mesuré sur le canari le 20.10.2026).
            parts = [f"[Date et heure actuelles]\n{format_now_fr(self.now_fn())}\n[/Date]"]
            if recall:
                rappel = "\n".join(
                    f"- [{r.note}] (score {r.score:.2f}) {r.text[:self.recall_text_limit]}" for r in recall)
                parts.append(f"[Rappel mémoire]\n{rappel}\n[/Rappel]")
            if turn.injected:
                # Contexte reçu d'un autre fil (docs/BUBBLES.md §3-§4, docs/CONTEXT-FORMAT.md) : après le rappel.
                parts.append(render_injected(turn.injected))
            return parts

        head = self.turn_head(turn, head_parts)
        messages.append({"role": "user", "content": self.past_user(turn)})

        # Rejoue l'ÉPISODE COURANT de CE tour : les tool_calls/résultats déjà produits PAR CE TOUR,
        # avant ce call_index (`core/loop.py` rappelle `assemble()` à chaque appel du MÊME tour, pas
        # seulement une fois par tour). Bug réel trouvé en mesurant contre le Router (P1-S1,
        # 17.10.2026) : sans cette ligne, le modèle ne voit JAMAIS ses propres appels d'outils déjà
        # faits dans ce tour — chaque appel suivant lui repose EXACTEMENT la question d'origine, sans
        # aucune trace de ce qu'il vient de faire. Conséquence observée en réel : `memory_search` et
        # `infra_disk_usage` rappelés en boucle (jusqu'à 14 fois) jusqu'à `max_calls`, le modèle
        # « redémarrant » à chaque appel sans savoir qu'il a déjà le résultat. Le Protocol
        # `ContextEngine` (core/ports.py) passe le `Turn` ENTIER précisément pour ça — ce n'était pas
        # exploité. Rôles produits ici : `assistant` (avec tool_calls) et `tool` seulement, jamais
        # `system` (cf. couche 1+2/5 ci-dessus : un `system` qui n'est pas en tête fait échouer la
        # requête en 502) — donc aucun conflit avec cette contrainte.
        messages.extend(m.to_openai() for m in turn.messages)

        tools_payload = [s.to_openai() for s in core_tools] + [s.to_openai() for s in loaded_tools]

        layer_tokens = {
            "prefix": estimate_tokens(self._prefix_text),
            "tools_index": estimate_tokens(index_block),
            "profile": estimate_tokens(self._profile_text),
            "episodes": estimate_tokens(episodes_text),
            "current": estimate_tokens("".join(self.past_user(p) for p in history)),
            "turn_block": estimate_tokens("\n\n".join([head, turn.input.text])),
        }
        return Assembled(messages=messages, tools=tools_payload, prefix_sha256=self._prefix_sha256,
                          layer_tokens=layer_tokens)

    def admit_result(self, result: ToolResult) -> ToolResult:
        if len(result.content) <= self.reservoir_char_limit:
            return result
        truncated = result.content[:self.reservoir_char_limit]
        # `recall_context` retiré de la surface tant que A-1 n'existe pas (décision Nick, P1-S1,
        # 17.10.2026) : ne JAMAIS promettre au modèle un outil qu'il ne peut pas appeler.
        mention = (f"\n[...tronqué à {self.reservoir_char_limit} caractères sur {len(result.content)} — "
                   "le réservoir réel arrive avec le chantier A-1 ; pas de relecture possible pour l'instant]")
        return ToolResult(call_id=result.call_id, name=result.name, content=truncated + mention,
                           is_error=result.is_error, reservoir_id=None, secret_handles=result.secret_handles)


def render_injected(blocks: list[str]) -> str:
    """`[Contexte reçu]\n<un bloc par ligne>\n[/Contexte]` — format docs/CONTEXT-FORMAT.md."""
    return "[Contexte reçu]\n" + "\n".join(blocks) + "\n[/Contexte]"


def _compute_prefix_sha256(prefix_text: str, core_tools: list[ToolSpec]) -> str:
    canon_tools = sorted(
        ({"name": t.name, "description": t.description, "parameters": t.parameters} for t in core_tools),
        key=lambda d: d["name"],
    )
    payload = prefix_text + "\n" + json.dumps(canon_tools, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["DEFAULT_PREFIX_PATH", "ZURICH", "ContextEngineV0", "estimate_tokens", "format_now_fr", "render_injected"]
