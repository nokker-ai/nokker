"""`InMemoryThreads` — tests et dev (aucune persistance)."""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field

from nokker.threads.base import BaseThreads
from nokker.threads.types import Thread


@dataclass
class InMemoryThreads(BaseThreads):
    _threads: dict[str, Thread] = field(default_factory=dict)
    _inbox: dict[str, list[str]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _load(self, thread_id: str) -> Thread | None:
        with self._lock:
            t = self._threads.get(thread_id)
            return copy.deepcopy(t) if t is not None else None

    def _store(self, thread: Thread) -> None:
        with self._lock:
            self._threads[thread.id] = copy.deepcopy(thread)

    def _list_all(self) -> list[Thread]:
        with self._lock:
            return [copy.deepcopy(t) for t in self._threads.values()]

    def _delete(self, thread_id: str) -> None:
        with self._lock:
            self._threads.pop(thread_id, None)
            self._inbox.pop(thread_id, None)

    def _push(self, thread_id: str, block: str) -> None:
        with self._lock:
            self._inbox.setdefault(thread_id, []).append(block)

    def _drain(self, thread_id: str) -> list[str]:
        with self._lock:
            return self._inbox.pop(thread_id, [])


__all__ = ["InMemoryThreads"]
