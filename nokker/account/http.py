"""Routes « Mon compte » du noyau qui ne sont pas de l'onboarding (docs/CLIENT-API.md) : export et suppression.

    POST /v1/account/export                  → 202 {id, status, created_at, expires_at, size, download}
                                                429 {error: "export_limit", retry_at} (une demande par 24 h)
    GET  /v1/account/exports/{id}            → 200 même corps | 404
    GET  /v1/account/exports/{id}/download   → 200 application/json (fichier) | 404 | 410 (expiré)
    POST /v1/account/delete                  → 202 {approval_id, strength: "strong", expires_at, reason} : carte
                                                FORTE ; accordée par un appareil fort du compte → compte suspendu,
                                                effacement en moins de 24 h (nokker/account/delete.py)

Appelées par `_account_route` du serveur : session vérifiée, locataire du compte fixé, compte `us_…`.
"""
from __future__ import annotations

import gzip
import re
from typing import Any

from nokker.account import delete
from nokker.account.export import AccountExports, ExportLimit, _iso

_exports: AccountExports | None = None
_approvals: Any = None  # backend des approbations : la carte de suppression
_ID = re.compile(r"^/v1/account/exports/(ex_[0-9a-f]{20})(/download)?$")


def configure(exports: AccountExports | None, approvals: Any = None) -> None:
    global _exports, _approvals
    _exports, _approvals = exports, approvals


def matches(path: str) -> bool:
    return path in ("/v1/account/export", "/v1/account/delete") or path.startswith("/v1/account/exports/")


def route(handler: Any, method: str, path: str, account: Any, tenant_id: str) -> bool:
    if not matches(path):
        return False
    if path == "/v1/account/delete":
        if method != "POST":
            handler._json(404, {"error": {"message": "not found"}})
        elif _approvals is None:
            handler._json(503, {"error": "delete_disabled"})
        else:
            card = delete.request(_approvals, account, tenant_id)
            handler._json(202, {"approval_id": card.id, "strength": card.strength, "expires_at": _iso(card.expires_at),
                                "reason": card.reason})
        return True
    ex = _exports
    if ex is None:
        handler._json(503, {"error": "export_disabled"})
        return True
    if path == "/v1/account/export" and method == "POST":
        try:
            meta = ex.request(account, tenant_id)
        except ExportLimit as e:
            handler._json(429, {"error": "export_limit", "retry_at": _iso(e.retry_at)})
            return True
        handler._json(202, meta.public(ex.clock()))
        return True
    m = _ID.match(path)
    if m is None or method != "GET":
        handler._json(404, {"error": {"message": "not found"}})
        return True
    row = ex.get(m.group(1), account.account_id)
    if row is None:
        handler._json(404, {"error": {"message": "not found"}})
        return True
    meta, body = row
    now = ex.clock()
    if not m.group(2):
        handler._json(200, meta.public(now))
        return True
    if now >= meta.expires_at:
        handler._json(410, {"error": "export_expired"})
        return True
    data = gzip.decompress(body)
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Disposition", f'attachment; filename="nokker-export-{meta.id}.json"')
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)
    return True


__all__ = ["configure", "matches", "route"]
