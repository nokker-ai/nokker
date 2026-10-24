"""`Policy` v0 (`core.ports.Policy`) — PLAN-P1 §2.6/§5.1 (F2). Règle fixe, pas encore de classe de tâche
ni d'ancre de cache (ça, c'est C-2) :

  - Ship par défaut, sans raisonnement ;
  - Deep si la dernière porte bascule a rendu REDO_DEEP, ou si `tool_failures >= deep_tool_failures`,
    ou si `repeated_error` ;
  - le motif (`RouteDecision.reason`) est TOUJOURS renseigné (journalisé au ledger, C2).
"""
from __future__ import annotations

from dataclasses import dataclass

from nokker.core.ports import RouteDecision, RouteState
from nokker.core.turn import Decision, Tier


@dataclass
class PolicyV0:
    ship_reasoning: bool = False
    deep_reasoning: bool = True
    deep_tool_failures: int = 3

    def choose(self, state: RouteState) -> RouteDecision:
        if any(v.decision is Decision.REDO_DEEP for v in state.switch_verdicts):
            return RouteDecision(tier=Tier.DEEP, reasoning=self.deep_reasoning,
                                  reason="porte bascule : verdict REDO_DEEP")
        if state.tool_failures >= self.deep_tool_failures:
            return RouteDecision(tier=Tier.DEEP, reasoning=self.deep_reasoning,
                                  reason=f"{state.tool_failures} échecs d'outil >= {self.deep_tool_failures}")
        if state.repeated_error:
            return RouteDecision(tier=Tier.DEEP, reasoning=self.deep_reasoning,
                                  reason="même signature d'erreur deux fois")
        return RouteDecision(tier=Tier.SHIP, reasoning=self.ship_reasoning, reason="défaut : Ship")


__all__ = ["PolicyV0"]
