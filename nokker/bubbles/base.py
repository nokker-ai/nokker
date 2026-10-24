"""Stockage des bulles : logique commune (`version` croissante, `if_version` → 409) ; la sous-classe
(`memory.InMemoryBubbles`, `pg.PgBubbles`) ne fournit que les primitives. Les DROITS et les événements
vivent dans `hub.BubbleHub` (un seul endroit pour les routes HTTP et les outils du modèle)."""
from __future__ import annotations

import copy
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nokker.bubbles.types import KINDS, STATUSES, Bubble, BubbleError, BubbleNotFound, VersionConflict


def new_bubble_id() -> str:
    return "bu_" + secrets.token_hex(6)


@dataclass
class BaseBubbles:
    clock: Callable[[], float] = time.time

    def _load(self, bubble_id: str) -> Bubble | None:
        raise NotImplementedError

    def _store(self, bubble: Bubble) -> None:
        raise NotImplementedError

    def _list(self, *, thread_id: str | None = None, shared_with_thread: str | None = None,
              kind: str | None = None, open_only: bool = False) -> list[Bubble]:
        raise NotImplementedError

    def get(self, bubble_id: str) -> Bubble | None:
        return self._load(bubble_id)

    def require(self, bubble_id: str) -> Bubble:
        b = self._load(bubble_id)
        if b is None:
            raise BubbleNotFound(bubble_id)
        return b

    def create(self, *, thread_id: str | None, kind: str, title: str = "", state: dict[str, Any] | None = None,
               status: str = "idle", actions: list[dict[str, Any]] | None = None,
               origin: dict[str, Any] | None = None, created_by: str = "nina",
               shared_members: list[str] | None = None) -> Bubble:
        if kind not in KINDS:
            raise BubbleError(f"kind inconnu : {kind}")
        if status not in STATUSES:
            raise BubbleError(f"status inconnu : {status}")
        now = self.clock()
        b = Bubble(id=new_bubble_id(), thread_id=thread_id, kind=kind, title=(title or "")[:200],
                   status=status, state=dict(state or {}), actions=list(actions or []), origin=origin,
                   shared_members=list(shared_members or []), created_by=created_by, created_at=now,
                   updated_at=now)
        self._store(b)
        return b

    def clone(self, src: Bubble, *, thread_id: str, origin: dict[str, Any]) -> Bubble:
        now = self.clock()
        b = copy.deepcopy(src)
        b.id, b.thread_id, b.origin = new_bubble_id(), thread_id, origin
        b.shared_members, b.shared_threads = [], []
        b.created_at = b.updated_at = now
        b.closed_at, b.version = None, 1
        self._store(b)
        return b

    def update(self, bubble_id: str, *, title: str | None = None, state: dict[str, Any] | None = None,
               status: str | None = None, if_version: int | None = None, thread_id: Any = ...,
               origin: Any = ..., shared_members: list[str] | None = None,
               shared_threads: list[str] | None = None, close: bool = False) -> Bubble:
        b = self.require(bubble_id)
        if if_version is not None and int(if_version) != b.version:
            raise VersionConflict(b.version)
        if status is not None and status not in STATUSES:
            raise BubbleError(f"status inconnu : {status}")
        if title is not None:
            b.title = str(title)[:200]
        if state is not None:
            if not isinstance(state, dict):
                raise BubbleError("state doit être un objet")
            b.state = state
        if status is not None:
            b.status = status
        if thread_id is not ...:
            b.thread_id = thread_id
        if origin is not ...:
            b.origin = origin
        if shared_members is not None:
            b.shared_members = list(dict.fromkeys(shared_members))
        if shared_threads is not None:
            b.shared_threads = list(dict.fromkeys(shared_threads))
        now = self.clock()
        if close and b.closed_at is None:
            b.closed_at = now
        b.updated_at = now
        b.version += 1
        self._store(b)
        return b

    def list_thread(self, thread_id: str, include_closed: bool = False) -> list[Bubble]:
        return self._list(thread_id=thread_id, open_only=not include_closed)

    def list_shared_with_thread(self, thread_id: str, include_closed: bool = False) -> list[Bubble]:
        return self._list(shared_with_thread=thread_id, open_only=not include_closed)

    def list_open_notify(self) -> list[Bubble]:
        return self._list(kind="notify", open_only=True)


__all__ = ["BaseBubbles", "new_bubble_id"]
