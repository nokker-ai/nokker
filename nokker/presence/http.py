"""Route « Qui fait quoi » (docs/WHO-DOES-WHAT.md §3), derrière le drapeau `who_does_what` :

    GET /v1/presence → {viewer, entries, counts, next_waiting}

Session du compte (`us_…`), lecture seule. `configure(None)` (drapeau éteint, défaut) : `enabled()` faux, 404 comme avant."""
from __future__ import annotations

from typing import Any

PREFIX = "/v1/presence"
_svc: Any = None


def configure(service: Any) -> None:
    global _svc
    _svc = service


def enabled() -> bool:
    return _svc is not None


def service() -> Any:
    return _svc


def is_presence_path(path: str) -> bool:
    route = path.split("?", 1)[0].rstrip("/")
    return route == PREFIX or route.startswith(PREFIX + "/")


def handle(h: Any, method: str, path: str, account: Any) -> bool:
    svc = _svc
    route = path.split("?", 1)[0].rstrip("/")
    if svc is None or not is_presence_path(route):
        return False
    if route == PREFIX and method == "GET":
        h._json(200, svc.view(account.account_id))
    elif route == PREFIX:
        h._json(405, {"error": {"code": "read_only", "message": "vue en lecture seule"}})
    else:
        h._json(404, {"error": {"code": "not_found", "message": "not found"}})
    return True


__all__ = ["PREFIX", "configure", "enabled", "handle", "is_presence_path", "service"]
