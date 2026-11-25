"""Routes du BYOK cloud (docs/CLOUD-BYOK.md §2), montées par `_account_route` du serveur (session vérifiée, compte
`us_…`, `tenant_scope` du locataire de l'appelant). Éteint (`cloud_byok.enabled` faux) : ces chemins répondent comme
n'importe quel chemin inconnu (404).

    GET  /v1/account/models          → 200 {default, effective, byok: [{name, base_url, model, has_key, ok, models?}],
                                             entitled}
    PUT  /v1/account/models          {default?, byok?: [{name, base_url, model?, api_key?}]} → 200 (forme du GET)
                                       | 400 {error: {code, message}} | 403 {error: {code: "plan_required"}} | 503
    POST /v1/account/models/test     {base_url, api_key?, model?} → 200 {ok, models, latency_ms, error?, model_listed?}
                                       | 400 | 403 plan_required
"""
from __future__ import annotations

import json
import logging
from typing import Any

from nokker.byok.service import ByokError, CloudByok

logger = logging.getLogger("nokker.byok")

PATHS = ("/v1/account/models", "/v1/account/models/test")
MAX_BODY = 64 * 1024

_service: CloudByok | None = None


def configure(service: CloudByok | None) -> None:
    global _service
    _service = service


def service() -> CloudByok | None:
    return _service


def matches(path: str) -> bool:
    return _service is not None and path in PATHS


def _body(handler: Any) -> Any:
    length = int(handler.headers.get("Content-Length", 0) or 0)
    if length > MAX_BODY:
        raise ByokError(413, "too_large", f"corps de {MAX_BODY // 1024} Kio au plus")
    raw = handler.rfile.read(length) if length > 0 else b""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ByokError(400, "bad_json", "JSON illisible") from None


def route(handler: Any, method: str, path: str) -> bool:
    """True si la requête a reçu sa réponse. Appelée DANS le `tenant_scope` du compte de l'appelant."""
    svc = _service
    if svc is None or path not in PATHS:
        return False
    try:
        if path == "/v1/account/models" and method == "GET":
            handler._json(200, svc.view())
        elif path == "/v1/account/models" and method == "PUT":
            handler._json(200, svc.put(_body(handler)))
        elif path == "/v1/account/models/test" and method == "POST":
            handler._json(200, svc.test(_body(handler)))
        else:
            handler._json(405, {"error": {"code": "method_not_allowed", "message": "méthode non permise"}})
    except ByokError as e:
        handler._json(e.status, e.body())
    return True


__all__ = ["configure", "matches", "route", "service"]
