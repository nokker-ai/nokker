"""`InMemoryBubbles` — tests et dev."""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field

from nokker.bubbles.base import BaseBubbles
from nokker.bubbles.types import Bubble


@dataclass
class InMemoryBubbles(BaseBubbles):
    _bubbles: dict[str, Bubble] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _load(self, bubble_id: str) -> Bubble | None:
        with self._lock:
            b = self._bubbles.get(bubble_id)
            return copy.deepcopy(b) if b is not None else None

    def _store(self, bubble: Bubble) -> None:
        with self._lock:
            self._bubbles[bubble.id] = copy.deepcopy(bubble)

    def _list(self, *, thread_id=None, shared_with_thread=None, kind=None, open_only=False) -> list[Bubble]:
        with self._lock:
            items = list(self._bubbles.values())
        out = []
        for b in items:
            if thread_id is not None and b.thread_id != thread_id:
                continue
            if shared_with_thread is not None and shared_with_thread not in b.shared_threads:
                continue
            if kind is not None and b.kind != kind:
                continue
            if open_only and b.closed_at is not None:
                continue
            out.append(copy.deepcopy(b))
        out.sort(key=lambda b: b.created_at)
        return out


__all__ = ["InMemoryBubbles"]
