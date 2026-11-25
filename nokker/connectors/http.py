"""Routes des cartes Connecteurs (T4). Le compte est résolu par la frontière de l'API (session de l'appareil), PAS ici :

    from nokker.connectors import http as connectors_http
    connectors_http.configure(Connectors(store))                       # une fois au démarrage
    if connectors_http.handle(self, method, path, account): return     # après résolution du compte, dans le locataire

    GET    /v1/connectors                 → {connectors:[Carte], members:[Membre], presets}   (068 ; avant : cartes par nature)
    POST   /v1/connectors                 → 201 Carte {…, message}    {kind, label?, settings, secret} ; testé avant
    PATCH  /v1/connectors/{id}            → 200 Carte                  {label?, default?: true}
    POST   /v1/connectors/{id}/test       → 200 {ok, message, code}
    DELETE /v1/connectors/{id}            → 200 {revoked}              (ses partages tombent)
    POST   /v1/connectors/{id}/grants     → 202 {status: pending, approval_id} | 200 {status: pending (même carte) | unchanged | updated (descente)}
    DELETE /v1/connectors/{id}/grants/{account_id|me} → 200 {revoked}
  Routes d'avant 068, servies sur le connecteur PAR DÉFAUT de la nature :
    PUT    /v1/connectors/{mail|calendar} → 200 carte {…, message}   {settings, secret} ; testé avant d'être enregistré
    POST   /v1/connectors/{kind}/test     → 200 {ok, message, code}   (re-teste le connecteur enregistré)
    DELETE /v1/connectors/{kind}          → 200 {revoked}             (supprime le secret)
Contrat complet : docs/CONNECTORS-MULTI.md.

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
          # Service TIERS en échec (serveur d'agenda, IMAP) : 424, jamais 502/504 — derrière Cloudflare et l'ingress, un
          # 502 d'origine est remplacé par une page HTML de passerelle et le web perd le JSON (rév. 37, iCal en 500).
          "auth_failed": 422, "unreachable": 424, "tls_failed": 424, "protocol_error": 424, "key_missing": 503,
          "bad_label": 400, "bad_level": 400, "label_taken": 409, "too_many": 409, "stale": 409, "not_found": 404,
          "not_owner": 403, "not_household_member": 403, "provider_off": 404, "bad_service": 400}

CALLBACK = PREFIX + "/oauth/callback"

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
            handler._json(200, c.listing(account))
        elif parts[:1] == ["oauth"]:
            # docs/CONNECTORS-OAUTH.md §3-4 : start, reconnect, suppression du compte relié (le callback est public)
            if method == "POST" and len(parts) == 3 and parts[2] == "start":
                if not _rate_ok(account.account_id):
                    handler._json(429, {"error": "rate"})
                    return True
                handler._json(200, c.oauth_start(account, parts[1], handler._read_json_body()))
            elif method == "POST" and len(parts) == 4 and parts[1] == "accounts" and parts[3] == "reconnect":
                handler._json(200, c.oauth_reconnect(account, parts[2]))
            elif method == "DELETE" and len(parts) == 3 and parts[1] == "accounts":
                handler._json(200, {"revoked": c.oauth_delete(account, parts[2])})
            else:
                return False
        elif method == "POST" and not parts:
            if not _rate_ok(account.account_id):
                handler._json(429, {"error": "rate"})
                return True
            body = handler._read_json_body()
            settings = body.get("settings")
            if not isinstance(settings, dict):
                raise ConnectorError("bad_settings", "settings requis")
            handler._json(201, c.add(account, str(body.get("kind") or ""), str(body.get("label") or ""), settings,
                                     str(body.get("secret") or "")))
        elif method == "PUT" and len(parts) == 1 and parts[0] in KINDS:
            if not _rate_ok(account.account_id):
                handler._json(429, {"error": "rate"})
                return True
            body = handler._read_json_body()
            settings = body.get("settings")
            if not isinstance(settings, dict):
                raise ConnectorError("bad_settings", "settings requis")
            handler._json(200, c.connect(account.account_id, parts[0], settings, str(body.get("secret") or "")))
        elif method == "POST" and len(parts) == 2 and parts[1] == "test":
            if not _rate_ok(account.account_id):
                handler._json(429, {"error": "rate"})
                return True
            handler._json(200, c.test(account.account_id, parts[0]) if parts[0] in KINDS else c.test_id(account, parts[0]))
        elif method == "DELETE" and len(parts) == 1:
            handler._json(200, {"revoked": c.revoke(account.account_id, parts[0]) if parts[0] in KINDS
                                else c.remove(account.account_id, parts[0])})
        elif method == "PATCH" and len(parts) == 1 and parts[0] not in KINDS:
            handler._json(200, c.patch(account, parts[0], handler._read_json_body()))
        elif method == "POST" and len(parts) == 2 and parts[1] == "grants":
            body = handler._read_json_body()
            status, out = c.grant(account, parts[0], str(body.get("account_id") or ""), str(body.get("level") or ""))
            handler._json(status, out)
        elif method == "DELETE" and len(parts) == 3 and parts[1] == "grants":
            handler._json(200, {"revoked": c.revoke_grant(account, parts[0], parts[2])})
        else:
            return False
    except ConnectorError as e:
        handler._json(STATUS.get(e.code, 500), {"error": e.code, "message": e.message})
    return True


def multi() -> bool:
    """`features.connectors_multi` de /v1/capabilities : le contrat 068 est servi (sinon, cartes par nature)."""
    return _connectors is not None and _connectors.multi


def service() -> Connectors | None:
    return _connectors


def handle_public(handler: Any, method: str, path: str) -> bool:
    """`GET /v1/connectors/oauth/callback` : PUBLIC (le navigateur revient du fournisseur sans session) ; c'est le
    `state` signé, à usage unique, qui lie au profil. Rend toujours un 302 vers le web, jamais un jeton."""
    from urllib.parse import parse_qs, urlparse

    if method != "GET" or urlparse(path).path.rstrip("/") != CALLBACK:
        return False
    c = _connectors
    oauth = getattr(c, "oauth", None) if c is not None else None
    if oauth is None:
        handler._json(404, {"error": "provider_off"})
        return True
    params = {k: v[-1] for k, v in parse_qs(urlparse(path).query).items()}
    location = oauth.callback(params)
    handler.send_response(302)
    handler.send_header("Location", location)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")  # le code d'autorisation ne part pas vers le web
    handler.end_headers()
    return True
