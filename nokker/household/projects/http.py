"""Routes des projets du foyer sur nokker.ai (drapeau `family_projects`, contrat docs/FAMILY-PROJECTS.md §6, lu par
le mur F1 de nokker-front). Session du compte (`us_…`), membre ACTIF d'un foyer ; erreurs
`{"error": {"code", "message"}}` ; 60 écritures par minute et par compte.

    GET    /v1/household/projects?status=active|all            → {today, overdue_names_shown, projects: [...]}
    GET    /v1/household/projects/weekly                       → point hebdo (mur)
    GET    /v1/household/projects/{hp}                         → {project}
    GET    /v1/household/tasks?member=&due_before=&project_id=&include_done=   → {today, member, tasks: [...]}
    POST   /v1/household/tasks/{ht}/done {done?: bool}         → {task}   (coche locale, aucune carte)
    POST   /v1/household/tasks/{ht}/nudge {message?}           → {nudged, to, where, task}   (adultes)
    GET    /v1/household/project-drafts                        → {drafts}   (les miens, ouverts ; adultes)
    GET    /v1/household/project-drafts/{pd}                   → {draft}
    PATCH  /v1/household/project-drafts/{pd}/lines/{n} {action: reject|restore|update, fields?}   → {draft}
    PATCH  /v1/household/project-drafts/{pd}/header {fields}   → {draft}
    GET    /v1/household/project-settings   PUT {overdue_by_member}   (PUT : adultes)

Ces chemins sont sous /v1/household : le serveur appelle `handle` AVANT celui du foyer. `configure(None)` (drapeau
éteint, défaut) : `enabled()` faux, `handle` rend False → le foyer répond comme avant (404).
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.household.projects.service import FamilyProjects, ProjectError

PREFIXES = ("/v1/household/projects", "/v1/household/tasks", "/v1/household/project-drafts",
            "/v1/household/project-settings")
RATE_MAX, RATE_WINDOW_S = 60, 60.0

_svc: FamilyProjects | None = None
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def configure(service: FamilyProjects | None) -> None:
    global _svc
    _svc = service
    _hits.clear()


def enabled() -> bool:
    return _svc is not None


def service() -> FamilyProjects | None:
    return _svc


def is_projects_path(path: str) -> bool:
    route = path.split("?", 1)[0].rstrip("/")
    return any(route == p or route.startswith(p + "/") for p in PREFIXES)


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


def handle(h: Any, method: str, path: str, account: Any) -> bool:
    svc = _svc
    if svc is None or not is_projects_path(path):
        return False
    route = urlsplit(path).path.rstrip("/")
    # Le serveur passe le chemin SANS la requête : elle se lit dans `h.path` (comme les notes du foyer).
    query = {k: v[0] for k, v in parse_qs(urlsplit(str(getattr(h, "path", "") or path)).query).items()}
    parts = [x for x in route[len("/v1/household"):].split("/") if x]
    if method != "GET" and not _rate_ok(account.account_id):
        h._json(429, {"error": {"code": "rate", "message": "trop d'écritures, réessayer dans une minute"}})
        return True
    try:
        out = _dispatch(svc, h, method, parts, query)
    except ProjectError as e:
        code = {404: "not_found", 409: "conflict", 403: "forbidden"}.get(e.status, "bad_request")
        h._json(e.status, {"error": {"code": code, "message": str(e)}})
        return True
    if out is None:
        h._json(404, {"error": {"code": "not_found", "message": "not found"}})
    else:
        h._json(*out)
    return True


def _body(h: Any) -> dict[str, Any]:
    body = h._read_json_body()
    return body if isinstance(body, dict) else {}


def _flag(v: str | None) -> bool:
    return str(v or "").lower() in ("1", "true", "yes", "oui")


def _dispatch(svc: FamilyProjects, h: Any, method: str, parts: list[str], q: dict[str, str]) -> Any:
    head = parts[0]
    if head == "projects" and method == "GET":
        if len(parts) == 1:
            status = q.get("status") or "active"
            if status not in ("active", "all"):
                raise ProjectError("status : active ou all")
            return 200, svc.projects(status=None if status == "all" else "active")
        if parts == ["projects", "weekly"]:
            return 200, svc.weekly()
        if len(parts) == 2:
            home, p, ts = svc.project(parts[1])
            hide = not svc.settings().overdue_by_member
            return 200, {"project": svc.project_json(home, p, ts, svc.today(), hide_overdue=hide)}
    if head == "tasks":
        if parts == ["tasks"] and method == "GET":
            return 200, svc.member_tasks(q.get("member"), q.get("due_before"), q.get("project_id"),
                                         _flag(q.get("include_done")))
        if len(parts) == 3 and method == "POST" and parts[2] == "done":
            body = _body(h)
            done = body.get("done", True)
            if not isinstance(done, bool):
                raise ProjectError("done : vrai ou faux")
            return 200, {"task": svc.set_done(parts[1], done)}
        if len(parts) == 3 and method == "POST" and parts[2] == "nudge":
            return 200, svc.nudge(parts[1], _body(h).get("message"))
    if head == "project-drafts":
        if parts == ["project-drafts"] and method == "GET":
            return 200, {"drafts": [svc.draft_json(d) for d in svc.drafts()]}
        if len(parts) == 2 and method == "GET":
            return 200, {"draft": svc.draft_json(svc.draft(parts[1]))}
        if len(parts) == 4 and parts[2] == "lines" and method == "PATCH":
            body = _body(h)
            action = str(body.get("action") or "")
            if action not in ("reject", "restore", "update"):
                raise ProjectError("action : reject, restore ou update")
            return 200, {"draft": svc.draft_json(svc.edit(parts[1], parts[3], action, body.get("fields")))}
        if len(parts) == 3 and parts[2] == "header" and method == "PATCH":
            return 200, {"draft": svc.draft_json(svc.edit(parts[1], None, "header", _body(h).get("fields")))}
    if parts == ["project-settings"]:
        if method == "PUT":
            svc.put_settings(_body(h))
        if method in ("GET", "PUT"):
            s = svc.settings()
            return 200, {"overdue_by_member": s.overdue_by_member, "wall_ha": list(s.wall_ha),
                         "wall_place": s.wall_place}
    return None


__all__ = ["PREFIXES", "configure", "enabled", "handle", "is_projects_path", "service"]
