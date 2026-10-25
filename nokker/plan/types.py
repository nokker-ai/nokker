"""Types du plan (docs/LOKKAL.md §7.18) : la carte (objectif → lots → tâches), la proposition de découpe, le brief."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

KINDS = ("goal", "milestone", "task")
STATUSES = ("todo", "in_progress", "blocked", "done")
PROPOSAL_STATES = ("proposed", "accepted", "refused", "stale")
MAX_DEPTH = 6
MAX_CARDS = 2000  # par locataire
MAX_CHILDREN_PROPOSED = 20


class PlanError(Exception):
    status = 400
    code = "invalid"

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code


class PlanNotFound(PlanError):
    status = 404
    code = "not_found"


def new_card_id() -> str:
    return "pc_" + secrets.token_hex(6)


def new_proposal_id() -> str:
    return "pp_" + secrets.token_hex(6)


@dataclass
class Card:
    id: str
    kind: str
    title: str
    created_by: str
    created_at: float
    updated_at: float
    parent_id: str | None = None
    project: str | None = None  # projet d'équipe (X17) ; None = carte du compte
    intent: str = ""  # ce que la carte doit obtenir
    constraints: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    status: str = "todo"  # déclaré sur une feuille ; recalculé pour une carte qui a des filles
    agents: list[str] = field(default_factory=list)  # agents Crew (ag_…) qui travaillent sur la carte
    threads: list[str] = field(default_factory=list)  # sessions (fils) rattachées
    position: int = 0


@dataclass
class Proposal:
    id: str
    card_id: str
    proposed_by: str
    created_at: float
    children: list[dict[str, Any]] = field(default_factory=list)  # [{title, kind, intent, constraints}]
    reason: str = ""
    state: str = "proposed"
    thread_id: str | None = None
    bubble_id: str | None = None
    created: list[str] = field(default_factory=list)  # cartes créées à l'acceptation
    decided_by: str | None = None
    decided_at: float | None = None


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def proposal_to_json(p: Proposal) -> dict[str, Any]:
    return {"id": p.id, "card_id": p.card_id, "children": [dict(c) for c in p.children], "reason": p.reason,
            "state": p.state, "proposed_by": p.proposed_by, "thread_id": p.thread_id, "bubble_id": p.bubble_id,
            "created": list(p.created), "decided_by": p.decided_by, "decided_at": iso(p.decided_at),
            "created_at": iso(p.created_at)}


__all__ = ["KINDS", "MAX_CARDS", "MAX_CHILDREN_PROPOSED", "MAX_DEPTH", "PROPOSAL_STATES", "STATUSES", "Card", "PlanError",
           "PlanNotFound", "Proposal", "iso", "new_card_id", "new_proposal_id", "proposal_to_json"]
