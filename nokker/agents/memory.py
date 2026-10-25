"""`InMemoryAgents` — tests et dev (aucune persistance)."""
from __future__ import annotations

import copy
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field

from nokker.agents.base import BaseAgents
from nokker.agents.types import Agent, AgentRun


@dataclass
class InMemoryAgents(BaseAgents):
    _agents: dict[str, Agent] = field(default_factory=dict)
    _runs: dict[str, AgentRun] = field(default_factory=dict)
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

    def _delete(self, agent_id: str) -> None:
        with self._lock:
            self._agents.pop(agent_id, None)

    def _load_run(self, run_id: str) -> AgentRun | None:
        with self._lock:
            r = self._runs.get(run_id)
            return copy.deepcopy(r) if r is not None else None

    def _store_run(self, run: AgentRun) -> None:
        with self._lock:
            self._runs[run.id] = copy.deepcopy(run)

    def _runs_of(self, agent_id: str) -> list[AgentRun]:
        with self._lock:
            return [copy.deepcopy(r) for r in self._runs.values() if r.agent_id == agent_id]

    def _runs_with_status(self, statuses: Iterable[str]) -> list[AgentRun]:
        wanted = set(statuses)
        with self._lock:
            return [copy.deepcopy(r) for r in self._runs.values() if r.status in wanted]

    def _run_by_thread(self, thread_id: str) -> AgentRun | None:
        with self._lock:
            r = next((r for r in self._runs.values() if r.thread_id == thread_id), None)
            return copy.deepcopy(r) if r is not None else None

    def _delete_runs(self, agent_id: str) -> None:
        with self._lock:
            for rid in [k for k, r in self._runs.items() if r.agent_id == agent_id]:
                del self._runs[rid]


__all__ = ["InMemoryAgents"]
