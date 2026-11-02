"""Routes du calendrier familial sur nokker.ai (drapeau `family_calendar`, docs/FAMILY-CALENDAR.md §6). Session du
compte (`us_…`), dans SON locataire ; erreurs `{"error": {"code", "message"}}` ; 60 écritures par minute et par compte.

    GET    /v1/family-calendar/drafts                     → {drafts: [...]} (brouillons ouverts)
    GET    /v1/family-calendar/drafts/{id}                → {draft} (lignes, état, raison de chaque ligne non écrite)
    PATCH  /v1/family-calendar/drafts/{id}/lines/{n} {action: reject|restore|keep|update, fields?}  → {draft}
    GET    /v1/family-calendar/drafts/{id}/ics            → fichier .ics, SEULEMENT après la carte (409 avant)
    GET    /v1/family-calendar/settings   PUT {people?, timezone?}

La carte web montre le brouillon ligne par ligne et l'édite par PATCH (chaque modification fait monter la révision ;
une carte déjà posée sur une révision antérieure n'écrit plus rien). `configure(None)` (drapeau éteint, défaut) :
`enabled()` faux, `handle` rend False → 404 comme avant.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any

from nokker.household.famcal.service import FamcalError, FamilyCalendar

PREFIX = "/v1/family-calendar"
RATE_MAX, RATE_WINDOW_S = 60, 60.0

_svc: FamilyCalendar | None = None
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def configure(service: FamilyCalendar | None) -> None:
    global _svc
    _svc = service
    _hits.clear()


def enabled() -> bool:
    return _svc is not None


def service() -> FamilyCalendar | None:
    return _svc


def is_famcal_path(path: str) -> bool:
    route = path.split("?", 1)[0]
    return route == PREFIX or route.startswith(PREFIX + "/")


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
    route = path.split("?", 1)[0].rstrip("/")
    if svc is None or not is_famcal_path(route):
        return False
    parts = [x for x in route[len(PREFIX):].split("/") if x]
    if method != "GET" and not _rate_ok(account.account_id):
        h._json(429, {"error": {"code": "rate", "message": "trop d'écritures, réessayer dans une minute"}})
        return True
    try:
        out = _dispatch(svc, h, method, parts)
    except FamcalError as e:
        code = {404: "not_found", 409: "conflict", 403: "forbidden"}.get(e.status, "bad_request")
        h._json(e.status, {"error": {"code": code, "message": str(e)}})
        return True
    if out is None:
        h._json(404, {"error": {"code": "not_found", "message": "not found"}})
    elif out != "sent":
        h._json(*out)
    return True


def _body(h: Any) -> dict[str, Any]:
    body = h._read_json_body()
    return body if isinstance(body, dict) else {}


def _dispatch(svc: FamilyCalendar, h: Any, method: str, parts: list[str]) -> Any:
    if parts == ["drafts"] and method == "GET":
        return 200, {"drafts": [svc.draft_json(d) for d in svc.drafts("open")]}
    if len(parts) == 2 and parts[0] == "drafts" and method == "GET":
        return 200, {"draft": svc.draft_json(svc.draft(parts[1]))}
    if len(parts) == 4 and parts[0] == "drafts" and parts[2] == "lines" and method == "PATCH":
        body = _body(h)
        d = svc.edit(parts[1], parts[3], str(body.get("action") or ""), body.get("fields"))
        return 200, {"draft": svc.draft_json(d)}
    if len(parts) == 3 and parts[0] == "drafts" and parts[2] == "ics" and method == "GET":
        content = svc.ics(parts[1]).encode("utf-8")
        h.send_response(200)
        h.send_header("Content-Type", "text/calendar; charset=utf-8")
        h.send_header("Content-Length", str(len(content)))
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("Content-Disposition", f"attachment; filename=\"calendrier-{parts[1]}.ics\"")
        h.end_headers()
        h.wfile.write(content)
        return "sent"
    if parts == ["settings"]:
        if method == "PUT":
            svc.put_settings(_body(h))
        if method in ("GET", "PUT"):
            s = svc.settings()
            return 200, {"people": s.people, "timezone": s.timezone}
    return None


__all__ = ["PREFIX", "configure", "enabled", "handle", "is_famcal_path", "service"]
