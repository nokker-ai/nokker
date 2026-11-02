"""Magasin du calendrier familial : brouillons (lignes relues, révision) et réglages du compte (personnes du foyer,
fuseau).

Tout est rangé dans le locataire du COMPTE (un compte = un locataire, docs/TENANCY.md) : la RLS de Postgres (migration
060, `tenant_isolation` forcée) fait qu'un foyer ne lit ni ne modifie jamais le brouillon d'un autre. Le brouillon ne
garde PAS le texte du document : seulement les lignes extraites, leur relecture et un libellé d'origine.

Écriture concurrente (outil d'édition et carte web en même temps) : `save(draft, expected)` n'écrit que si la révision
rangée est encore `expected` (comparer-et-poser) ; sinon il rend faux et rien ne change.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from typing import Any

from nokker.tenancy.context import current_tenant

DEFAULT_TZ = "Europe/Zurich"


@dataclass
class Draft:
    draft_id: str
    account_id: str
    created_at: float
    updated_at: float
    revision: int
    status: str  # open | committed
    origin: str  # libellé affiché : « programme.pdf », « image jointe », « pièce jointe texte »
    lines: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)  # après écriture : écrit, échoué, cible, .ics
    timezone: str = DEFAULT_TZ


@dataclass
class Settings:
    account_id: str
    people: list[str] = field(default_factory=list)
    timezone: str = DEFAULT_TZ


class MemoryFamcalStore:
    def __init__(self) -> None:
        self._drafts: dict[str, dict[str, Draft]] = {}
        self._settings: dict[str, dict[str, Settings]] = {}
        self._lock = threading.Lock()

    def _t(self, table: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return table.setdefault(current_tenant(), {})

    def create(self, d: Draft) -> None:
        with self._lock:
            self._t(self._drafts)[d.draft_id] = replace(d, lines=[dict(x) for x in d.lines])

    def get(self, draft_id: str) -> Draft | None:
        with self._lock:
            d = self._t(self._drafts).get(draft_id)
            return replace(d, lines=[dict(x) for x in d.lines], result=dict(d.result)) if d else None

    def save(self, d: Draft, expected: int) -> bool:
        with self._lock:
            cur = self._t(self._drafts).get(d.draft_id)
            if cur is None or cur.revision != expected or cur.account_id != d.account_id:
                return False
            self._t(self._drafts)[d.draft_id] = replace(d, lines=[dict(x) for x in d.lines], result=dict(d.result))
            return True

    def list(self, account_id: str, status: str | None = None) -> list[Draft]:
        with self._lock:
            rows = [d for d in self._t(self._drafts).values() if d.account_id == account_id
                    and (status is None or d.status == status)]
        return sorted(rows, key=lambda d: -d.created_at)

    def settings(self, account_id: str) -> Settings:
        with self._lock:
            s = self._t(self._settings).get(account_id)
            return replace(s, people=list(s.people)) if s else Settings(account_id=account_id)

    def put_settings(self, s: Settings) -> None:
        with self._lock:
            self._t(self._settings)[s.account_id] = replace(s, people=list(s.people))


class PgFamcalStore:
    """Tables `family_calendar_drafts`, `family_calendar_settings` (migration 061), RLS forcée par locataire."""

    _COLS = "draft_id, account_id, created_at, updated_at, revision, status, origin, lines, result, timezone"

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):
        from nokker.store.conn import connect

        return connect(self.dsn)

    @staticmethod
    def _row(r: Any) -> Draft:
        return Draft(draft_id=r[0], account_id=r[1], created_at=float(r[2]), updated_at=float(r[3]),
                     revision=int(r[4]), status=r[5], origin=r[6] or "", lines=list(r[7] or []),
                     result=dict(r[8] or {}), timezone=r[9] or DEFAULT_TZ)

    def create(self, d: Draft) -> None:
        from psycopg.types.json import Jsonb

        with self._conn() as conn:
            conn.execute(f"INSERT INTO family_calendar_drafts ({self._COLS}) VALUES "
                         "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                         (d.draft_id, d.account_id, d.created_at, d.updated_at, d.revision, d.status, d.origin,
                          Jsonb(d.lines), Jsonb(d.result), d.timezone))

    def get(self, draft_id: str) -> Draft | None:
        with self._conn() as conn:
            r = conn.execute(f"SELECT {self._COLS} FROM family_calendar_drafts WHERE draft_id = %s",
                             (draft_id,)).fetchone()
        return self._row(r) if r else None

    def save(self, d: Draft, expected: int) -> bool:
        from psycopg.types.json import Jsonb

        with self._conn() as conn:
            cur = conn.execute("UPDATE family_calendar_drafts SET updated_at = %s, revision = %s, status = %s, "
                               "lines = %s, result = %s WHERE draft_id = %s AND account_id = %s AND revision = %s",
                               (d.updated_at, d.revision, d.status, Jsonb(d.lines), Jsonb(d.result), d.draft_id,
                                d.account_id, expected))
            return cur.rowcount == 1

    def list(self, account_id: str, status: str | None = None) -> list[Draft]:
        q = f"SELECT {self._COLS} FROM family_calendar_drafts WHERE account_id = %s"
        args: list[Any] = [account_id]
        if status:
            q += " AND status = %s"
            args.append(status)
        with self._conn() as conn:
            rows = conn.execute(q + " ORDER BY created_at DESC LIMIT 100", args).fetchall()
        return [self._row(r) for r in rows]

    def settings(self, account_id: str) -> Settings:
        with self._conn() as conn:
            r = conn.execute("SELECT people, timezone FROM family_calendar_settings WHERE account_id = %s",
                             (account_id,)).fetchone()
        if not r:
            return Settings(account_id=account_id)
        return Settings(account_id=account_id, people=list(r[0] or []), timezone=r[1] or DEFAULT_TZ)

    def put_settings(self, s: Settings) -> None:
        from psycopg.types.json import Jsonb

        with self._conn() as conn:
            conn.execute("INSERT INTO family_calendar_settings (account_id, people, timezone) VALUES (%s, %s, %s) "
                         "ON CONFLICT (tenant_id, account_id) DO UPDATE SET people = EXCLUDED.people, "
                         "timezone = EXCLUDED.timezone", (s.account_id, Jsonb(s.people), s.timezone))


__all__ = ["DEFAULT_TZ", "Draft", "MemoryFamcalStore", "PgFamcalStore", "Settings"]
