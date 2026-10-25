"""Types des incidents (docs/LOKKAL.md §7.18) : la source d'alerte (une URL signée par source), l'incident (regroupé
par empreinte, ouvert → en cours → résolu) et son fil (ce qui s'est passé, les passages d'agents, leurs cartes)."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

FORMATS = ("alertmanager", "generic")
STATES = ("open", "in_progress", "resolved")
LIVE_STATES = ("open", "in_progress")
MAX_TIMELINE = 200  # entrées gardées par incident (la première, l'ouverture, n'est jamais retirée)


class IncidentError(Exception):
    """Requête invalide (400) ; `status` et `code` pour la réponse."""

    status = 400
    code = "invalid"

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code


class IncidentNotFound(IncidentError):
    status = 404
    code = "not_found"


def new_source_id() -> str:
    return "as_" + secrets.token_hex(6)


def new_incident_id() -> str:
    return "in_" + secrets.token_hex(6)


@dataclass
class AlertSource:
    id: str
    name: str
    format: str
    created_by: str
    created_at: float
    key: str | None = None  # "enc:<fernet>" | "raw:<clé>" (HMAC) | "sha256:<hex>" (jeton Bearer haché) ; jamais servie
    last_seen_at: float | None = None

    @property
    def auth(self) -> str:
        """`hmac` (défaut : corps signé) ou `bearer` (jeton dans `Authorization`, rangé haché) — lu sur la forme de la
        clé rangée, sans colonne de plus (migration 029 inchangée)."""
        return "bearer" if (self.key or "").startswith("sha256:") else "hmac"


@dataclass
class Incident:
    id: str
    source_id: str
    fingerprint: str
    name: str  # nom de l'alerte (alertname), celui de l'événement `alert:<nom>`
    first_seen_at: float
    last_seen_at: float
    created_at: float
    updated_at: float
    source_name: str = ""
    title: str = ""
    severity: str = "warning"
    state: str = "open"
    labels: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    occurrences: int = 1
    started_at: float | None = None
    resolved_at: float | None = None
    resolved_by: str | None = None  # "source:<nom>" (résolution venue d'Alertmanager) ou l'identité qui résout
    acked_by: str | None = None
    runs: list[dict[str, Any]] = field(default_factory=list)  # [{run_id, agent_id, agent, status, summary, approvals}]
    timeline: list[dict[str, Any]] = field(default_factory=list)  # [{at, kind, text, by?, run_id?, approval_id?}]


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def source_to_json(s: AlertSource) -> dict[str, Any]:
    return {"id": s.id, "name": s.name, "format": s.format, "url": f"/v1/alerts/{s.id}", "created_by": s.created_by,
            "created_at": iso(s.created_at), "last_seen_at": iso(s.last_seen_at), "has_secret": bool(s.key),
            "auth": s.auth}


def _entry(e: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in e.items() if k != "at"}
    out["at"] = iso(e.get("at"))
    return out


def incident_to_json(i: Incident, *, timeline: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": i.id, "source": {"id": i.source_id, "name": i.source_name}, "fingerprint": i.fingerprint,
        "name": i.name, "title": i.title, "severity": i.severity, "state": i.state, "labels": dict(i.labels),
        "summary": i.summary, "occurrences": i.occurrences, "first_seen_at": iso(i.first_seen_at),
        "last_seen_at": iso(i.last_seen_at), "started_at": iso(i.started_at), "resolved_at": iso(i.resolved_at),
        "resolved_by": i.resolved_by, "acked_by": i.acked_by,
        "runs": [{**r, "ended_at": iso(r.get("ended_at"))} for r in i.runs],
        "created_at": iso(i.created_at), "updated_at": iso(i.updated_at),
        "last_event": _entry(i.timeline[-1]) if i.timeline else None,
    }
    if timeline:
        out["timeline"] = [_entry(e) for e in i.timeline]
    return out


__all__ = ["FORMATS", "LIVE_STATES", "MAX_TIMELINE", "STATES", "AlertSource", "Incident", "IncidentError",
           "IncidentNotFound", "incident_to_json", "iso", "new_incident_id", "new_source_id", "source_to_json"]
