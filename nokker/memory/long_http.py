"""Routes de la mémoire longue multi-locataire (docs/MEMORY-TENANCY.md §6), drapeau `tenant_long_memory` (sinon 404
comme avant). nokker.ai seulement : session du compte (`us_…`), locataire fixé par `_account_route`.

    GET    /v1/memory/long[?scope=user|household]      → {household: {id, payer, profile} | null, notes: [note]}
    POST   /v1/memory/long/notes {scope, kind?, title, text, audience?}
                                                       → 201 {note} | 200 {note, duplicate: true}
                                                         | 202 {pending: {id, status, scope}}  (membre → foyer)
    DELETE /v1/memory/long/notes/{nom}                 → {deleted: true}
    POST   /v1/memory/long/notes/{nom}/audience {audience: all|adults}  → {note}   (payeur)
    GET    /v1/memory/long/export                      → {user: [note], household_authored: [note]}

`note` = {name, scope, kind, title, text, audience, created_at, updated_at, mine, household_id?}. Une proposition d'un
membre au foyer se décide par les routes existantes de la quarantaine (`/v1/memory/pending*`, côté payeur).
Erreurs `{"error": {"code", "message"}}` : 400 corps, 403 interdit, 404 inconnu ou éteint, 409 relecture éteinte."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.memory.tenant_long import LongMemory, LongMemoryError

_NOTE = re.compile(r"^/v1/memory/long/notes/([a-z0-9][a-z0-9-]{1,120})(/audience)?$")

_service: LongMemory | None = None


def configure(service: LongMemory | None) -> None:
    global _service
    _service = service


def enabled() -> bool:
    return _service is not None


def service() -> LongMemory | None:
    return _service


def is_long_memory_path(path: str) -> bool:
    route = path.split("?", 1)[0].rstrip("/")
    return route == "/v1/memory/long" or route.startswith("/v1/memory/long/")


def _err(status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"error": {"code": code, "message": message}}


def dispatch(method: str, full_path: str, account_id: str, body: Any) -> tuple[int, dict[str, Any]] | None:
    svc = _service
    split = urlsplit(full_path)
    path, q = split.path.rstrip("/"), parse_qs(split.query)
    if svc is None or not is_long_memory_path(path):
        return None
    try:
        if path == "/v1/memory/long" and method == "GET":
            scope = (q.get("scope") or [None])[0]
            if scope not in (None, "user", "household"):
                return _err(400, "bad_query", "scope : user | household")
            return 200, svc.listing(account_id, scope)
        if path == "/v1/memory/long/export" and method == "GET":
            return 200, svc.export(account_id)
        if path == "/v1/memory/long/notes" and method == "POST":
            data = body()
            if not isinstance(data, dict):
                return _err(400, "bad_body", "corps {scope, kind?, title, text, audience?}")
            out = svc.write(account_id, scope=str(data.get("scope") or "user"), kind=str(data.get("kind") or "fact"),
                            title=str(data.get("title") or ""), text=str(data.get("text") or ""),
                            audience=str(data.get("audience") or "all"))
            return (202 if "pending" in out else 200 if out.get("duplicate") else 201), out
        m = _NOTE.match(path)
        if m and not m.group(2) and method == "DELETE":
            return 200, {"deleted": svc.delete(account_id, m.group(1))}
        if m and m.group(2) and method == "POST":
            data = body()
            if not isinstance(data, dict) or not isinstance(data.get("audience"), str):
                return _err(400, "bad_body", "corps {audience: all | adults}")
            return 200, {"note": svc.set_audience(account_id, m.group(1), data["audience"])}
    except LongMemoryError as e:
        return _err(e.status, e.code, e.message)
    return _err(404, "not_found", "not found")


def handle(h: Any, method: str, path: str, account: Any) -> bool:
    """nokker.ai : dans `_account_route` (session vérifiée, locataire fixé)."""
    if _service is None or not is_long_memory_path(path):
        return False
    out = dispatch(method, getattr(h, "path", None) or path, account.account_id, h._read_json_body)
    h._json(*(out or _err(404, "not_found", "not found")))
    return True


__all__ = ["configure", "dispatch", "enabled", "handle", "is_long_memory_path", "service"]
