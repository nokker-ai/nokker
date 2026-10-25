"""Stockage des agents et de leurs passages : logique commune (`memory.InMemoryAgents`, `pg.PgAgents` ne fournissent
que les primitives) — même découpage que `bubbles/base.py`/`threads/base.py`. Les droits, la validation et les
événements (`agent.*`) vivent dans `hub.AgentsHub` ; l'exécution dans `engine.AgentEngine`."""
from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from nokker.agents.types import LIVE_RUN, STATUSES, Agent, AgentError, AgentNotFound, AgentRun


def new_agent_id() -> str:
    return "ag_" + secrets.token_hex(6)


def new_run_id() -> str:
    return "ar_" + secrets.token_hex(8)


@dataclass
class BaseAgents:
    clock: Callable[[], float] = time.time

    # ─────────── primitives (sous-classe) ───────────
    def _load(self, agent_id: str) -> Agent | None:
        raise NotImplementedError

    def _store(self, agent: Agent) -> None:
        raise NotImplementedError

    def _list_all(self) -> list[Agent]:
        raise NotImplementedError

    def _delete(self, agent_id: str) -> None:
        raise NotImplementedError

    def _load_run(self, run_id: str) -> AgentRun | None:
        raise NotImplementedError

    def _store_run(self, run: AgentRun) -> None:
        raise NotImplementedError

    def _runs_of(self, agent_id: str) -> list[AgentRun]:
        raise NotImplementedError

    def _runs_with_status(self, statuses: Iterable[str]) -> list[AgentRun]:
        raise NotImplementedError

    def _run_by_thread(self, thread_id: str) -> AgentRun | None:
        raise NotImplementedError

    def _delete_runs(self, agent_id: str) -> None:
        raise NotImplementedError

    # ─────────── agents ───────────
    def get(self, agent_id: str) -> Agent | None:
        return self._load(agent_id)

    def require(self, agent_id: str) -> Agent:
        a = self._load(agent_id)
        if a is None:
            raise AgentNotFound(agent_id)
        return a

    def list_all(self) -> list[Agent]:
        items = self._list_all()
        items.sort(key=lambda a: a.created_at)
        return items

    def create(self, *, name: str, role: str, model: str, tools: list[str] | None = None,
               status: str = "armed", created_by: str = "nina", **fields) -> Agent:
        if status not in STATUSES:
            raise AgentError(f"status inconnu : {status}")
        now = self.clock()
        a = Agent(id=new_agent_id(), name=(name or "").strip()[:200] or "Agent",
                  role=(role or "").strip()[:200], model=(model or "").strip()[:200],
                  tools=[str(t) for t in (tools or [])][:40], status=status, created_by=created_by,
                  created_at=now, updated_at=now, **fields)
        self._store(a)
        return a

    def save(self, agent: Agent, *, touch: bool = True) -> Agent:
        if touch:
            agent = replace(agent, updated_at=self.clock())
        self._store(agent)
        return agent

    def delete(self, agent_id: str) -> None:
        self.require(agent_id)
        self._delete_runs(agent_id)
        self._delete(agent_id)

    # ─────────── passages ───────────
    def new_run(self, agent_id: str, trigger: str, *, run_id: str | None = None, **fields) -> AgentRun:
        """`run_id` : id choisi d'avance par l'appelant (relève du courrier : l'intention rangée avant la mise en file)."""
        run = AgentRun(id=run_id or new_run_id(), agent_id=agent_id, trigger=trigger, created_at=self.clock(),
                       **fields)
        self._store_run(run)
        return run

    def save_run(self, run: AgentRun) -> AgentRun:
        self._store_run(run)
        return run

    def get_run(self, run_id: str) -> AgentRun | None:
        return self._load_run(run_id)

    def runs(self, agent_id: str, *, limit: int = 20, before: str | None = None) -> tuple[list[AgentRun], str | None]:
        """Plus récents d'abord ; `before` = id d'un passage (exclu). Rend (page, id à passer en `before` ou None)."""
        items = sorted(self._runs_of(agent_id), key=lambda r: (r.created_at, r.id), reverse=True)
        if before:
            ids = [r.id for r in items]
            if before not in ids:
                raise AgentNotFound(f"passage {before}")
            items = items[ids.index(before) + 1:]
        limit = max(1, min(100, int(limit)))
        page = items[:limit]
        return page, (page[-1].id if len(items) > limit else None)

    def live_run(self, agent_id: str) -> AgentRun | None:
        live = [r for r in self._runs_of(agent_id) if r.status in LIVE_RUN]
        return max(live, key=lambda r: r.created_at) if live else None

    def runs_with_status(self, *statuses: str) -> list[AgentRun]:
        return sorted(self._runs_with_status(statuses), key=lambda r: r.created_at)

    def run_by_thread(self, thread_id: str) -> AgentRun | None:
        return self._run_by_thread(thread_id)


__all__ = ["BaseAgents", "new_agent_id", "new_run_id"]
