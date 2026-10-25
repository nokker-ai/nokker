"""`PgThreads` — fils en Postgres (psycopg 3), tables de `migrations/0001_threads.sql`."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from nokker.store.conn import TenantConn, migrate
from nokker.threads.base import BaseThreads
from nokker.threads.types import Thread

MIGRATION_SQL = (Path(__file__).parent / "migrations" / "0001_threads.sql").read_text(encoding="utf-8")
# Colonne `origin` des fils de passage d'agent (lot X14) : posée par la 024, rejouée ici (idempotente).
ENGINE_SQL = (Path(__file__).parent.parent / "store" / "migrations" / "024_agent_engine.sql").read_text(
    encoding="utf-8")

_COLS = "id, title, owner, members, created_at, updated_at, working, last_snippet, origin"


def _row(row: tuple) -> Thread:
    tid, title, owner, members, created_at, updated_at, working, last_snippet, origin = row
    return Thread(id=tid, title=title, owner=owner, members=list(members or []), created_at=created_at,
                  updated_at=updated_at, working=working, last_snippet=last_snippet or "",
                  origin=dict(origin) if origin else None)


@dataclass
class PgThreads(BaseThreads):
    dsn: str = ""

    def __post_init__(self) -> None:
        if not self.dsn:
            raise ValueError("PgThreads requiert un DSN Postgres")
        migrate(self.dsn, MIGRATION_SQL, ENGINE_SQL)  # en propriétaire, puis la RLS (nokker/store/conn.py)
        self._db = TenantConn(self.dsn)
        self._lock = threading.Lock()

    @property
    def _conn(self) -> Any:
        return self._db.get()  # app.tenant_id = locataire courant, reposé à chaque usage (NoTenant hors scope)

    def close(self) -> None:
        self._db.close()

    def _load(self, thread_id: str) -> Thread | None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(f"SELECT {_COLS} FROM nokker_threads WHERE id = %s", (thread_id,))
            row = cur.fetchone()
        return _row(row) if row else None

    def _store(self, t: Thread) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT INTO nokker_threads ({_COLS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, owner = EXCLUDED.owner, "
                "members = EXCLUDED.members, updated_at = EXCLUDED.updated_at, working = EXCLUDED.working, "
                "last_snippet = EXCLUDED.last_snippet",
                (t.id, t.title, t.owner, Jsonb(t.members), t.created_at, t.updated_at, t.working, t.last_snippet,
                 Jsonb(t.origin) if t.origin else None))

    def _list_all(self) -> list[Thread]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(f"SELECT {_COLS} FROM nokker_threads")
            rows = cur.fetchall()
        return [_row(r) for r in rows]

    def _delete(self, thread_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM nokker_thread_inbox WHERE thread_id = %s", (thread_id,))
            self._conn.execute("DELETE FROM nokker_threads WHERE id = %s", (thread_id,))

    def _push(self, thread_id: str, block: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO nokker_thread_inbox (thread_id, block) VALUES (%s, %s)",
                               (thread_id, block))

    def _drain(self, thread_id: str) -> list[str]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute("UPDATE nokker_thread_inbox SET consumed_at = now() WHERE thread_id = %s "
                        "AND consumed_at IS NULL RETURNING id, block", (thread_id,))
            rows = cur.fetchall()
        return [b for _, b in sorted(rows)]


__all__ = ["PgThreads"]
