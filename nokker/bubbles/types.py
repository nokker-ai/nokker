"""Types des bulles (docs/BUBBLES.md §2)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

KINDS = frozenset({"monitor", "search", "notify", "timer", "script", "media", "agent_draft", "approval",
                    "episode", "artifact"})  # episode : mémoire de session délestée (docs/NEVER-CLEAR.md §3) ;
# artifact : page créée en session (docs/BUBBLES.md §10), créée par le noyau seulement
STATUSES = frozenset({"idle", "working", "done", "failed"})
MODES = frozenset({"move", "copy"})


class BubbleError(Exception):
    """Requête invalide (400)."""


class BubbleNotFound(BubbleError):
    """Bulle inconnue (404)."""


class BubbleForbidden(BubbleError):
    """Pas membre du fil de la bulle, ou accès en lecture seule (403)."""


class VersionConflict(BubbleError):
    """`if_version` ne correspond pas à la version courante (409)."""

    def __init__(self, current: int):
        super().__init__(f"version courante {current}")
        self.current = current


@dataclass
class Bubble:
    id: str
    thread_id: str | None
    kind: str
    title: str = ""
    status: str = "idle"
    state: dict[str, Any] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    origin: dict[str, Any] | None = None
    shared_members: list[str] = field(default_factory=list)
    shared_threads: list[str] = field(default_factory=list)
    created_by: str = "nina"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    closed_at: float | None = None
    version: int = 1

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def bubble_to_json(b: Bubble, *, shared: bool = False) -> dict[str, Any]:
    d = {"id": b.id, "thread_id": b.thread_id, "kind": b.kind, "title": b.title, "status": b.status,
         "state": b.state, "actions": b.actions, "origin": b.origin,
         "shared_with": {"members": list(b.shared_members), "threads": list(b.shared_threads)},
         "created_by": b.created_by, "created_at": _iso(b.created_at), "updated_at": _iso(b.updated_at),
         "closed_at": _iso(b.closed_at), "version": b.version}
    if shared:
        d["shared"] = True
    return d


__all__ = ["KINDS", "MODES", "STATUSES", "Bubble", "BubbleError", "BubbleForbidden", "BubbleNotFound",
           "VersionConflict", "bubble_to_json"]
