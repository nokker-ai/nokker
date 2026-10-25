"""Routes du plan (lot X18, docs/LOKKAL.md §7.18), derrière le drapeau `plan` (éteint par défaut) :

    from nokker.plan import http as plan_http
    plan_http.configure(Plan(...))                   # au démarrage, drapeau allumé seulement
    if plan_http.handle(self, method, path): return   # dans le dispatcheur du serveur

`configure(None)` : `handle` rend False, le serveur répond comme avant (404).

Identité vérifiée du locataire (comme /v1/agents) :
    GET    /v1/plan/cards?project=&roots=1          POST /v1/plan/cards {title, kind?, parent_id?, project?, …}
    GET    /v1/plan/cards/<pc_…>                    PATCH /v1/plan/cards/<id> {…}   DELETE /v1/plan/cards/<id>
    GET    /v1/plan/cards/<id>/context
    POST   /v1/plan/cards/<id>/threads {thread_id}  DELETE /v1/plan/cards/<id>/threads/<thread_id>
    GET    /v1/plan/proposals?card_id=&state=       POST /v1/plan/proposals/<pp_…>/accept | /refuse
    GET    /v1/plan/brief                           POST /v1/plan/brief {cron?, tz?, model?}   DELETE /v1/plan/brief
    GET    /v1/plan/brief/delivery                  PUT {telegram?: bool, email?: adresse | null}
Erreurs : `{"error": {"code", "message"}}` ; une carte d'un autre locataire n'existe pas (404).
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from nokker.agents.types import AgentError, agent_to_json
from nokker.plan.service import Plan
from nokker.plan.types import PlanError, proposal_to_json
from nokker.tenancy.context import scoped_thread_id, tenant_scope

PREFIX = "/v1/plan"
_RE_CARD = re.compile(r"^/v1/plan/cards/(pc_[0-9a-f]{12})(?:/(context|threads)(?:/([^/]+))?)?$")
_RE_PROPOSAL = re.compile(r"^/v1/plan/proposals/(pp_[0-9a-f]{12})/(accept|refuse)$")

_plan: Plan | None = None


def configure(plan: Plan | None) -> None:
    global _plan
    _plan = plan


def service() -> Plan | None:
    return _plan


def is_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def _err(h: Any, status: int, code: str, message: str) -> None:
    h._json(status, {"error": {"code": code, "message": message}})


def handle(h: Any, method: str, path: str) -> bool:
    plan = _plan
    if plan is None or not is_path(path):
        return False
    body = h._read_json_body() if method in ("POST", "PATCH", "PUT") else {}
    body = body if isinstance(body, dict) else {}
    sender = h._resolve_identity(body)
    if sender is None:
        _err(h, 401, "unauthorized", "unauthorized")
        return True
    tid = h._tenant_for(sender)
    if tid is None:
        return True
    try:
        with tenant_scope(tid):
            out = _dispatch(plan, h, method, path, body, sender, tid)
    except PlanError as e:
        _err(h, e.status, e.code, e.message)
        return True
    except AgentError as e:
        _err(h, 400, "invalid", str(e))
        return True
    if out is None:
        _err(h, 404, "not_found", "not found")
    else:
        h._json(*out)
    return True


def _q(h: Any) -> dict[str, list[str]]:
    return parse_qs(urlsplit(h.path).query)


def _dispatch(plan: Plan, h: Any, method: str, path: str, body: dict[str, Any], sender: str, tid: str
              ) -> tuple[int, Any] | None:
    if path == "/v1/plan/cards":
        if method == "GET":
            q = _q(h)
            project = (q.get("project") or [None])[0]
            roots = (q.get("roots") or ["0"])[0] in ("1", "true")
            return 200, {"cards": plan.list_cards(project=project, roots=roots)}
        if method == "POST":
            return 201, plan.create(body, sender)
        return None
    m = _RE_CARD.match(path)
    if m:
        cid, sub, tail = m.group(1), m.group(2), m.group(3)
        if sub is None:
            if method == "GET":
                return 200, plan.get_card(cid)
            if method == "PATCH":
                return 200, plan.patch(cid, body, sender)
            if method == "DELETE":
                return 200, plan.delete(cid, sender)
            return None
        if sub == "context" and tail is None and method == "GET":
            return 200, plan.context(cid)
        if sub == "threads":
            if tail is None and method == "POST":
                thread = str(body.get("thread_id") or "").strip()
                if not thread:
                    raise PlanError("thread_id : requis")
                return 200, plan.attach(cid, scoped_thread_id(tid, thread), sender)
            if tail is not None and method == "DELETE":
                return 200, plan.detach(cid, scoped_thread_id(tid, unquote(tail)), sender)
        return None
    if path == "/v1/plan/proposals" and method == "GET":
        q = _q(h)
        items = plan.proposals((q.get("card_id") or [None])[0], (q.get("state") or [None])[0])
        return 200, {"proposals": [proposal_to_json(p) for p in items]}
    m = _RE_PROPOSAL.match(path)
    if m and method == "POST":
        return 200, plan.decide(m.group(1), m.group(2), sender)
    if path == "/v1/plan/brief/delivery":
        if plan.delivery is None:
            return None
        if method == "GET":
            return 200, plan.delivery.get(sender)
        if method == "PUT":
            return 200, plan.delivery.put(sender, body)
        return None
    if path == "/v1/plan/brief":
        if method == "GET":
            a = plan.get_brief(sender)
            return 200, {"agent": agent_to_json(a) if a is not None else None}
        if method == "POST":
            a, created = plan.ensure_brief(body, sender)
            return (201 if created else 200), {"agent": agent_to_json(a)}
        if method == "DELETE":
            return 200, {"deleted": plan.delete_brief(sender)}
    return None


__all__ = ["PREFIX", "configure", "handle", "is_path", "service"]
