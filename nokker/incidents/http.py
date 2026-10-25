"""Routes des incidents (lot X18, docs/LOKKAL.md §7.18), derrière le drapeau `operate` (éteint par défaut) :

    from nokker.incidents import http as incidents_http
    incidents_http.configure(Operate(...))              # au démarrage, drapeau allumé seulement
    if incidents_http.handle(self, method, path): return  # dans le dispatcheur du serveur

`configure(None)` : `handle` rend False, le serveur répond comme avant (404).

Sans identité (la signature de la source en tient lieu) :
    POST   /v1/alerts/<as_…>                       X-Nokker-Signature: t=<epoch>,v1=<hex>   → 202
                                                    (source `auth: bearer` : Authorization: Bearer <jeton>)
Identité vérifiée du locataire (comme /v1/agents) :
    GET    /v1/alerts/sources                       POST /v1/alerts/sources {name, format, auth?: hmac|bearer}
                                                    → 201 (clé ou jeton rendu UNE fois)
    POST   /v1/alerts/sources/<id>/secret           DELETE /v1/alerts/sources/<id>
    GET    /v1/incidents?state=open|in_progress|resolved|live&limit=&before=
    GET    /v1/incidents/<in_…>                     POST /v1/incidents/<id>/ack   POST /v1/incidents/<id>/resolve {note?}
Erreurs : `{"error": {"code", "message"}}` ; une source ou un incident d'un autre locataire n'existe pas (404).
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.incidents.service import SIG_HEADER, Operate
from nokker.incidents.types import IncidentError, incident_to_json, source_to_json
from nokker.tenancy.context import tenant_scope

MAX_ALERT_BODY = 256 * 1024
_RE_SIGNED = re.compile(r"^/v1/alerts/(as_[0-9a-f]{12})$")
_RE_SOURCE = re.compile(r"^/v1/alerts/sources/(as_[0-9a-f]{12})(?:/(secret))?$")
_RE_INCIDENT = re.compile(r"^/v1/incidents/(in_[0-9a-f]{12})(?:/(ack|resolve))?$")

_operate: Operate | None = None


def configure(operate: Operate | None) -> None:
    global _operate
    _operate = operate


def service() -> Operate | None:
    return _operate


def is_path(path: str) -> bool:
    return path in ("/v1/incidents", "/v1/alerts/sources") or path.startswith(("/v1/incidents/", "/v1/alerts/"))


def _err(h: Any, status: int, code: str, message: str) -> None:
    h._json(status, {"error": {"code": code, "message": message}})


def handle(h: Any, method: str, path: str) -> bool:
    op = _operate
    if op is None or not is_path(path):
        return False
    m = _RE_SIGNED.match(path)
    if m and method == "POST":
        length = int(h.headers.get("Content-Length", 0) or 0)
        if length > MAX_ALERT_BODY:
            _err(h, 413, "too_large", f"corps au-delà de {MAX_ALERT_BODY // 1024} Kio")
            return True
        raw = h.rfile.read(length) if length else b""
        h._json(*op.receive(m.group(1), raw, h.headers.get(SIG_HEADER), authorization=h.headers.get("Authorization")))
        return True
    body = h._read_json_body() if method in ("POST", "PATCH") else {}
    body = body if isinstance(body, dict) else {}
    sender = h._resolve_identity(body)
    if sender is None:
        _err(h, 401, "unauthorized", "unauthorized")
        return True
    tid = h._tenant_for(sender)
    if tid is None:
        return True
    try:
        with tenant_scope(tid):
            out = _dispatch(op, h, method, path, body, sender)
    except IncidentError as e:
        _err(h, e.status, e.code, e.message)
        return True
    if out is None:
        _err(h, 404, "not_found", "not found")
    else:
        h._json(*out)
    return True


def _secret_reply(s: Any, key: str) -> dict[str, Any]:
    if s.auth == "bearer":  # Alertmanager : `http_config.authorization: {type: Bearer, credentials: <secret>}`
        return {"source": source_to_json(s), "secret": key, "url": f"/v1/alerts/{s.id}", "header": "Authorization",
                "signature": "Bearer <secret>"}
    return {"source": source_to_json(s), "secret": key, "url": f"/v1/alerts/{s.id}", "header": SIG_HEADER,
            "signature": "t=<epoch>,v1=<hex HMAC-SHA256(secret, \"<t>.\" + corps brut)>"}


def _dispatch(op: Operate, h: Any, method: str, path: str, body: dict[str, Any], sender: str
              ) -> tuple[int, Any] | None:
    if path == "/v1/alerts/sources":
        if method == "GET":
            return 200, {"sources": [source_to_json(s) for s in op.list_sources()]}
        if method == "POST":
            s, key = op.create_source(body, sender)
            return 201, _secret_reply(s, key)
        return None
    m = _RE_SOURCE.match(path)
    if m:
        if m.group(2) == "secret" and method == "POST":
            s, key = op.rotate_secret(m.group(1))
            return 200, _secret_reply(s, key)
        if m.group(2) is None and method == "DELETE":
            op.delete_source(m.group(1))
            return 200, {"deleted": True}
        return None
    if path == "/v1/incidents" and method == "GET":
        q = parse_qs(urlsplit(h.path).query)
        try:
            limit = int((q.get("limit") or ["50"])[0])
        except ValueError:
            raise IncidentError("limit : entier attendu") from None
        page, nxt = op.list(state=(q.get("state") or [None])[0], limit=limit, before=(q.get("before") or [None])[0])
        return 200, {"incidents": [incident_to_json(i, timeline=False) for i in page], "next_before": nxt}
    m = _RE_INCIDENT.match(path)
    if m:
        iid, verb = m.group(1), m.group(2)
        if verb is None and method == "GET":
            return 200, incident_to_json(op.get(iid))
        if verb == "ack" and method == "POST":
            return 200, incident_to_json(op.ack(iid, sender))
        if verb == "resolve" and method == "POST":
            return 200, incident_to_json(op.resolve(iid, sender, str(body.get("note") or "")))
    return None


__all__ = ["MAX_ALERT_BODY", "configure", "handle", "is_path", "service"]
