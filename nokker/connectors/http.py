"""Routes des cartes Connecteurs (T4). Le compte est résolu par la frontière de l'API (session de l'appareil), PAS ici :

    from nokker.connectors import http as connectors_http
    connectors_http.configure(Connectors(store))                       # une fois au démarrage
    if connectors_http.handle(self, method, path, account): return     # après résolution du compte, dans le locataire

    GET    /v1/connectors                 → {connectors:[{kind, state: absent|unverified|ok, settings, connected_at, last_ok_at}], presets}
    PUT    /v1/connectors/{mail|calendar} → 200 carte {…, message}   {settings, secret} ; testé avant d'être enregistré
    POST   /v1/connectors/{kind}/test     → 200 {ok, message, code}   (re-teste le connecteur enregistré)
    DELETE /v1/connectors/{kind}          → 200 {revoked}             (supprime le secret)

`handle` rend False pour un chemin qui n'est pas à lui. Le secret n'est JAMAIS renvoyé ; 8 tentatives de liaison par minute et compte."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any

from nokker.connectors.errors import ConnectorError
from nokker.connectors.service import Connectors
from nokker.connectors.store import KINDS

PREFIX = "/v1/connectors"
RATE_MAX, RATE_WINDOW_S = 8, 60.0
STATUS = {"bad_settings": 400, "port_not_allowed": 400, "host_blocked": 400, "unsupported": 400, "not_configured": 404,
          "auth_failed": 422, "unreachable": 502, "tls_failed": 502, "protocol_error": 502, "key_missing": 503}

_connectors: Connectors | None = None
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def configure(connectors: Connectors | None) -> None:
    global _connectors
    _connectors = connectors
    _hits.clear()


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


def handle(handler: Any, method: str, path: str, account: Any) -> bool:
    route = path.split("?", 1)[0].rstrip("/")
    if _connectors is None or not (route == PREFIX or route.startswith(PREFIX + "/")):
        return False
    parts = route[len(PREFIX):].strip("/").split("/") if route != PREFIX else []
    c = _connectors
    try:
        if method == "GET" and not parts:
            handler._json(200, {"connectors": c.cards(account.account_id), "presets": c.presets()})
        elif method == "PUT" and len(parts) == 1 and parts[0] in KINDS:
            if not _rate_ok(account.account_id):
                handler._json(429, {"error": "rate"})
                return True
            body = handler._read_json_body()
            settings = body.get("settings")
            if not isinstance(settings, dict):
                raise ConnectorError("bad_settings", "settings requis")
            handler._json(200, c.connect(account.account_id, parts[0], settings, str(body.get("secret") or "")))
        elif method == "POST" and len(parts) == 2 and parts[0] in KINDS and parts[1] == "test":
            if not _rate_ok(account.account_id):
                handler._json(429, {"error": "rate"})
                return True
            handler._json(200, c.test(account.account_id, parts[0]))
        elif method == "DELETE" and len(parts) == 1 and parts[0] in KINDS:
            handler._json(200, {"revoked": c.revoke(account.account_id, parts[0])})
        else:
            return False
    except ConnectorError as e:
        handler._json(STATUS.get(e.code, 500), {"error": e.code, "message": e.message})
    return True
