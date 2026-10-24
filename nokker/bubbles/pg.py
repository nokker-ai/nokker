"""`PgBubbles` — bulles en Postgres (psycopg 3), table de `migrations/0001_bubbles.sql`.

`version` : l'UPDATE est conditionné à la version lue (`WHERE version = <lue>`), pour qu'un `if_version`
reste vrai même avec deux processus qui écrivent la même bulle (sinon 409 côté `BaseBubbles.update`)."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from nokker.bubbles.base import BaseBubbles
from nokker.bubbles.types import Bubble, VersionConflict
from nokker.store.conn import TenantConn, known_tenants, migrate

MIGRATION_SQL = (Path(__file__).parent / "migrations" / "0001_bubbles.sql").read_text(encoding="utf-8")

_COLS = ("id, thread_id, kind, title, status, state, actions, origin, shared_members, shared_threads, "
         "created_by, created_at, updated_at, closed_at, version")


def _row(r: tuple) -> Bubble:
    (bid, thread_id, kind, title, status, state, actions, origin, sm, st, created_by, created_at, updated_at,
     closed_at, version) = r
    return Bubble(id=bid, thread_id=thread_id, kind=kind, title=title, status=status, state=dict(state or {}),
                  actions=list(actions or []), origin=origin, shared_members=list(sm or []),
                  shared_threads=list(st or []), created_by=created_by, created_at=created_at,
                  updated_at=updated_at, closed_at=closed_at, version=version)


@dataclass
class PgBubbles(BaseBubbles):
    dsn: str = ""

    def __post_init__(self) -> None:
        if not self.dsn:
            raise ValueError("PgBubbles requiert un DSN Postgres")
        migrate(self.dsn, MIGRATION_SQL)  # en propriétaire, puis la RLS (nokker/store/conn.py)
        self._db = TenantConn(self.dsn)
        self._lock = threading.Lock()

    @property
    def _conn(self) -> Any:
        return self._db.get()  # app.tenant_id = locataire courant, reposé à chaque usage (NoTenant hors scope)

    def close(self) -> None:
        self._db.close()

    def tenants(self) -> list[str]:
        """Locataires que parcourt le balayeur de fond (`BubbleHub.sweep_expired` hors requête)."""
        return known_tenants(self.dsn)

    def _load(self, bubble_id: str) -> Bubble | None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(f"SELECT {_COLS} FROM nokker_bubbles WHERE id = %s", (bubble_id,))
            row = cur.fetchone()
        return _row(row) if row else None

    def _store(self, b: Bubble) -> None:
        vals = (b.id, b.thread_id, b.kind, b.title, b.status, Jsonb(b.state), Jsonb(b.actions),
                Jsonb(b.origin) if b.origin is not None else None, Jsonb(b.shared_members),
                Jsonb(b.shared_threads), b.created_by, b.created_at, b.updated_at, b.closed_at, b.version)
        with self._lock, self._conn.cursor() as cur:
            if b.version == 1:
                cur.execute(f"INSERT INTO nokker_bubbles ({_COLS}) VALUES "
                            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                            "ON CONFLICT (id) DO NOTHING", vals)
                return
            cur.execute(
                "UPDATE nokker_bubbles SET thread_id=%s, kind=%s, title=%s, status=%s, state=%s, actions=%s, "
                "origin=%s, shared_members=%s, shared_threads=%s, created_by=%s, created_at=%s, updated_at=%s, "
                "closed_at=%s, version=%s WHERE id=%s AND version=%s",
                (*vals[1:], vals[0], b.version - 1))
            if cur.rowcount == 0:
                cur.execute("SELECT version FROM nokker_bubbles WHERE id = %s", (b.id,))
                row = cur.fetchone()
                raise VersionConflict(row[0] if row else 0)

    def _list(self, *, thread_id=None, shared_with_thread=None, kind=None, open_only=False) -> list[Bubble]:
        where, args = [], []
        if thread_id is not None:
            where.append("thread_id = %s")
            args.append(thread_id)
        if shared_with_thread is not None:
            where.append("shared_threads ? %s")
            args.append(shared_with_thread)
        if kind is not None:
            where.append("kind = %s")
            args.append(kind)
        if open_only:
            where.append("closed_at IS NULL")
        sql = f"SELECT {_COLS} FROM nokker_bubbles"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at"
        with self._lock, self._conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        return [_row(r) for r in rows]


__all__ = ["PgBubbles"]
