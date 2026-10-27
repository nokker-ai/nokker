"""Commande d'opérateur des foyers autorisés à déclarer des profils mineurs (docs/LOKKAL.md §7.29.4).

`python -m nokker household minors allow|revoke|list (--email E | --account ID)` : à blanc par défaut, `--apply` pour
agir. L'autorisation vit en base (`household_minor_grants`, migration 043), jamais dans un fichier. Chaque
autorisation et chaque retrait laissent une ligne d'audit en ajout seul. Aucune sortie ne contient l'adresse ni
l'identifiant du compte : le foyer est désigné par son code court `mg-xxxxxx`. Un retrait SUSPEND les profils mineurs
déjà déclarés (aucun tour), il ne supprime rien : l'effacement passe par `nokker erase`."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class GrantError(Exception):
    """Refus de l'outil d'opérateur (compte introuvable, sélecteur absent, base non configurée)."""


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


@dataclass
class MinorGrants:
    """`store` : `MemoryHouseholdStore` ou `PgHouseholdStore` (méthodes grant_*). `lookup(email, account_id)` rend
    `(account_id, tenant_id)` ou None."""

    store: Any
    lookup: Callable[[str | None, str | None], tuple[str, str] | None]
    clock: Callable[[], float] = field(default=time.time)

    def _target(self, email: str | None, account: str | None) -> tuple[str, str]:
        if bool(email) == bool(account):
            raise GrantError("indiquer --email ou --account (un seul)")
        found = self.lookup((email or "").strip().lower() or None, account)
        if found is None:
            raise GrantError("aucun compte ne correspond")
        return found

    def allow(self, *, email: str | None = None, account: str | None = None, apply: bool = False) -> dict[str, Any]:
        acc, tenant = self._target(email, account)
        cur = self.store.minor_grant(acc)
        state = "already" if cur is not None and cur["revoked_at"] is None else "restored" if cur else "new"
        if not apply or state == "already":
            return {"applied": False, "state": state, "code": cur["code"] if cur else None}
        row, state = self.store.grant_allow(acc, tenant, self.clock())
        return {"applied": True, "state": state, "code": row["code"], "at": _iso(self.clock())}

    def revoke(self, *, email: str | None = None, account: str | None = None, apply: bool = False) -> dict[str, Any]:
        acc, _ = self._target(email, account)
        cur = self.store.minor_grant(acc)
        if cur is None:
            return {"applied": False, "state": "none", "code": None, "suspended": 0}
        if cur["revoked_at"] is not None:
            return {"applied": False, "state": "already", "code": cur["code"], "suspended": 0}
        if not apply:
            return {"applied": False, "state": "active", "code": cur["code"],
                    "suspended": self.store.grant_minors(acc)}
        row, n = self.store.grant_revoke(acc, self.clock())
        return {"applied": True, "state": "revoked", "code": row["code"], "suspended": n, "at": _iso(self.clock())}

    def listing(self, *, email: str | None = None, account: str | None = None) -> list[dict[str, Any]]:
        only = self._target(email, account)[0] if (email or account) else None
        rows = self.store.grant_list()
        return [{"code": r["code"], "state": "retirée" if r["revoked_at"] else "autorisée",
                 "granted": _iso(r["granted_at"]), "revoked": _iso(r["revoked_at"]),
                 "minors": self.store.grant_minors(r["account_id"])}
                for r in rows if only is None or r["account_id"] == only]

    @staticmethod
    def audit_line(out: dict[str, Any], action: str) -> dict[str, Any]:
        return {"event": f"household_minors_{action}", "code": out["code"], "at": out["at"],
                **({"suspended": out["suspended"]} if action == "revoke" else {})}


def open_operator(config: Any) -> MinorGrants:
    """La commande d'opérateur sur la base de la configuration (même DSN que `nokker erase`)."""
    dsn = config.pg_dsn()
    if not dsn:
        raise GrantError("aucun DSN Postgres configuré")
    import psycopg

    from nokker.household.store import PgHouseholdStore

    def lookup(email: str | None, account: str | None) -> tuple[str, str] | None:
        with psycopg.connect(dsn) as c:
            if account:
                r = c.execute("SELECT id, tenant_id FROM accounts WHERE id = %s", (account,)).fetchone()
            else:
                r = c.execute("SELECT id, tenant_id FROM accounts WHERE email = %s", (email,)).fetchone()
        return (r[0], r[1]) if r else None

    return MinorGrants(store=PgHouseholdStore(dsn, migrate_now=False), lookup=lookup)


__all__ = ["GrantError", "MinorGrants", "open_operator"]
