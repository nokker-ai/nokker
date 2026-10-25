"""Types des agents (docs/BUBBLES.md §9.4, §9.5) : l'agent (sa carte du deck) et ses passages."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

STATUSES = frozenset({"armed", "running", "paused", "failed"})
TRIGGERS = frozenset({"manual", "schedule", "once", "event"})
RUN_STATUSES = frozenset({"queued", "running", "waiting_approval", "succeeded", "failed", "cancelled", "skipped"})
LIVE_RUN = frozenset({"queued", "running", "waiting_approval"})  # un seul à la fois par agent
FINAL_RUN = RUN_STATUSES - LIVE_RUN


class AgentError(Exception):
    """Requête invalide (400). `code` : identifiant stable pour les écrans (`{"error": {"message", "code"}}`), facultatif ;
    le message reste la raison en clair."""

    def __init__(self, *args: object, code: str | None = None) -> None:
        super().__init__(*args)
        self.code = code


class AgentNotFound(AgentError):
    """Agent ou passage inconnu (404)."""


class AgentConflict(AgentError):
    """État incompatible (409) : passage déjà en cours, échéance passée…"""


class AgentMissing(AgentConflict):
    """Déménagement ou passage refusé (409) : ce qui manque à la destination, PAR NOMS (lot X15) —
    `[{kind: secret|repo|host|connector|mcp|gpu|machine|engine|offer, name?, message}]`."""

    def __init__(self, message: str, missing: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.missing = list(missing)


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
    # §9.5 (lot X14)
    instructions: str = ""
    trigger: dict[str, Any] = field(default_factory=lambda: {"kind": "manual"})
    next_run_at: float | None = None
    last_run: dict[str, Any] | None = None  # {run_id, status, ended_at (epoch)}
    secrets: list[str] = field(default_factory=list)  # NOMS du coffre, jamais de valeur
    budget: dict[str, Any] | None = None  # {max_calls, max_cost, currency}
    auto_approve: list[str] = field(default_factory=list)  # règles `outil(motif)`
    placement: dict[str, Any] | None = None  # X15 : {machine: auto | mc_… | web} ; None = la machine qui porte le noyau
    needs: dict[str, Any] | None = None  # X15 : {repos, hosts, connectors, mcp: [noms], gpu} exigés de la machine
    project: str | None = None  # X17 : champ seulement
    origin: dict[str, Any] | None = None  # créé depuis la carte d'un pack : {pack, template} (pack_remove le retrouve)
    # Internes (jamais servis tels quels) :
    armed: bool = True  # l'humain a armé l'agent (échéances et événements actifs) ; `status` en découle
    approved_rules: str | None = None  # empreinte des règles d'écriture approuvées par une carte forte
    arm_approval_id: str | None = None  # carte forte d'armement en attente
    webhook_key: str | None = None  # clé HMAC du déclencheur webhook (chiffrée si NOKKER_AGENTS_ENC_KEY)


@dataclass
class AgentRun:
    id: str
    agent_id: str
    trigger: str  # manual | schedule | once | event
    status: str = "queued"
    scheduled_for: float | None = None
    started_at: float | None = None
    ended_at: float | None = None
    thread_id: str | None = None
    cost: dict[str, Any] | None = None
    summary: str | None = None
    catch_up: bool = False  # échéance manquée (noyau arrêté) rattrapée ou écartée : au plus une, marquée
    event: str | None = None  # événement déclencheur (`webhook:<id>`, `alert:<nom>`…)
    input: str = ""  # bloc de données non fiables du déclencheur (déjà caviardé), pour la reprise
    approvals: list[str] = field(default_factory=list)  # cartes posées par le passage
    created_at: float = field(default_factory=time.time)
    # X15 : machine où nokker.ai a envoyé ce passage (None = il tourne ici) ; sur la machine, `guest` porte la
    # définition de l'agent du web qui l'a demandé (l'agent ne vit pas ici, son passage oui).
    machine_id: str | None = None
    guest: dict[str, Any] | None = None


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def last_run_json(lr: dict[str, Any] | None) -> dict[str, Any] | None:
    if not lr:
        return None
    return {"run_id": lr.get("run_id"), "status": lr.get("status"), "ended_at": _iso(lr.get("ended_at"))}


def agent_to_json(a: Agent) -> dict[str, Any]:
    return {"id": a.id, "name": a.name, "role": a.role, "model": a.model, "tools": list(a.tools),
            "status": a.status, "created_by": a.created_by, "created_at": _iso(a.created_at),
            "updated_at": _iso(a.updated_at),
            "instructions": a.instructions, "trigger": dict(a.trigger), "next_run_at": _iso(a.next_run_at),
            "last_run": last_run_json(a.last_run), "secrets": list(a.secrets),
            "budget": dict(a.budget) if a.budget else None, "auto_approve": list(a.auto_approve),
            "placement": dict(a.placement) if a.placement else None, "needs": dict(a.needs) if a.needs else None,
            "project": a.project, "origin": dict(a.origin) if a.origin else None,
            "arm_approval_id": a.arm_approval_id}


def run_to_json(r: AgentRun) -> dict[str, Any]:
    return {"run_id": r.id, "agent_id": r.agent_id, "trigger": r.trigger, "scheduled_for": _iso(r.scheduled_for),
            "started_at": _iso(r.started_at), "ended_at": _iso(r.ended_at), "status": r.status,
            "thread_id": r.thread_id, "cost": dict(r.cost) if r.cost else None, "summary": r.summary,
            "catch_up": r.catch_up, "event": r.event, "machine_id": r.machine_id}


__all__ = ["FINAL_RUN", "LIVE_RUN", "RUN_STATUSES", "STATUSES", "TRIGGERS", "Agent", "AgentConflict", "AgentError",
           "AgentMissing", "AgentNotFound", "AgentRun", "agent_to_json", "last_run_json", "run_to_json"]
