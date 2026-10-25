"""Routes du SSO OIDC sur nokker.ai (drapeau `sso`, contrat docs/LOKKAL.md §7.24) :

    from nokker.sso import http as sso_http
    sso_http.configure(Sso(...))                                  # une fois au démarrage, drapeau allumé seulement
    if sso_http.handle_public(self, method, path): return        # /v1/auth/oidc/* (pas encore de session)
    if sso_http.handle(self, method, path, account): return       # dans _account_route : session vérifiée

`configure(None)` (drapeau éteint, le défaut) : `enabled()` est faux, `handle*` rendent False et le serveur répond
exactement comme avant (404 sur /v1/sso*, /v1/auth/oidc/*, /v1/projects/{id}/sso).

Publiques (limitées par IP) :
    POST /v1/auth/oidc/start     {connection | email, public_key_spki, name?, lang?} → {authorize_url, state,
                                                                                      connection, expires_at}
    POST /v1/auth/oidc/callback  {state, code | error, signature}                   → {device_id, user, projects}
Session du compte (`us_…`) :
    GET|POST          /v1/sso/connections
    GET|PATCH|DELETE  /v1/sso/connections/{sc}
    PUT               /v1/sso/connections/{sc}/client-secret {value}      (écriture seule)
    POST              /v1/sso/connections/{sc}/domains {domain}
    POST              /v1/sso/connections/{sc}/domains/{domaine}/verify
    DELETE            /v1/sso/connections/{sc}/domains/{domaine}
    GET               /v1/sso/connections/{sc}/audit?limit=
    GET|PUT|DELETE    /v1/projects/{pj}/sso {connection, mappings, required, max_age_s?}
Erreurs : `{"error": {"code", "message"}}`, comme l'équipe.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.sso.service import Sso
from nokker.team.model import TeamError

PREFIX = "/v1/sso/connections"
PUBLIC_PREFIX = "/v1/auth/oidc/"
START_PER_IP, CALLBACK_PER_IP, WRITES_PER_ACCOUNT, WINDOW_S = 20, 30, 60, 60.0

_sso: Sso | None = None
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def configure(sso: Sso | None) -> None:
    global _sso
    _sso = sso
    _hits.clear()


def enabled() -> bool:
    return _sso is not None


def service() -> Sso | None:
    return _sso


def is_sso_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/") or _project_sso(path) is not None


def _project_sso(path: str) -> str | None:
    parts = [x for x in path.split("?", 1)[0].rstrip("/").split("/") if x]
    if len(parts) == 4 and parts[:2] == ["v1", "projects"] and parts[3] == "sso":
        return parts[2]
    return None


def _rate_ok(key: str, limit: int) -> bool:
    now = time.monotonic()
    with _lock:
        q = _hits[key]
        while q and now - q[0] > WINDOW_S:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def _body(h: Any) -> dict[str, Any]:
    b = h._read_json_body()
    return b if isinstance(b, dict) else {}


def _client_ip(h: Any) -> str:
    hd = h.headers
    return hd.get("CF-Connecting-IP") or hd.get("X-Real-IP") or h.client_address[0] or "?"


def _send(h: Any, fn: Any) -> None:
    try:
        out = fn()
    except TeamError as e:
        h._json(e.status, {"error": {"code": e.code, "message": e.message}})
        return
    if out is None:
        h._json(404, {"error": {"code": "not_found", "message": "not found"}})
    else:
        h._json(*out)


def handle_public(h: Any, method: str, path: str) -> bool:
    s = _sso
    route = urlsplit(path).path
    if s is None or not route.startswith(PUBLIC_PREFIX) or method != "POST":
        return False
    step = route[len(PUBLIC_PREFIX):].rstrip("/")
    if step not in ("start", "callback"):
        return False
    ip = _client_ip(h)
    if not _rate_ok(f"{step}:{ip}", START_PER_IP if step == "start" else CALLBACK_PER_IP):
        h._json(429, {"error": {"code": "rate", "message": "trop de tentatives, réessayer dans une minute"}})
        return True
    body = _body(h)
    if step == "start":
        _send(h, lambda: (200, s.start(body, ip)))
    else:
        _send(h, lambda: (200, s.callback(body, ip)))
    return True


def handle(h: Any, method: str, path: str, account: Any) -> bool:
    s = _sso
    route = urlsplit(path).path.rstrip("/")
    if s is None or not is_sso_path(route):
        return False
    if method != "GET" and not _rate_ok(f"w:{account.account_id}", WRITES_PER_ACCOUNT):
        h._json(429, {"error": {"code": "rate", "message": "trop d'écritures, réessayer dans une minute"}})
        return True
    pid = _project_sso(route)
    if pid is not None:
        if method == "GET":
            _send(h, lambda: (200, s.get_binding(account, pid)))
        elif method == "PUT":
            _send(h, lambda: (200, s.put_binding(account, pid, _body(h))))
        elif method == "DELETE":
            _send(h, lambda: (200, s.delete_binding(account, pid)))
        else:
            _send(h, lambda: None)
        return True
    parts = [x for x in route[len(PREFIX):].split("/") if x]
    _send(h, lambda: _dispatch(s, h, method, parts, account))
    return True


def _dispatch(s: Sso, h: Any, method: str, parts: list[str], a: Any) -> tuple[int, Any] | None:
    n = len(parts)
    if n == 0:
        if method == "GET":
            return 200, s.list_connections(a)
        if method == "POST":
            return 201, s.create_connection(a, _body(h))
        return None
    cid = parts[0]
    if n == 1:
        if method == "GET":
            return 200, s.get_connection(a, cid)
        if method == "PATCH":
            return 200, s.patch_connection(a, cid, _body(h))
        if method == "DELETE":
            return 200, s.delete_connection(a, cid)
        return None
    sub = parts[1]
    if sub == "client-secret" and n == 2 and method == "PUT":
        return 200, s.set_client_secret(a, cid, _body(h))
    if sub == "audit" and n == 2 and method == "GET":
        q = {k: v[0] for k, v in parse_qs(urlsplit(h.path).query).items()}
        return 200, s.audit(a, cid, q)
    if sub == "domains":
        if n == 2 and method == "POST":
            return 201, s.add_domain(a, cid, _body(h))
        if n == 4 and parts[3] == "verify" and method == "POST":
            return 200, s.verify_domain(a, cid, parts[2])
        if n == 3 and method == "DELETE":
            return 200, s.delete_domain(a, cid, parts[2])
    s.get_connection(a, cid)  # chemin inconnu sous une connexion : 404 identique, après le contrôle d'accès
    return None


__all__ = ["PREFIX", "PUBLIC_PREFIX", "configure", "enabled", "handle", "handle_public", "is_sso_path", "service"]
