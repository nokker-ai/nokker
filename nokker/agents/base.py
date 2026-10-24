"""Stockage des agents : logique commune (`memory.InMemoryAgents`, `pg.PgAgents` ne fournissent que les
primitives) — même découpage que `bubbles/base.py`/`threads/base.py`. Les droits et les événements
(`agent.created`) vivent dans `hub.AgentsHub`, pas ici."""
from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from nokker.agents.types import STATUSES, Agent, AgentError, AgentNotFound


def new_agent_id() -> str:
    return "ag_" + secrets.token_hex(6)


@dataclass
class BaseAgents:
    clock: Callable[[], float] = time.time

    def _load(self, agent_id: str) -> Agent | None:
        raise NotImplementedError

    def _store(self, agent: Agent) -> None:
        raise NotImplementedError

    def _list_all(self) -> list[Agent]:
        raise NotImplementedError

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
               status: str = "armed", created_by: str = "nina") -> Agent:
        if status not in STATUSES:
            raise AgentError(f"status inconnu : {status}")
        now = self.clock()
        a = Agent(id=new_agent_id(), name=(name or "").strip()[:200] or "Agent",
                  role=(role or "").strip()[:200], model=(model or "").strip()[:200],
                  tools=[str(t) for t in (tools or [])][:40], status=status, created_by=created_by,
                  created_at=now, updated_at=now)
        self._store(a)
        return a


__all__ = ["BaseAgents", "new_agent_id"]
