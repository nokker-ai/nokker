"""Outil du modèle pour la rubrique (docs/OUTCOMES-GOAL.md) : `rubric_propose`, en effet READ — il POSE une proposition
à côté de l'objectif du fil ; elle ne sert à noter qu'une fois figée par l'utilisateur (« /goal valide » ou l'API)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.qa.outcomes import MAX_CRITERIA, RubricError

RUBRIC_TOOL = "rubric_propose"
RUBRIC_TOOL_DESCRIPTION = ("PROPOSE une rubrique pour la tâche ou l'objectif du fil : des critères vérifiables qu'un "
                           "correcteur séparé notera. L'utilisateur la fige ou la refuse ; elle ne sert à rien tant "
                           "qu'elle n'est pas figée.")
_PARAMS = {
    "type": "object",
    "properties": {
        "criteria": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_CRITERIA,
                     "description": "critères vérifiables, un par élément (ce qui doit être vrai à la fin, et comment "
                                    "le constater)"},
        "objective": {"type": "string", "description": "l'objectif en une phrase, s'il n'y en a pas encore sur ce fil"},
    },
    "required": ["criteria"],
}
PROPOSED_NOTE = ("rubrique proposée : l'utilisateur la voit et la fige (« /goal valide ») ou la refuse. Tant qu'elle "
                 "n'est pas figée, elle ne sert pas à noter ; ne dis pas qu'elle est en place.")


@dataclass
class GoalToolSource:
    service: Any  # nokker.goals.service.GoalService

    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=RUBRIC_TOOL, description=RUBRIC_TOOL_DESCRIPTION, parameters=_PARAMS,
                         effect=Effect.READ, source="builtin")]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if name != RUBRIC_TOOL:
            return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)
        try:
            g = self.service.propose(ctx.thread_id, ctx.sender or "", arguments.get("criteria"),
                                     str(arguments.get("objective") or ""))
        except RubricError as e:
            return ToolResult(call_id="", name=name, content=f"refusé : {e}", is_error=True)
        return ToolResult(call_id="", name=name, content=json.dumps(
            {"proposed": [f"C{i}. {t}" for i, t in enumerate(g.proposed or [], 1)], "note": PROPOSED_NOTE},
            ensure_ascii=False))


__all__ = ["PROPOSED_NOTE", "RUBRIC_TOOL", "RUBRIC_TOOL_DESCRIPTION", "GoalToolSource"]
