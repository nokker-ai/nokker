"""`InMemoryAgents` — tests et dev (aucune persistance)."""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field

from nokker.agents.base import BaseAgents
from nokker.agents.types import Agent


@dataclass
class InMemoryAgents(BaseAgents):
    _agents: dict[str, Agent] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _load(self, agent_id: str) -> Agent | None:
        with self._lock:
            a = self._agents.get(agent_id)
            return copy.deepcopy(a) if a is not None else None

    def _store(self, agent: Agent) -> None:
        with self._lock:
            self._agents[agent.id] = copy.deepcopy(agent)

    def _list_all(self) -> list[Agent]:
        with self._lock:
            return [copy.deepcopy(a) for a in self._agents.values()]


__all__ = ["InMemoryAgents"]
