"""Outils du modèle pour le plan (docs/LOKKAL.md §7.18) — tous en effet READ : ils lisent le plan ou POSENT une
proposition ; aucun ne crée, ne modifie ni ne supprime une carte (seul un humain le fait, par les routes ou la bulle)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.plan.service import BRIEF_TOOL, Plan
from nokker.plan.types import PlanError, proposal_to_json

_GET_PARAMS = {
    "type": "object",
    "properties": {"card_id": {"type": "string", "description": "id de la carte (pc_…) ; absent = la carte de ce fil, "
                                                                "sinon les objectifs"}},
}
_SPLIT_PARAMS = {
    "type": "object",
    "properties": {
        "card_id": {"type": "string", "description": "carte à découper (pc_…)"},
        "children": {"type": "array", "description": "cartes filles proposées, dans l'ordre",
                     "items": {"type": "object", "properties": {
                         "title": {"type": "string"},
                         "kind": {"type": "string", "enum": ["goal", "milestone", "task"]},
                         "intent": {"type": "string", "description": "ce que la carte doit obtenir"},
                         "constraints": {"type": "array", "items": {"type": "string"}}},
                         "required": ["title"]}},
        "reason": {"type": "string", "description": "pourquoi ce découpage, en une ou deux phrases"},
    },
    "required": ["card_id", "children"],
}


@dataclass
class PlanToolSource:
    plan: Plan

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="plan_get", description="Lit une carte du plan (objectif, lot, tâche) : consignes héritées, "
                     "avancement, cartes filles.", parameters=_GET_PARAMS, effect=Effect.READ, source="builtin"),
            ToolSpec(name="plan_propose_split", description="PROPOSE de découper une carte du plan en cartes filles ; "
                     "l'utilisateur accepte ou refuse (ne crée AUCUNE carte par lui-même).",
                     parameters=_SPLIT_PARAMS, effect=Effect.READ, source="builtin"),
            ToolSpec(name=BRIEF_TOOL, description="Lit de quoi faire le brief du matin : objectifs, tâches en cours, "
                     "bloquées et prochaines, incidents ouverts.", parameters={"type": "object", "properties": {}},
                     effect=Effect.READ, source="builtin"),
        ]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            if name == "plan_get":
                return self._get(arguments, ctx)
            if name == "plan_propose_split":
                p = self.plan.propose_split(str(arguments.get("card_id") or ""), arguments.get("children"),
                                            str(arguments.get("reason") or ""), ctx.sender or "nina",
                                            thread_id=ctx.thread_id)
                return ToolResult(call_id="", name=name, content=json.dumps({
                    "proposal": proposal_to_json(p),
                    "note": "proposition posée : l'utilisateur la voit et décide. Aucune carte n'est créée tant qu'il "
                            "ne l'a pas acceptée ; ne dis pas qu'elles existent."}, ensure_ascii=False))
            if name == BRIEF_TOOL:
                return ToolResult(call_id="", name=name,
                                  content=json.dumps(self.plan.brief_data(ctx.sender), ensure_ascii=False))
        except PlanError as e:
            return ToolResult(call_id="", name=name, content=f"refusé : {e.message}", is_error=True)
        return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)

    def _get(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        card_id = str(arguments.get("card_id") or "")
        if not card_id:
            idx = self.plan._index()
            mine = self.plan.card_for_thread(idx, ctx.thread_id)
            if mine is None:
                goals = self.plan.list_cards(roots=True)
                return ToolResult(call_id="", name="plan_get", content=json.dumps(
                    {"goals": [{k: g[k] for k in ("id", "title", "status", "progress")} for g in goals]},
                    ensure_ascii=False))
            card_id = mine.id
        card = self.plan.get_card(card_id)
        card["context"] = self.plan.context(card_id)["block"]
        return ToolResult(call_id="", name="plan_get", content=json.dumps(card, ensure_ascii=False))


__all__ = ["PlanToolSource"]
