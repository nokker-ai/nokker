"""Backends en mémoire de `EpisodeStore` et `ReservoirStore` (tests, dev, CLI locale). Même contrat que les backends
Postgres (`nokker/context/pg.py`) : un test de contrat les parcourt tous les deux."""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field

from nokker.context.types import DaySummary, Episode, ReservoirItem, ViewState


@dataclass
class MemoryEpisodeStore:
    _episodes: dict[str, Episode] = field(default_factory=dict)
    _days: dict[tuple[str, str], DaySummary] = field(default_factory=dict)
    _states: dict[str, ViewState] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def current(self, thread_id: str) -> Episode | None:
        with self._lock:
            opened = [e for e in self._episodes.values() if e.thread_id == thread_id and e.is_open]
            return copy.deepcopy(max(opened, key=lambda e: e.seq)) if opened else None

    def get(self, episode_id: str) -> Episode | None:
        with self._lock:
            e = self._episodes.get(episode_id)
            return copy.deepcopy(e) if e else None

    def list(self, thread_id: str, *, closed_only: bool = False) -> list[Episode]:
        with self._lock:
            items = [e for e in self._episodes.values() if e.thread_id == thread_id and (e.closed_at is not None or
                                                                                          not closed_only)]
            return [copy.deepcopy(e) for e in sorted(items, key=lambda e: e.seq)]

    def save(self, episode: Episode) -> None:
        with self._lock:
            if episode.id in self._episodes:
                episode.version = self._episodes[episode.id].version + 1
            self._episodes[episode.id] = copy.deepcopy(episode)

    def next_seq(self, thread_id: str) -> int:
        with self._lock:
            seqs = [e.seq for e in self._episodes.values() if e.thread_id == thread_id]
            return (max(seqs) + 1) if seqs else 0

    def get_day(self, thread_id: str, day: str) -> DaySummary | None:
        with self._lock:
            d = self._days.get((thread_id, day))
            return copy.deepcopy(d) if d else None

    def list_days(self, thread_id: str) -> list[DaySummary]:
        with self._lock:
            return [copy.deepcopy(d) for (t, _), d in sorted(self._days.items()) if t == thread_id]

    def save_day(self, day: DaySummary) -> None:
        with self._lock:
            self._days[(day.thread_id, day.day)] = copy.deepcopy(day)

    def threads_with_open_episode(self) -> list[str]:
        with self._lock:
            return sorted({e.thread_id for e in self._episodes.values() if e.is_open})

    def get_state(self, thread_id: str) -> ViewState | None:
        with self._lock:
            s = self._states.get(thread_id)
            return copy.deepcopy(s) if s else None

    def save_state(self, state: ViewState) -> None:
        with self._lock:
            self._states[state.thread_id] = copy.deepcopy(state)


@dataclass
class MemoryReservoirStore:
    _items: dict[str, ReservoirItem] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def put(self, item: ReservoirItem) -> None:
        with self._lock:
            self._items[item.id] = copy.deepcopy(item)

    def get(self, item_id: str) -> ReservoirItem | None:
        with self._lock:
            it = self._items.get(item_id)
            return copy.deepcopy(it) if it else None

    def list(self, thread_id: str, *, kind: str | None = None, limit: int = 200) -> list[ReservoirItem]:
        with self._lock:
            items = [i for i in self._items.values() if i.thread_id == thread_id and (kind is None or i.kind == kind)]
            items.sort(key=lambda i: (i.created_at, i.id))
            return [copy.deepcopy(i) for i in items[-limit:]]

    def find(self, thread_id: str, turn_index: int, call_id: str) -> ReservoirItem | None:
        with self._lock:
            for i in self._items.values():
                if i.thread_id == thread_id and i.turn_index == turn_index and i.call_id == call_id:
                    return copy.deepcopy(i)
            return None


__all__ = ["MemoryEpisodeStore", "MemoryReservoirStore"]
