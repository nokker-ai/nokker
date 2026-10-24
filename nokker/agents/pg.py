"""`PgAgents` — agents en Postgres (psycopg 3), table de `migrations/0001_agents.sql`."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from nokker.agents.base import BaseAgents
from nokker.agents.types import Agent
from nokker.store.conn import TenantConn, migrate

MIGRATION_SQL = (Path(__file__).parent / "migrations" / "0001_agents.sql").read_text(encoding="utf-8")

_COLS = "id, name, role, model, tools, status, created_by, created_at, updated_at"


def _row(r: tuple) -> Agent:
    aid, name, role, model, tools, status, created_by, created_at, updated_at = r
    return Agent(id=aid, name=name, role=role, model=model, tools=list(tools or []), status=status,
                 created_by=created_by, created_at=created_at, updated_at=updated_at)


@dataclass
class PgAgents(BaseAgents):
    dsn: str = ""

    def __post_init__(self) -> None:
        if not self.dsn:
            raise ValueError("PgAgents requiert un DSN Postgres")
        migrate(self.dsn, MIGRATION_SQL)  # en propriétaire, puis la RLS (nokker/store/conn.py)
        self._db = TenantConn(self.dsn)
        self._lock = threading.Lock()

    @property
    def _conn(self) -> Any:
        return self._db.get()  # app.tenant_id = locataire courant, reposé à chaque usage (NoTenant hors scope)

    def close(self) -> None:
        self._db.close()

    def _load(self, agent_id: str) -> Agent | None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(f"SELECT {_COLS} FROM nokker_agents WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        return _row(row) if row else None

    def _store(self, a: Agent) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT INTO nokker_agents ({_COLS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, role = EXCLUDED.role, "
                "model = EXCLUDED.model, tools = EXCLUDED.tools, status = EXCLUDED.status, "
                "updated_at = EXCLUDED.updated_at",
                (a.id, a.name, a.role, a.model, Jsonb(a.tools), a.status, a.created_by, a.created_at,
                 a.updated_at))

    def _list_all(self) -> list[Agent]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(f"SELECT {_COLS} FROM nokker_agents")
            rows = cur.fetchall()
        return [_row(r) for r in rows]


__all__ = ["PgAgents"]
