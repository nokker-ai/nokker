"""Types des fils (docs/BUBBLES.md §1)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class ThreadError(Exception):
    """Erreur générique (400)."""


class ThreadNotFound(ThreadError):
    """Fil inconnu (404)."""


class ThreadForbidden(ThreadError):
    """L'identité n'est pas membre du fil (403)."""


@dataclass
class Thread:
    id: str
    title: str = ""
    owner: str | None = None
    members: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    working: bool = False
    last_snippet: str = ""
    # Fil d'un passage d'agent (docs/BUBBLES.md §9.5) : {"kind": "agent", "agent_id", "run_id"} ; None ailleurs.
    origin: dict[str, Any] | None = None

    def is_member(self, user: str | None) -> bool:
        """Propriétaire ou membre. Un fil sans propriétaire (serveur ouvert, aucun `sender` vérifié) n'a
        aucun membre : il n'est listé pour personne (pas de « tout le monde » implicite)."""
        if not user:
            return False
        return user == self.owner or user in self.members

    def audience(self) -> set[str]:
        out = set(self.members)
        if self.owner:
            out.add(self.owner)
        return out


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def thread_to_json(t: Thread, bubble_count: int = 0) -> dict[str, Any]:
    members = list(dict.fromkeys([*(m for m in [t.owner] if m), *t.members]))
    out = {"id": t.id, "title": t.title, "owner": t.owner, "members": members,
           "created_at": _iso(t.created_at), "updated_at": _iso(t.updated_at), "working": t.working,
           "unread": 0, "bubble_count": bubble_count, "last_snippet": t.last_snippet}
    if t.origin:
        out["origin"] = dict(t.origin)  # absent pour les fils ordinaires (§9.5)
    return out


__all__ = ["Thread", "ThreadError", "ThreadForbidden", "ThreadNotFound", "thread_to_json"]
