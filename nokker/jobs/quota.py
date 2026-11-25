"""Débit au quota Nokker des jetons consommés chez le service emploi (docs/JOBS-PACK.md §11.1, garde-fou 4).

Décision du 05.11 : même grille que les tours du Router. Le quota Nokker compte des TOURS (`TenantLimits`, compteurs
`turns` et `week_turns`) ; les jetons rendus par chaque réponse du service sont convertis en tours entiers
(`tokens_per_turn` jetons = 1 tour, arrondi au supérieur) et ajoutés aux mêmes compteurs que les tours de la personne.
Le locataire interne n'est jamais débité (comme `check_turn`).
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Protocol

from nokker.jobs.client import Usage

DEFAULT_TOKENS_PER_TURN = 12_000


class JobsQuota(Protocol):
    def available(self, sender: str) -> bool: ...

    def debit(self, sender: str, usage: Usage) -> int: ...


class NoQuota:
    """Sans plafonds configurés (dev, tests) : tout passe, rien n'est compté."""

    def available(self, sender: str) -> bool:
        return True

    def debit(self, sender: str, usage: Usage) -> int:
        return 0


class TurnGridQuota:
    def __init__(self, limits: Any, tokens_per_turn: int = DEFAULT_TOKENS_PER_TURN) -> None:
        self.limits, self.tokens_per_turn = limits, max(1, int(tokens_per_turn))

    def units(self, usage: Usage) -> int:
        return math.ceil(usage.total / self.tokens_per_turn) if usage.total > 0 else 0

    def available(self, sender: str) -> bool:
        from nokker.tenancy.context import INTERNAL_TENANT, current_tenant

        if current_tenant() == INTERNAL_TENANT:
            return True
        u = self.limits.usage(sender)
        day, week = u["today"]["turns"], u["week"]["turns"]
        caps = [c for c in (day.get("cap"), day.get("hard_cap")) if c is not None]
        if caps and day["used"] >= min(caps):
            return False
        return not (week.get("cap") is not None and u.get("mode") == "hard" and week["used"] >= week["cap"])

    def debit(self, sender: str, usage: Usage) -> int:
        from nokker.tenancy.context import INTERNAL_TENANT, current_tenant
        from nokker.tenancy.limits import day_window, week_window

        tid = current_tenant()
        n = self.units(usage)
        if n == 0 or tid == INTERNAL_TENANT:
            return 0
        eff = self.limits._for(tid) if hasattr(self.limits, "_for") else self.limits
        clock = getattr(eff, "clock", None)
        now = clock() if callable(clock) else datetime.now(UTC)
        counter = eff.counter
        counter.add(tid, sender, day_window(now)[0].date(), "turns", n)
        counter.add(tid, sender, week_window(now)[0].date(), "week_turns", n)
        return n


__all__ = ["DEFAULT_TOKENS_PER_TURN", "JobsQuota", "NoQuota", "TurnGridQuota"]
