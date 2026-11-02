"""Palier imposé à un sous-agent (`delegate`, paramètre `tier`, drapeau `delegate_tier`).

`ForcedTierPolicy` rend toujours le palier demandé ; le plafond du compte (`LimitedPolicy`, `deep_per_day`) reste PAR-DESSUS :
un enfant ne dépasse jamais les droits du parent."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nokker.core.ports import RouteDecision, RouteState
from nokker.core.turn import Tier


@dataclass
class ForcedTierPolicy:
    inner: Any
    tier: Tier

    def choose(self, state: RouteState) -> RouteDecision:
        d = self.inner.choose(state)
        if d.tier is self.tier:
            return d
        reasoning = bool(getattr(self.inner, "deep_reasoning", True)) if self.tier is Tier.DEEP \
            else bool(getattr(self.inner, "ship_reasoning", False))
        return RouteDecision(tier=self.tier, reasoning=reasoning,
                             reason=f"sous-agent : palier {self.tier.value} demandé à delegate ; {d.reason}")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def force_tier(policy: Any, tier: Tier) -> Any:
    """`policy` du parent avec le palier imposé SOUS son plafond de compte (`LimitedPolicy`) quand il y en a un."""
    from nokker.tenancy.limits import LimitedPolicy

    if isinstance(policy, LimitedPolicy):
        return LimitedPolicy(inner=ForcedTierPolicy(policy.inner, tier), limits=policy.limits)
    return ForcedTierPolicy(policy, tier)


__all__ = ["ForcedTierPolicy", "force_tier"]
