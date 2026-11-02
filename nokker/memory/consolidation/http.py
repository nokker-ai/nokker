"""Routes de la consolidation nocturne (docs/CONSOLIDATION.md §6), drapeau `memory_consolidation` (sinon 404 comme
avant). nokker.ai : session du compte (`us_…`), locataire fixé par `_account_route` ; un compte ne voit et ne défait
que SES nuits.

    GET  /v1/memory/consolidation/runs[?limit=]          → {runs: [run]}
    GET  /v1/memory/consolidation/runs/{id}              → {run, ops: [op]}            (le rapport du matin)
    POST /v1/memory/consolidation/runs/{id}/undo         → {run, undone, already, conflicts, complete}
    POST /v1/memory/consolidation/ops/{id}/undo          → {op, undone, already}        (409 conflict)
    POST /v1/memory/consolidation/run {dry_run?: bool}   → {run, ops} | 409 busy
    GET  /v1/memory/consolidation/settings               PUT {enabled?, tz?, cron?} → {enabled, tz, cron, max_ops}
    GET  /v1/memory/consolidation/pins                   PUT /v1/memory/consolidation/pins/{nom} {pinned: bool}

Erreurs `{"error": {"code", "message"}}`."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.memory.consolidation.engine import ConsolidationError, Consolidator

PREFIX = "/v1/memory/consolidation"
_RUN = re.compile(r"^/v1/memory/consolidation/runs/(cr_[0-9a-f]{16})(/undo)?$")
_OP = re.compile(r"^/v1/memory/consolidation/ops/(co_[0-9a-f]{16})/undo$")
_PIN = re.compile(r"^/v1/memory/consolidation/pins/([a-z0-9][a-z0-9-]{1,140})$")

_service: Consolidator | None = None


def configure(service: Consolidator | None) -> None:
    global _service
    _service = service


def enabled() -> bool:
    return _service is not None


def service() -> Consolidator | None:
    return _service


def is_consolidation_path(path: str) -> bool:
    route = path.split("?", 1)[0].rstrip("/")
    return route == PREFIX or route.startswith(PREFIX + "/")


def _err(status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"error": {"code": code, "message": message}}


def dispatch(method: str, full_path: str, account_id: str, body: Any) -> tuple[int, dict[str, Any]] | None:
    svc = _service
    split = urlsplit(full_path)
    path, q = split.path.rstrip("/"), parse_qs(split.query)
    if svc is None or not is_consolidation_path(path):
        return None
    try:
        if path == PREFIX + "/runs" and method == "GET":
            try:
                limit = int((q.get("limit") or ["30"])[0])
            except ValueError:
                return _err(400, "bad_query", "limit : entier")
            return 200, svc.runs(account_id, limit)
        m = _RUN.match(path)
        if m and not m.group(2) and method == "GET":
            return 200, svc.report(account_id, m.group(1))
        if m and m.group(2) and method == "POST":
            return 200, svc.undo_run(account_id, m.group(1))
        m = _OP.match(path)
        if m and method == "POST":
            return 200, svc.undo_op(account_id, m.group(1))
        if path == PREFIX + "/run" and method == "POST":
            data = body() or {}
            if not isinstance(data, dict) or not isinstance(data.get("dry_run", False), bool):
                return _err(400, "bad_body", "corps {dry_run?: bool}")
            out = svc.run(account_id, dry_run=data.get("dry_run", False), trigger="manual",
                          tz=svc.get_settings(account_id)["tz"])
            if out.get("status") == "busy":
                return _err(409, "busy", "une consolidation tourne déjà pour ce compte")
            return 200, out
        if path == PREFIX + "/settings" and method == "GET":
            return 200, svc.get_settings(account_id)
        if path == PREFIX + "/settings" and method == "PUT":
            data = body()
            if not isinstance(data, dict):
                return _err(400, "bad_body", "corps {enabled?, tz?, cron?}")
            return 200, svc.put_settings(account_id, data)
        if path == PREFIX + "/pins" and method == "GET":
            return 200, svc.pins(account_id)
        m = _PIN.match(path)
        if m and method == "PUT":
            data = body()
            if not isinstance(data, dict) or not isinstance(data.get("pinned"), bool):
                return _err(400, "bad_body", "corps {pinned: bool}")
            return 200, svc.pin(account_id, m.group(1), data["pinned"])
    except ConsolidationError as e:
        return _err(e.status, e.code, e.message)
    return _err(404, "not_found", "not found")


def handle(h: Any, method: str, path: str, account: Any) -> bool:
    """nokker.ai : dans `_account_route` (session vérifiée, locataire fixé)."""
    if _service is None or not is_consolidation_path(path):
        return False
    out = dispatch(method, getattr(h, "path", None) or path, account.account_id, h._read_json_body)
    h._json(*(out or _err(404, "not_found", "not found")))
    return True


__all__ = ["configure", "dispatch", "enabled", "handle", "is_consolidation_path", "service"]
