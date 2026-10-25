"""`PgAgents` — agents et passages en Postgres (psycopg 3) : `migrations/0001_agents.sql` puis
`nokker/store/migrations/024_agent_engine.sql` (champs du moteur, table `nokker_agent_runs` sous RLS)."""
from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from nokker.agents.base import BaseAgents
from nokker.agents.types import Agent, AgentRun
from nokker.store.conn import TenantConn, migrate

MIGRATION_SQL = (Path(__file__).parent / "migrations" / "0001_agents.sql").read_text(encoding="utf-8")
PROJECT_SQL = (Path(__file__).parent / "migrations" / "0002_agent_project.sql").read_text(encoding="utf-8")
ENGINE_SQL = (Path(__file__).parent.parent / "store" / "migrations" / "024_agent_engine.sql").read_text(
    encoding="utf-8")
PLACEMENT_SQL = (Path(__file__).parent.parent / "store" / "migrations" / "028_deck_placement.sql").read_text(
    encoding="utf-8")
PACKS_SQL = (Path(__file__).parent.parent / "store" / "migrations" / "036_packs_web.sql").read_text(encoding="utf-8")

_COLS = ("id, name, role, model, tools, status, created_by, created_at, updated_at, instructions, trigger, "
         "next_run_at, last_run, secrets, budget, auto_approve, placement, project, armed, approved_rules, "
         "arm_approval_id, webhook_key, needs, origin")
_JSON_AGENT = {"tools", "trigger", "last_run", "secrets", "budget", "auto_approve", "placement", "needs", "origin"}
_RUN_COLS = ("id, agent_id, trigger, status, scheduled_for, started_at, ended_at, thread_id, cost, summary, "
             "catch_up, event, input, approvals, created_at, machine_id, guest")
_JSON_RUN = {"cost", "approvals", "guest"}


def _names(cols: str) -> list[str]:
    return [c.strip() for c in cols.split(",")]


def _row(r: tuple) -> Agent:
    d = dict(zip(_names(_COLS), r, strict=True))
    d["tools"] = list(d["tools"] or [])
    d["secrets"] = list(d["secrets"] or [])
    d["auto_approve"] = list(d["auto_approve"] or [])
    d["trigger"] = dict(d["trigger"] or {"kind": "manual"})
    return Agent(**d)


def _run_row(r: tuple) -> AgentRun:
    d = dict(zip(_names(_RUN_COLS), r, strict=True))
    d["approvals"] = list(d["approvals"] or [])
    return AgentRun(**d)


def _vals(obj: Any, cols: str, json_cols: set[str]) -> list[Any]:
    out = []
    for c in _names(cols):
        v = getattr(obj, c)
        out.append(Jsonb(v) if c in json_cols and v is not None else v)
    return out


def _upsert(table: str, cols: str) -> str:
    names = _names(cols)
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in names if c not in ("id", "created_at", "created_by"))
    return (f"INSERT INTO {table} ({cols}) VALUES ({', '.join(['%s'] * len(names))}) "
            f"ON CONFLICT (id) DO UPDATE SET {sets}")


@dataclass
class PgAgents(BaseAgents):
    dsn: str = ""

    def __post_init__(self) -> None:
        if not self.dsn:
            raise ValueError("PgAgents requiert un DSN Postgres")
        # en propriétaire, puis la RLS (nokker/store/conn.py)
        migrate(self.dsn, MIGRATION_SQL, PROJECT_SQL, ENGINE_SQL, PLACEMENT_SQL, PACKS_SQL)
        self._db = TenantConn(self.dsn)
        self._lock = threading.Lock()

    @property
    def _conn(self) -> Any:
        return self._db.get()  # app.tenant_id = locataire courant, reposé à chaque usage (NoTenant hors scope)

    def close(self) -> None:
        self._db.close()

    def _one(self, sql: str, args: tuple) -> tuple | None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()

    def _exec(self, sql: str, args: tuple | list) -> None:
        with self._lock:
            self._conn.execute(sql, args)

    # agents
    def _load(self, agent_id: str) -> Agent | None:
        row = self._one(f"SELECT {_COLS} FROM nokker_agents WHERE id = %s", (agent_id,))
        return _row(row) if row else None

    def _store(self, a: Agent) -> None:
        self._exec(_upsert("nokker_agents", _COLS), _vals(a, _COLS, _JSON_AGENT))

    def _list_all(self) -> list[Agent]:
        return [_row(r) for r in self._all(f"SELECT {_COLS} FROM nokker_agents")]

    def _delete(self, agent_id: str) -> None:
        self._exec("DELETE FROM nokker_agents WHERE id = %s", (agent_id,))

    # passages
    def _load_run(self, run_id: str) -> AgentRun | None:
        row = self._one(f"SELECT {_RUN_COLS} FROM nokker_agent_runs WHERE id = %s", (run_id,))
        return _run_row(row) if row else None

    def _store_run(self, run: AgentRun) -> None:
        self._exec(_upsert("nokker_agent_runs", _RUN_COLS), _vals(run, _RUN_COLS, _JSON_RUN))

    def _runs_of(self, agent_id: str) -> list[AgentRun]:
        return [_run_row(r) for r in self._all(f"SELECT {_RUN_COLS} FROM nokker_agent_runs WHERE agent_id = %s",
                                               (agent_id,))]

    def _runs_with_status(self, statuses: Iterable[str]) -> list[AgentRun]:
        return [_run_row(r) for r in self._all(f"SELECT {_RUN_COLS} FROM nokker_agent_runs WHERE status = ANY(%s)",
                                               (list(statuses),))]

    def _run_by_thread(self, thread_id: str) -> AgentRun | None:
        row = self._one(f"SELECT {_RUN_COLS} FROM nokker_agent_runs WHERE thread_id = %s LIMIT 1", (thread_id,))
        return _run_row(row) if row else None

    def _delete_runs(self, agent_id: str) -> None:
        self._exec("DELETE FROM nokker_agent_runs WHERE agent_id = %s", (agent_id,))


__all__ = ["PgAgents"]
