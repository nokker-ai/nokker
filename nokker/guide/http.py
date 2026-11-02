"""Routes du Guide (docs/GUIDE.md §3), derrière le drapeau `guide` :

    GET  /v1/guide        → {profile, show_me, features: [{id, status, used, screen?, ui?, command?}], intro, next,
                             catalog_tokens}
    POST /v1/guide/intro  → {seen: true} (le tour d'accueil a été vu)
    POST /v1/guide/ask {text, history?} → {reply, features: [...], point: {ui, feature}|null}

Toute identité vérifiée (compte `us_…`, propriétaire Lokkal), dans son locataire. `configure(None)` (drapeau éteint,
défaut) : `enabled()` faux, rien ne répond (404 comme avant)."""
from __future__ import annotations

from typing import Any

PREFIX = "/v1/guide"
_svc: Any = None


def configure(service: Any) -> None:
    global _svc
    _svc = service


def enabled() -> bool:
    return _svc is not None


def service() -> Any:
    return _svc


def is_guide_path(path: str) -> bool:
    route = path.split("?", 1)[0].rstrip("/")
    return route == PREFIX or route.startswith(PREFIX + "/")


def handle(h: Any, method: str, path: str, body: dict[str, Any], sender: str, tenant: str) -> None:
    from nokker.guide.service import GuideRefused

    svc = _svc
    route = path.split("?", 1)[0].rstrip("/")
    try:
        if route == PREFIX and method == "GET":
            h._json(200, svc.view(sender))
        elif route == PREFIX + "/intro" and method == "POST":
            svc.mark_intro_seen()
            h._json(200, {"seen": True})
        elif route == PREFIX + "/ask" and method == "POST":
            h._json(200, svc.ask(sender, tenant, body.get("text", ""), body.get("history")))
        elif route in (PREFIX, PREFIX + "/intro", PREFIX + "/ask"):
            h._json(405, {"error": {"code": "method_not_allowed", "message": "méthode non permise"}})
        else:
            h._json(404, {"error": {"code": "not_found", "message": "not found"}})
    except GuideRefused as e:
        h._json(e.status, {"error": {"code": e.code, "message": str(e)}})


__all__ = ["PREFIX", "configure", "enabled", "handle", "is_guide_path", "service"]
