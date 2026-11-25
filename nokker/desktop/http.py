"""Routes HTTP du Bureau (docs/DESKTOP.md §1.1).

Nœud (`/v1/desktop/node/*`) : sur la machine seulement (jamais par la passerelle), corps brut lu pour la preuve.
  POST /v1/desktop/node/enroll                 {name, mode, public_key_spki} → 202 {approval_id} (carte forte)
  GET  /v1/desktop/node/enroll/{approval_id}   preuve de la clé inscrite → 202 | 201 {node_id, token} | 403 | 410
  POST /v1/desktop/node/beat                   jeton + preuve, {v: 1, desktop: on|degraded, reason}
  GET  /v1/desktop/node/tasks?wait=25          jeton + preuve → {tasks: [...]}
  POST /v1/desktop/node/tasks/{id}/lease       jeton + preuve
  POST /v1/desktop/node/tasks/{id}/result      jeton + preuve, {status: done|failed, result | error}
Propriétaire (`/v1/local/desktop*`, jeton local, voir `LocalApi.handle`) : GET (nœuds, santé, journal sans contenu),
DELETE /v1/local/desktop/nodes/{id} (retirer un nœud ; sur la machine seulement)."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.desktop.proof import PROOF_HEADER
from nokker.desktop.service import DesktopError, DesktopService, err

PREFIX = "/v1/desktop/node/"
_RE_ENROLL = re.compile(r"^/v1/desktop/node/enroll/([A-Za-z0-9_-]{1,80})$")
_RE_TASK = re.compile(r"^/v1/desktop/node/tasks/(dt_[0-9a-f]{16})/(lease|result)$")
RAW_MAX = 70 * 1024


def node_route(h: Any, svc: DesktopService | None, method: str, relayed: bool) -> None:
    """Sert une route `/v1/desktop/node/*` sur le gestionnaire HTTP `h` (`headers`, `rfile`, `path`, `_json`)."""
    if svc is None:
        h._json(404, err("not_found", "bureau désactivé (local.desktop.enabled)"))
        return
    if relayed:
        h._json(403, err("local_only", "sur la machine seulement"))
        return
    n = int(h.headers.get("Content-Length") or 0)
    if n > RAW_MAX:
        h._json(413, err("too_large"))
        return
    raw = h.rfile.read(n) if n > 0 else b""
    route = urlsplit(h.path).path
    host = h.headers.get("Host") or ""
    proof = h.headers.get(PROOF_HEADER)
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        h._json(400, err("bad_json", "corps JSON attendu"))
        return
    try:
        if route == "/v1/desktop/node/enroll" and method == "POST":
            h._json(202, svc.enroll(body))
            return
        m = _RE_ENROLL.match(route)
        if m and method == "GET":
            h._json(*svc.pickup(m.group(1), method, host, h.path, raw, proof))
            return
        node = svc.authenticate(h.headers.get("Authorization"), method, host, h.path, raw, proof)
        if route == "/v1/desktop/node/beat" and method == "POST":
            h._json(200, svc.beat(node, body))
            return
        if route == "/v1/desktop/node/tasks" and method == "GET":
            q = parse_qs(urlsplit(h.path).query)
            try:
                wait = float((q.get("wait") or ["0"])[0])
            except ValueError:
                wait = 0.0
            h._json(200, {"tasks": svc.poll(node, wait)})
            return
        mt = _RE_TASK.match(route)
        if mt and method == "POST":
            if mt.group(2) == "lease":
                h._json(200, svc.lease(node, mt.group(1)))
            else:
                h._json(200, svc.result(node, mt.group(1), body, len(raw)))
            return
        h._json(404, err("not_found"))
    except DesktopError as e:
        h._json(e.status, err(e.code, e.message))


def owner_route(svc: DesktopService | None, method: str, path: str, relayed: bool) -> tuple[int, dict[str, Any]]:
    """`/v1/local/desktop` (GET) et `/v1/local/desktop/nodes/{id}` (DELETE) pour le propriétaire."""
    if svc is None:
        return 404, err("disabled", "bureau désactivé (local.desktop.enabled)")
    bare = path.split("?", 1)[0].rstrip("/")
    if bare == "/v1/local/desktop" and method == "GET":
        return 200, svc.view()
    m = re.match(r"^/v1/local/desktop/nodes/(dk_[0-9a-f]{16})$", bare)
    if m and method == "DELETE":
        if relayed:
            return 403, err("local_only", "sur la machine seulement (jeton local)")
        return (200, {"revoked": True}) if svc.revoke(m.group(1)) else (404, err("not_found"))
    return 404, err("not_found")


__all__ = ["PREFIX", "node_route", "owner_route"]
