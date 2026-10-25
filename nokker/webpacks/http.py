"""Routes des packs d'agents sur nokker.ai (docs/LOKKAL.md §7.30), derrière le drapeau `packs_web` :

    from nokker.webpacks import http as packs_http
    packs_http.configure(WebPacks(...))                       # une fois au démarrage, drapeau allumé seulement
    if packs_http.handle(self, method, path, account): return  # dans _account_route : session vérifiée, locataire fixé

`configure(None)` (le défaut) : `enabled()` est faux, `handle` rend False et le serveur répond comme avant (404).

Session du compte (`us_…`) :
    GET    /v1/packs                          → {packs: [{name, title, …, installed, web: {skills, agents},
                                                 machine: {skills, agents}}]}
    GET    /v1/packs/{nom}                    → détail, `installed`, `install`, `created_agents`
    POST   /v1/packs/{nom}/install {bind?, thread_id?, handoff?}  → {installed, skills, proposed, to_propose?,
                                                 partial, machine: {skills, agents, linked, handoff, next} | null}
    POST   /v1/packs/{nom}/propose {thread_id, templates?}        → {pack, proposed, to_propose?}
    DELETE /v1/packs/{nom}?thread_id=…        → {removed, skills, already_removed, drafts_closed, agents, card}
Erreurs : `{"error": {"code", "message", …}}` — 400 corps, 404 pack ou fil inconnus (un fil d'un autre compte
n'existe pas), 409 `machine_only` (rien ne s'installe au web : `machine` dit quoi faire) ou `invalid_pack`, 422
`scan_refused` (`report`), 429 > 30 écritures par minute et par compte, 503 `no_scanner` (garde en échec fermé).
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.webpacks.service import WebPackError, WebPacks

PREFIX = "/v1/packs"
RATE_MAX, RATE_WINDOW_S = 30, 60.0

_packs: WebPacks | None = None
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def configure(service: WebPacks | None) -> None:
    global _packs
    _packs = service
    _hits.clear()


def enabled() -> bool:
    return _packs is not None


def service() -> WebPacks | None:
    return _packs


def is_packs_path(path: str) -> bool:
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


def _thread(raw: Any) -> str | None:
    """Fil du client → fil du noyau (rangé sous le locataire courant), comme les routes des bulles."""
    if not raw:
        return None
    from nokker.tenancy.context import current_tenant, scoped_thread_id

    return scoped_thread_id(current_tenant(), str(raw))


def handle(h: Any, method: str, path: str, account: Any) -> bool:
    svc = _packs
    if svc is None or not is_packs_path(path):
        return False
    route = urlsplit(h.path).path.rstrip("/") if getattr(h, "path", None) else path.rstrip("/")
    parts = [x for x in route[len(PREFIX):].split("/") if x]
    acc = account.account_id
    if method != "GET" and not _rate_ok(acc):
        h._json(429, {"error": {"code": "rate", "message": "trop d'écritures, réessayer dans une minute"}})
        return True
    try:
        if not parts and method == "GET":
            h._json(200, svc.catalog(acc))
        elif len(parts) == 1 and method == "GET":
            h._json(200, svc.detail(acc, parts[0]))
        elif len(parts) == 1 and method == "DELETE":
            q = parse_qs(urlsplit(getattr(h, "path", "") or "").query)
            h._json(200, svc.remove(acc, parts[0], thread_id=_thread((q.get("thread_id") or [None])[0])))
        elif len(parts) == 2 and method == "POST" and parts[1] in ("install", "propose"):
            body = h._read_json_body()
            if not isinstance(body, dict):
                raise WebPackError(400, "bad_body", "corps JSON objet attendu")
            if parts[1] == "install":
                if "handoff" in body and not isinstance(body["handoff"], bool):
                    raise WebPackError(400, "bad_body", "handoff : booléen")
                h._json(200, svc.install(acc, parts[0], body.get("bind"), thread_id=_thread(body.get("thread_id")),
                                         handoff=bool(body.get("handoff"))))
            else:
                templates = body.get("templates")
                if templates is not None and not (isinstance(templates, list)
                                                  and all(isinstance(t, str) for t in templates)):
                    raise WebPackError(400, "bad_body", "templates : liste de noms")
                h._json(200, svc.propose(acc, parts[0], _thread(body.get("thread_id")) or "", templates))
        else:
            h._json(404, {"error": {"code": "not_found", "message": "not found"}})
    except WebPackError as e:
        h._json(e.status, {"error": {"code": e.code, "message": e.message, **e.payload}})
    return True


__all__ = ["PREFIX", "configure", "enabled", "handle", "is_packs_path", "service"]
