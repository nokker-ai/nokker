"""Types des agents (docs/BUBBLES.md §9.4)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

STATUSES = frozenset({"armed", "running", "paused", "failed"})


class AgentError(Exception):
    """Requête invalide (400)."""


class AgentNotFound(AgentError):
    """Agent inconnu (404)."""


@dataclass
class Agent:
    id: str
    name: str
    role: str
    model: str
    tools: list[str] = field(default_factory=list)
    status: str = "armed"
    created_by: str = "nina"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def agent_to_json(a: Agent) -> dict[str, Any]:
    return {"id": a.id, "name": a.name, "role": a.role, "model": a.model, "tools": list(a.tools),
            "status": a.status, "created_by": a.created_by, "created_at": _iso(a.created_at),
            "updated_at": _iso(a.updated_at)}


__all__ = ["STATUSES", "Agent", "AgentError", "AgentNotFound", "agent_to_json"]
