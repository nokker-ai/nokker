"""`recall_context` — l'outil de chargement à la demande (docs/NEVER-CLEAR.md §2). Source d'outils locale, limitée au fil
appelant (`ToolContext.thread_id`) : jamais de lecture inter-fils."""
from __future__ import annotations

from typing import Any

from nokker.context.manager import RECALL_TOOL, ContextManager
from nokker.context.tokens import estimate_tokens
from nokker.core import model_texts as mt
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult


class RecallContextSource:
    EMPTY = ("Rien de pertinent trouvé dans la mémoire de ce fil.",)  # résultat vide de `ContextManager.recall`

    def __init__(self, manager: ContextManager) -> None:
        self.manager = manager
        self._spent: dict[tuple[str, int], int] = {}  # (fil, tour) -> jetons rendus par les recherches du tour
        self._empty: dict[tuple[str, int], int] = {}  # (fil, tour) -> recherches vides déjà rendues
        self._spec = ToolSpec(
            name=RECALL_TOOL,
            description=("Relit ce qui est sorti de la fenêtre : un épisode clos (ep_…), un résultat d'outil délesté ou "
                         "un export (rs_…), un résumé de jour (dy_…) ; ou cherche dans la mémoire de CETTE session "
                         "(query). Donne `ref` OU `query`."),
            parameters={"type": "object", "properties": {
                "ref": {"type": "string", "description": "identifiant ep_…, rs_… ou dy_… vu dans le contexte"},
                "query": {"type": "string", "description": "mots-clés pour retrouver un épisode ou un résultat"},
                "part": {"type": "string", "enum": ["summary", "raw"],
                         "description": "pour un épisode : résumé (défaut) ou transcription complète"},
                "max_chars": {"type": "integer", "description": "taille de la page (200-8000, défaut 3000)"},
                "offset": {"type": "integer", "description": "décalage de lecture pour la page suivante"}}},
            effect=Effect.READ, core=True, source="builtin")

    def specs(self) -> list[ToolSpec]:
        return [self._spec]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if name != RECALL_TOOL:
            return ToolResult(call_id="", name=name, content=mt.pick(mt.UNKNOWN_TOOL_BARE).format(name=name), is_error=True)
        key = (ctx.thread_id, ctx.turn_index)
        ref = str(arguments.get("ref") or "").strip()
        by_name = ref or str(arguments.get("query") or "").strip()
        if by_name and not by_name.startswith(("ep_", "rs_", "dy_")) and self.manager.value_lookup is not None:
            # relecture d'UNE valeur de la mémoire par son NOM (en `ref` ou en `query`) : une lecture, pas une recherche
            # (hors du compte des leurres) ; un nom inconnu retombe sur le comportement d'avant
            try:
                line = self.manager.value_lookup(ctx.thread_id, by_name)
            except Exception:  # noqa: BLE001
                line = None
            if line:
                return ToolResult(call_id="", name=name, content=line)
        query = bool(str(arguments.get("query") or "").strip()) and not ref
        budget = int(getattr(self.manager.budgets, "recall_budget_tokens", 0) or 0)
        if query and (self._empty.get(key, 0) >= 2 or (budget > 0 and self._spent.get(key, 0) >= budget)):
            # deux recherches vides déjà, ou le plafond de jetons des recherches du tour atteint : plus de relance
            return ToolResult(call_id="", name=name, content=mt.pick(mt.RECALL_NOTHING_FOUND))
        text = self.manager.recall(ctx.thread_id, ref=str(arguments.get("ref") or "") or None,
                                   query=str(arguments.get("query") or "") or None,
                                   part=str(arguments.get("part") or "auto"),
                                   max_chars=int(arguments.get("max_chars") or 3000),
                                   offset=int(arguments.get("offset") or 0))
        if query:
            self._spent[key] = self._spent.get(key, 0) + estimate_tokens(text)
            while len(self._spent) > 2000:
                self._spent.pop(next(iter(self._spent)), None)
        if query and text.strip() in self.EMPTY:
            n = self._empty.get(key, 0) + 1
            self._empty[key] = n
            while len(self._empty) > 2000:  # borne mémoire
                self._empty.pop(next(iter(self._empty)), None)
            if n >= 2:  # 2e résultat vide du tour : le noyau dit au modèle de répondre « rien trouvé »
                text = mt.pick(mt.RECALL_NOTHING_FOUND)
        return ToolResult(call_id="", name=name, content=text)


__all__ = ["RECALL_TOOL", "RecallContextSource"]
