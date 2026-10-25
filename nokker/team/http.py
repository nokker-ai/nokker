"""Routes de l'équipe sur nokker.ai (lot X17, contrat docs/LOKKAL.md §7.15), derrière le drapeau `team` :

    from nokker.team import http as team_http
    team_http.configure(Team(...))                           # une fois au démarrage, drapeau allumé seulement
    if team_http.handle(self, method, path, account): return  # dans _account_route : session vérifiée, locataire fixé
    if team_http.handle_machine(self, method, path, machine): return  # nokker/cloud/http.py : jeton de machine

`configure(None)` (drapeau éteint, le défaut) : `enabled()` est faux, `handle*` rendent False et le serveur répond
exactement comme avant (404 sur /v1/projects*, aucun champ `project` sur les agents).

Session du compte (`us_…`) :
    GET    /v1/projects                               POST /v1/projects {name, description?, repos?, budget?}
    GET    /v1/projects/invitations                   POST /v1/projects/invitations/{iv}/accept | /decline
    GET    /v1/projects/{id}   PATCH {name?, description?, repos?, budget?, mirror?}   DELETE
    GET    /v1/projects/{id}/members                  PATCH|DELETE /v1/projects/{id}/members/{us_…}
    GET    /v1/projects/{id}/invitations              POST {email, role}   DELETE /v1/projects/{id}/invitations/{iv}
    GET    /v1/projects/{id}/skills                   PUT|DELETE /v1/projects/{id}/skills/{nom}
    GET    /v1/projects/{id}/rules                    PUT {mode?, ask, deny}
    GET    /v1/projects/{id}/secrets                  PUT|DELETE /v1/projects/{id}/secrets/{NOM}   (noms seulement)
    GET    /v1/projects/{id}/notes?since=             GET /v1/projects/{id}/notes/quarantine
    POST   /v1/projects/{id}/notes/quarantine/{q}/restore                   (maintainer)
    GET    /v1/projects/{id}/agents                   POST /v1/projects/{id}/agents/{ag}/disarm
    GET    /v1/projects/{id}/budget                   PUT {daily?, monthly?, currency?} | DELETE  (maintainer)
    GET    /v1/projects/{id}/audit?limit=                                   (maintainer)
Jeton de machine (Lokkal lié) :
    GET    /v1/mirror/projects                        GET|POST /v1/mirror/projects/{id}/notes
Erreurs : `{"error": {"code", "message"}}` ; 404 identique pour un projet inconnu et un projet dont on n'est pas membre.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.team.model import TeamError
from nokker.team.service import Team

PREFIX = "/v1/projects"
MIRROR_PREFIX = "/v1/mirror/projects"
RATE_MAX, RATE_WINDOW_S = 60, 60.0  # écritures par compte et par minute (invitations comprises)

_team: Team | None = None
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def configure(team: Team | None) -> None:
    global _team
    _team = team
    _hits.clear()


def enabled() -> bool:
    return _team is not None


def service() -> Team | None:
    return _team


def is_team_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def _rate_ok(key: str) -> bool:
    now = time.monotonic()
    with _lock:
        q = _hits[key]
        while q and now - q[0] > RATE_WINDOW_S:
            q.popleft()
        if len(q) >= RATE_MAX:
            return False
        q.append(now)
        return True


def _error(h: Any, e: TeamError) -> None:
    h._json(e.status, {"error": {**getattr(e, "extra", {}), "code": e.code, "message": e.message}})


def _body(h: Any) -> dict[str, Any]:
    b = h._read_json_body()
    return b if isinstance(b, dict) else {}


def handle(h: Any, method: str, path: str, account: Any) -> bool:
    t = _team
    route = path.split("?", 1)[0].rstrip("/")
    if t is None or not is_team_path(route):
        return False
    parts = [x for x in route[len(PREFIX):].split("/") if x]
    if method != "GET" and not _rate_ok(account.account_id):
        h._json(429, {"error": {"code": "rate", "message": "trop d'écritures, réessayer dans une minute"}})
        return True
    try:
        out = _dispatch(t, h, method, parts, account)
    except TeamError as e:
        _error(h, e)
        return True
    if out is None:
        h._json(404, {"error": {"code": "not_found", "message": "not found"}})
    else:
        status, payload = out
        h._json(status, payload)
    return True


def _dispatch(t: Team, h: Any, method: str, parts: list[str], a: Any) -> tuple[int, Any] | None:
    n = len(parts)
    if n == 0:
        if method == "GET":
            return 200, t.list_projects(a)
        if method == "POST":
            return 201, t.create(a, _body(h))
        return None
    if parts[0] == "invitations":
        if n == 1 and method == "GET":
            return 200, t.my_invites(a)
        if n == 3 and method == "POST" and parts[2] == "accept":
            return 200, t.accept(a, parts[1])
        if n == 3 and method == "POST" and parts[2] == "decline":
            return 200, t.decline(a, parts[1])
        return None
    pid = parts[0]
    if n == 1:
        if method == "GET":
            return 200, t.get(a, pid)
        if method == "PATCH":
            return 200, t.patch(a, pid, _body(h))
        if method == "DELETE":
            return 200, t.delete(a, pid)
        return None
    sub = parts[1]
    if sub == "members":
        if n == 2 and method == "GET":
            return 200, t.members(a, pid)
        if n == 3 and method == "PATCH":
            return 200, t.set_role(a, pid, parts[2], _body(h))
        if n == 3 and method == "DELETE":
            return 200, t.remove(a, pid, parts[2])
    elif sub == "invitations":
        if n == 2 and method == "GET":
            return 200, t.invites(a, pid)
        if n == 2 and method == "POST":
            return 201, t.invite(a, pid, _body(h))
        if n == 3 and method == "DELETE":
            return 200, t.revoke_invite(a, pid, parts[2])
    elif sub == "skills":
        if n == 2 and method == "GET":
            return 200, t.skills(a, pid)
        if n == 3 and method == "PUT":
            return 200, t.put_skill(a, pid, parts[2], _body(h))
        if n == 3 and method == "DELETE":
            return 200, t.delete_skill(a, pid, parts[2])
    elif sub == "rules":
        if n == 2 and method == "GET":
            return 200, t.rules(a, pid)
        if n == 2 and method == "PUT":
            return 200, t.put_rules(a, pid, _body(h))
    elif sub == "secrets":
        if n == 2 and method == "GET":
            return 200, t.secrets(a, pid)
        if n == 3 and method == "PUT":
            return 200, t.put_secret(a, pid, parts[2], _body(h))
        if n == 3 and method == "DELETE":
            return 200, t.delete_secret(a, pid, parts[2])
    elif sub == "notes":
        if n == 2 and method == "GET":
            since = (parse_qs(urlsplit(h.path).query).get("since") or [None])[0]
            return 200, t.notes(a, pid, since)
        if n == 3 and parts[2] == "quarantine" and method == "GET":
            return 200, t.quarantined(a, pid)
        if n == 5 and parts[2] == "quarantine" and parts[4] == "restore" and method == "POST":
            return 200, t.restore_note(a, pid, parts[3])
    elif sub == "agents":
        if n == 2 and method == "GET":
            return 200, {"agents": t.project_agents(a, pid)}
        if n == 4 and parts[3] == "disarm" and method == "POST":
            return 200, {"agent": t.disarm_agent(a, pid, parts[2])}
    elif sub == "budget":
        if n == 2 and method == "GET":
            return 200, t.budget_view(a, pid)
        if n == 2 and method == "PUT":
            return 200, t.put_budget(a, pid, _body(h))
        if n == 2 and method == "DELETE":
            return 200, t.delete_budget(a, pid)
    elif sub == "audit":
        if n == 2 and method == "GET":
            raw = (parse_qs(urlsplit(h.path).query).get("limit") or ["100"])[0]
            return 200, t.audit(a, pid, limit=int(raw) if raw.isdigit() else 100)
    # Chemin inconnu SOUS un projet : la même réponse qu'un projet inconnu, après le contrôle d'accès (un
    # non-membre ne distingue pas un projet existant d'un chemin inexistant).
    t.get(a, pid)
    return None


def handle_machine(h: Any, method: str, path: str, machine: Any) -> bool:
    """Routes d'une machine liée (`nokker/cloud/http.py`, après vérification du jeton de machine). Le compte de la
    machine est l'acteur ; le travail se fait dans SON locataire (la RLS de 023 lui ouvre les projets dont il est
    membre)."""
    t = _team
    route = path.split("?", 1)[0].rstrip("/")
    if t is None or not (route == MIRROR_PREFIX or route.startswith(MIRROR_PREFIX + "/")):
        return False
    from nokker.tenancy.context import tenant_scope

    with tenant_scope(machine.tenant):
        actor = t.actor_for(machine.account)
        if actor is None or actor.tenant_id != machine.tenant:
            h._json(401, {"error": {"code": "unauthorized", "message": "compte de la machine inconnu"}})
            return True
        parts = [x for x in route[len(MIRROR_PREFIX):].split("/") if x]
        try:
            if not parts and method == "GET":
                h._json(200, t.machine_projects(actor))
                return True
            if len(parts) == 2 and parts[1] == "notes" and method in ("GET", "POST"):
                if t.mirror_allowed is None or not t.mirror_allowed(actor.account_id):
                    h._json(403, {"error": {"code": "mirror_subscription", "message": "miroir réservé aux abonnés"}})
                    return True
                if method == "GET":
                    since = (parse_qs(urlsplit(h.path).query).get("since") or [None])[0]
                    h._json(200, t.notes(actor, parts[0], since))
                else:
                    if not _rate_ok(f"machine:{machine.id}"):
                        h._json(429, {"error": {"code": "rate", "message": "trop d'envois, réessayer dans une minute"}})
                        return True
                    h._json(200, t.push_notes(actor, parts[0], machine.id, _body(h).get("notes")))
                return True
        except TeamError as e:
            _error(h, e)
            return True
    h._json(404, {"error": {"code": "not_found", "message": "not found"}})
    return True


__all__ = ["MIRROR_PREFIX", "PREFIX", "configure", "enabled", "handle", "handle_machine", "is_team_path", "service"]
