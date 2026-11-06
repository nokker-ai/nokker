"""Le foyer vu par les connecteurs (docs/CONNECTORS-MULTI.md) : foyer d'un compte, ses membres et leurs locataires.

`HouseholdLink(service, tenancy)` adapte `nokker/household/service.py::Household` (résolu à chaque appel : le foyer peut
être branché après les connecteurs, ou éteint). Lu dans le locataire courant, celui du compte demandé."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from nokker.connectors.service import HouseholdInfo, Member

logger = logging.getLogger("nokker.connectors")


class HouseholdLink:
    def __init__(self, service: Callable[[], Any], tenancy: Any) -> None:
        self._service, self._tenancy = service, tenancy

    def _member(self, r: Any) -> Member:
        acc = self._tenancy.account(r.account_id) if self._tenancy is not None else None
        return Member(account_id=r.account_id, email=r.email or (acc.email if acc else ""),
                      tenant_id=acc.tenant_id if acc else "", minor=bool(getattr(r, "minor", False)))

    def of(self, account_id: str) -> HouseholdInfo | None:
        hh = self._service()
        if hh is None or not account_id:
            return None
        try:
            found = hh.membership(account_id)
            if found is None:
                return None
            acc, p, _ = found
            rows = hh.store.hh_members(acc, p.id)
        except Exception:  # noqa: BLE001 — foyer illisible : aucun partage (fermé par défaut)
            logger.warning("connecteurs : foyer de %s illisible", account_id, exc_info=True)
            return None
        return HouseholdInfo(household_id=p.id, household_tenant=p.tenant_id,
                             members=tuple(self._member(r) for r in rows))

    def members(self, rows: list[Any]) -> list[Member]:
        return [self._member(r) for r in rows]


__all__ = ["HouseholdLink"]
