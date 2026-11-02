"""Route média de la veille (docs/HOME-WATCH.md §5.3), derrière le drapeau `home_watch` (Lokkal seulement) :

    GET /v1/home-watch/media?bubble=<bu_…>      → 200 {url, expires_at, kind}

L'écran local (web Lokkal, écran Cast) demande au noyau une URL SIGNÉE de la passerelle pour une bulle `home_watch`
qu'il a le droit de lire ; il charge ensuite l'image (ou le direct) à la passerelle elle-même. Le noyau ne lit jamais
l'image. Refus : 404 (bulle inconnue ou illisible), 403 (droit du membre, fenêtre du direct close).
`configure(None)` : `handle` rend False (404 du serveur, comme avant).
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.tenancy.context import tenant_scope

PATH = "/v1/home-watch/media"
_source: Any = None
_hub: Any = None


def configure(source: Any, hub: Any = None) -> None:
    global _source, _hub
    _source, _hub = source, hub


def handle(h: Any, method: str, path: str) -> bool:
    if _source is None or path != PATH:
        return False
    if method != "GET":
        h._json(405, {"error": {"code": "method", "message": "GET seulement"}})
        return True
    sender = h._resolve_identity({})
    if sender is None:
        h._json(401, {"error": {"code": "unauthorized", "message": "unauthorized"}})
        return True
    tid = h._tenant_for(sender)
    if tid is None:
        return True
    bid = (parse_qs(urlsplit(h.path).query).get("bubble") or [""])[0]
    from nokker.bubbles.types import BubbleError
    from nokker.homewatch.client import BridgeError

    with tenant_scope(tid):
        if tid != _source.tenant or _hub is None:
            h._json(404, {"error": {"code": "not_found", "message": "not found"}})
            return True
        try:
            bubble, _ = _hub.get_bubble(bid, sender)
            h._json(200, _source.media_for(bubble, sender))
        except (BubbleError, LookupError):
            h._json(404, {"error": {"code": "not_found", "message": "not found"}})
        except PermissionError as e:
            h._json(403, {"error": {"code": "forbidden", "message": str(e)}})
        except BridgeError as e:
            h._json(503, {"error": {"code": e.code, "message": str(e)}})
    return True


__all__ = ["PATH", "configure", "handle"]
