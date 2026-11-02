"""Routes de la salle Maison (docs/HOUSE-ROOM.md §6), derrière le drapeau `house_room` (et `household`) :

    GET  /v1/house                       → {household_id, me, agents}
    GET  /v1/house/messages?after=&before=&limit=   → {messages, cursor}
    POST /v1/house/messages {text}       → 202 {message, reply: null, agent}   (la réponse arrive par `house.message`)

Session du compte (`us_…`) ; erreurs `{"error": {"code", "message"}}` ; 30 messages par minute et par compte.
`configure(None)` (drapeau éteint, défaut) : `enabled()` faux, `handle` rend False, 404 comme avant.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.team.model import TeamError

PREFIX = "/v1/house"
RATE_MAX, RATE_WINDOW_S = 30, 60.0

_room: Any = None
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def configure(room: Any) -> None:
    global _room
    _room = room
    _hits.clear()


def enabled() -> bool:
    return _room is not None


def service() -> Any:
    return _room


def is_house_path(path: str) -> bool:
    route = path.split("?", 1)[0].rstrip("/")
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


def _int(v: str | None) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        raise TeamError(400, "bad_request", "after, before, limit : des entiers") from None


def handle(h: Any, method: str, path: str, account: Any) -> bool:
    room = _room
    route = path.split("?", 1)[0].rstrip("/")
    if room is None or not is_house_path(route):
        return False
    try:
        if route == PREFIX and method == "GET":
            h._json(200, room.view(account.account_id))
        elif route == PREFIX + "/messages" and method == "GET":
            q = parse_qs(urlsplit(getattr(h, "path", "") or "").query)
            one = lambda k: (q.get(k) or [None])[0]
            h._json(200, room.messages(account.account_id, after=_int(one("after")) or 0, before=_int(one("before")),
                                       limit=_int(one("limit"))))
        elif route == PREFIX + "/messages" and method == "POST":
            if not _rate_ok(account.account_id):
                h._json(429, {"error": {"code": "rate", "message": "trop de messages, réessayer dans une minute"}})
                return True
            body = h._read_json_body()
            if not isinstance(body, dict) or not isinstance(body.get("text"), str) or set(body) - {"text"}:
                raise TeamError(400, "bad_request", "corps attendu : {text}")
            h._json(202, room.post(account.account_id, body["text"]))
        else:
            h._json(404, {"error": {"code": "not_found", "message": "not found"}})
    except TeamError as e:
        h._json(e.status, {"error": {"code": e.code, "message": e.message}})
    return True


__all__ = ["PREFIX", "configure", "enabled", "handle", "is_house_path", "service"]
