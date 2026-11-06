"""Partages de connecteurs dans le foyer (docs/CONNECTORS-MULTI.md §1, migration 068).

Une ligne `connector_grants` vit dans le locataire du PROPRIÉTAIRE du connecteur. Côté propriétaire (son locataire) :
demande, activation, niveau, retrait, journal. Côté bénéficiaire (`shared_with`, SON locataire) : la politique
`grant_grantee_read` lui ouvre ses lignes ACTIVES tant que lui et le propriétaire sont membres du foyer de la ligne ;
il ne lit jamais le secret (le noyau construit la boîte dans le locataire du propriétaire, nokker/connectors/access.py).

Niveaux : mail `read` | `send` ; agenda `read` | `write`. `level` = accès actif (NULL tant que rien n'est accordé),
`pending_level` + `approval_id` = carte forte en attente chez le propriétaire.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from nokker.connectors.errors import ConnectorError

LEVELS = {"mail": ("read", "send"), "calendar": ("read", "write"), "contacts": ("read", "write"),
          "files": ("read", "write"), "tasks": ("read", "write"), "pages": ("read", "write")}
RANK = {"read": 1, "send": 2, "write": 2}


def check_level(kind: str, level: str) -> str:
    if level not in LEVELS.get(kind, ()):
        raise ConnectorError("bad_level", f"niveau inconnu pour {kind} : {level} (attendus : {', '.join(LEVELS[kind])})")
    return level


@dataclass(frozen=True)
class Grant:
    tenant_id: str  # locataire du propriétaire
    connector_id: str
    owner_account: str
    grantee_account: str
    household_id: str
    household_tenant: str
    kind: str
    level: str | None = None
    granted_at: datetime | None = None
    pending_level: str | None = None
    approval_id: str | None = None

    @property
    def active(self) -> bool:
        return self.level is not None


@dataclass(frozen=True)
class GrantEvent:
    connector_id: str
    owner_account: str
    grantee_account: str
    actor_account: str
    event: str  # granted | revoked | dropped
    level: str | None
    reason: str = ""
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


class GrantStore(Protocol):
    # — côté propriétaire (locataire courant = le sien) —
    def of_owner(self, owner_account: str) -> list[Grant]: ...

    def get(self, connector_id: str, grantee_account: str) -> Grant | None: ...

    def put(self, g: Grant) -> None: ...

    def delete(self, connector_id: str, grantee_account: str) -> Grant | None: ...

    def delete_connector(self, connector_id: str) -> list[Grant]: ...

    def log(self, e: GrantEvent) -> None: ...

    def events(self, owner_account: str) -> list[GrantEvent]: ...

    # — côté bénéficiaire (locataire courant = le sien) —
    def shared_with(self, grantee_account: str) -> list[Grant]: ...


class MemoryGrantStore:
    """Toutes les lignes ensemble, filtrées comme la RLS : `tenant` (locataire courant) rend celui du tour ;
    `members(household_id)` rend les comptes du foyer (la condition de `grant_grantee_read`)."""

    def __init__(self, tenant: Callable[[], str], members: Callable[[str], set[str]] | None = None) -> None:
        # `members` None : pas de contrôle ici (le service revérifie le foyer à chaque lecture)
        self._tenant, self._members = tenant, members
        self._rows: dict[tuple[str, str], Grant] = {}
        self._events: list[tuple[str, GrantEvent]] = []
        self._lock = threading.Lock()

    def _mine(self) -> list[Grant]:
        t = self._tenant()
        return [g for g in self._rows.values() if g.tenant_id == t]

    def of_owner(self, owner_account: str) -> list[Grant]:
        return sorted((g for g in self._mine() if g.owner_account == owner_account),
                      key=lambda g: (g.connector_id, g.grantee_account))

    def get(self, connector_id: str, grantee_account: str) -> Grant | None:
        g = self._rows.get((connector_id, grantee_account))
        return g if g is not None and g.tenant_id == self._tenant() else None

    def put(self, g: Grant) -> None:
        if g.tenant_id != self._tenant():
            raise PermissionError("une ligne s'écrit dans son locataire")
        with self._lock:
            self._rows[(g.connector_id, g.grantee_account)] = g

    def delete(self, connector_id: str, grantee_account: str) -> Grant | None:
        with self._lock:
            g = self.get(connector_id, grantee_account)
            if g is not None:
                del self._rows[(connector_id, grantee_account)]
            return g

    def delete_connector(self, connector_id: str) -> list[Grant]:
        with self._lock:
            gone = [g for g in self._mine() if g.connector_id == connector_id]
            for g in gone:
                del self._rows[(g.connector_id, g.grantee_account)]
            return gone

    def log(self, e: GrantEvent) -> None:
        self._events.append((self._tenant(), e))

    def events(self, owner_account: str) -> list[GrantEvent]:
        t = self._tenant()
        return [e for tt, e in self._events if tt == t and e.owner_account == owner_account]

    def shared_with(self, grantee_account: str) -> list[Grant]:
        out = []
        for g in self._rows.values():
            members = self._members(g.household_id) if self._members is not None else None
            if g.grantee_account == grantee_account and g.active and (
                    members is None or {g.owner_account, grantee_account} <= members):
                out.append(g)
        return sorted(out, key=lambda g: (g.kind, g.granted_at or datetime.min.replace(tzinfo=UTC), g.connector_id))


class PgGrantStore:
    COLS = ("tenant_id, connector_id, owner_account, grantee_account, household_id, household_tenant, kind, level, "
            "granted_at, pending_level, approval_id")

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self, actor: str | None = None):
        from psycopg.rows import dict_row

        from nokker.store.conn import connect

        conn = connect(self.dsn, row_factory=dict_row)
        if actor:
            try:  # la politique du bénéficiaire lit nokker_actor() (059) : compte du locataire courant seulement
                conn.execute("SELECT set_config('app.account_id', %s, false)", (actor,))
                conn.commit()
            except BaseException:
                conn.close()
                raise
        return conn

    @staticmethod
    def _grant(r: dict[str, Any]) -> Grant:
        return Grant(**r)

    def of_owner(self, owner_account: str) -> list[Grant]:
        with self._conn() as conn:
            rows = conn.execute(f"SELECT {self.COLS} FROM connector_grants WHERE owner_account = %s "
                                "AND tenant_id = current_setting('app.tenant_id') ORDER BY connector_id, grantee_account",
                                (owner_account,)).fetchall()
        return [self._grant(r) for r in rows]

    def get(self, connector_id: str, grantee_account: str) -> Grant | None:
        with self._conn() as conn:
            r = conn.execute(f"SELECT {self.COLS} FROM connector_grants WHERE connector_id = %s AND grantee_account = %s "
                             "AND tenant_id = current_setting('app.tenant_id')", (connector_id, grantee_account)).fetchone()
        return self._grant(r) if r else None

    def put(self, g: Grant) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO connector_grants (connector_id, owner_account, grantee_account, household_id, "
                "household_tenant, kind, level, granted_at, pending_level, approval_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (connector_id, grantee_account) DO UPDATE SET level = EXCLUDED.level, "
                "granted_at = EXCLUDED.granted_at, pending_level = EXCLUDED.pending_level, "
                "approval_id = EXCLUDED.approval_id, household_id = EXCLUDED.household_id, "
                "household_tenant = EXCLUDED.household_tenant",
                (g.connector_id, g.owner_account, g.grantee_account, g.household_id, g.household_tenant, g.kind, g.level,
                 g.granted_at, g.pending_level, g.approval_id))

    def delete(self, connector_id: str, grantee_account: str) -> Grant | None:
        with self._conn() as conn:
            r = conn.execute(f"DELETE FROM connector_grants WHERE connector_id = %s AND grantee_account = %s "
                             f"AND tenant_id = current_setting('app.tenant_id') RETURNING {self.COLS}",
                             (connector_id, grantee_account)).fetchone()
        return self._grant(r) if r else None

    def delete_connector(self, connector_id: str) -> list[Grant]:
        with self._conn() as conn:
            rows = conn.execute(f"DELETE FROM connector_grants WHERE connector_id = %s "
                                f"AND tenant_id = current_setting('app.tenant_id') RETURNING {self.COLS}",
                                (connector_id,)).fetchall()
        return [self._grant(r) for r in rows]

    def log(self, e: GrantEvent) -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO connector_grant_events (connector_id, owner_account, grantee_account, actor_account, "
                         "event, level, reason, at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                         (e.connector_id, e.owner_account, e.grantee_account, e.actor_account, e.event, e.level,
                          e.reason[:200], e.at))

    def events(self, owner_account: str) -> list[GrantEvent]:
        with self._conn() as conn:
            rows = conn.execute("SELECT connector_id, owner_account, grantee_account, actor_account, event, level, reason, "
                                "at FROM connector_grant_events WHERE owner_account = %s ORDER BY id",
                                (owner_account,)).fetchall()
        return [GrantEvent(**r) for r in rows]

    def shared_with(self, grantee_account: str) -> list[Grant]:
        with self._conn(actor=grantee_account) as conn:
            rows = conn.execute(f"SELECT {self.COLS} FROM connector_grants WHERE grantee_account = %s AND level IS NOT NULL "
                                "ORDER BY kind, granted_at, connector_id", (grantee_account,)).fetchall()
        return [self._grant(r) for r in rows]


__all__ = ["LEVELS", "RANK", "Grant", "GrantEvent", "GrantStore", "MemoryGrantStore", "PgGrantStore", "check_level",
           "replace"]
