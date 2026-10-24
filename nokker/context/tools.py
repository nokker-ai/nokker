"""`recall_context` — l'outil de chargement à la demande (docs/NEVER-CLEAR.md §2). Source d'outils locale, limitée au fil
appelant (`ToolContext.thread_id`) : jamais de lecture inter-fils."""
from __future__ import annotations

from typing import Any

from nokker.context.manager import ContextManager
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult

RECALL_TOOL = "recall_context"


class RecallContextSource:
    def __init__(self, manager: ContextManager) -> None:
        self.manager = manager
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
            return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)
        text = self.manager.recall(ctx.thread_id, ref=str(arguments.get("ref") or "") or None,
                                   query=str(arguments.get("query") or "") or None,
                                   part=str(arguments.get("part") or "auto"),
                                   max_chars=int(arguments.get("max_chars") or 3000),
                                   offset=int(arguments.get("offset") or 0))
        return ToolResult(call_id="", name=name, content=text)


__all__ = ["RECALL_TOOL", "RecallContextSource"]
