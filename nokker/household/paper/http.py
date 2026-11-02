"""Routes des papiers du foyer sur nokker.ai (drapeau `household_paper`, docs/HOUSEHOLD-PAPER.md §7). Session du
compte (`us_…`), dans SON locataire ; erreurs `{"error": {"code", "message"}}` ; 60 écritures par minute et par compte.

    POST   /v1/paper/documents {filename, media_type?, content_base64, qr_payload?, person?}  → 201 {document, created}
    GET    /v1/paper/documents?kind=&year=&person=      → {documents: [...]}
    GET    /v1/paper/documents/{id}                     → {document, bill?, tax?, text}
    GET    /v1/paper/documents/{id}/content             → le fichier (téléchargement)
    PATCH  /v1/paper/documents/{id} {kind?, person?, year?, title?}   → {document}
    DELETE /v1/paper/documents/{id}                     → {deleted}
    GET    /v1/paper/settings   PUT {debtor_name?, debtor_iban?, debtor_bic?, people?, tax_sources?}  (IBAN masqué)
    GET    /v1/paper/exports                            → {exports: [...]} (fichiers pain.001 et .ics)
    GET    /v1/paper/exports/{id}                       → le fichier, SEULEMENT s'il est `ready` (carte approuvée)
    GET    /v1/paper/tax-report?year=                   → rapport fiscal de l'année

`configure(None)` (drapeau éteint, défaut) : `enabled()` faux, `handle` rend False → 404 comme avant.
"""
from __future__ import annotations

import base64
import binascii
import threading
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.household.paper.service import MAX_BYTES, Paper, PaperError

PREFIX = "/v1/paper"
RATE_MAX, RATE_WINDOW_S = 60, 60.0
MAX_BODY = MAX_BYTES * 4 // 3 + 64 * 1024

_svc: Paper | None = None
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def configure(service: Paper | None) -> None:
    global _svc
    _svc = service
    _hits.clear()


def enabled() -> bool:
    return _svc is not None


def service() -> Paper | None:
    return _svc


def is_paper_path(path: str) -> bool:
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


def _q(h: Any, key: str) -> str | None:
    return (parse_qs(urlsplit(h.path).query).get(key) or [None])[0]


def _send_file(h: Any, body: bytes, media_type: str, filename: str) -> None:
    from urllib.parse import quote

    ascii_name = filename.encode("ascii", "replace").decode().replace("?", "_").replace('"', "") or "fichier"
    h.send_response(200)
    h.send_header("Content-Type", media_type)
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
    h.send_header("Content-Disposition", f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}")
    h.end_headers()
    h.wfile.write(body)


def handle(h: Any, method: str, path: str, account: Any) -> bool:
    svc = _svc
    route = path.split("?", 1)[0].rstrip("/")
    if svc is None or not is_paper_path(route):
        return False
    parts = [x for x in route[len(PREFIX):].split("/") if x]
    if method != "GET" and not _rate_ok(account.account_id):
        h._json(429, {"error": {"code": "rate", "message": "trop d'écritures, réessayer dans une minute"}})
        return True
    try:
        out = _dispatch(svc, h, method, parts)
    except PaperError as e:
        h._json(e.status, {"error": {"code": "not_found" if e.status == 404 else "bad_request", "message": str(e)}})
        return True
    if out is None:
        h._json(404, {"error": {"code": "not_found", "message": "not found"}})
    elif out != "sent":
        h._json(*out)
    return True


def _dispatch(svc: Paper, h: Any, method: str, parts: list[str]) -> Any:
    if parts == ["documents"] and method == "POST":
        length = int(h.headers.get("Content-Length", 0) or 0)
        if length > MAX_BODY:
            raise PaperError("fichier trop volumineux", 413)
        body = h._read_json_body()
        body = body if isinstance(body, dict) else {}
        try:
            content = base64.b64decode(str(body.get("content_base64") or ""), validate=True)
        except (binascii.Error, ValueError):
            raise PaperError("content_base64 illisible") from None
        doc, created = svc.import_document(str(body.get("filename") or ""), str(body.get("media_type") or ""),
                                           content, qr_payload=body.get("qr_payload") or None,
                                           person=body.get("person") or None)
        return (201 if created else 200), {"document": svc.brief(doc), "created": created}
    if parts == ["documents"] and method == "GET":
        year = _q(h, "year")
        docs = svc.documents(kind=_q(h, "kind"), year=int(year) if year and year.isdigit() else None,
                             person=_q(h, "person"))
        return 200, {"documents": [svc.brief(d) for d in docs]}
    if len(parts) == 2 and parts[0] == "documents":
        if method == "GET":
            doc = svc.document(parts[1])
            bill = svc.bill(doc)
            return 200, {"document": svc.brief(doc), "bill": bill.to_json() if bill else None, "tax": doc.tax,
                         "text": doc.text}
        if method == "PATCH":
            body = h._read_json_body()
            body = body if isinstance(body, dict) else {}
            doc = svc.file(parts[1], kind=body.get("kind"), person=body.get("person"), year=body.get("year"),
                           title=body.get("title"))
            return 200, {"document": svc.brief(doc)}
        if method == "DELETE":
            return 200, {"deleted": svc.delete(parts[1])}
    if len(parts) == 3 and parts[0] == "documents" and parts[2] == "content" and method == "GET":
        doc, content = svc.content(parts[1])
        _send_file(h, content, doc.media_type, doc.filename)
        return "sent"
    if parts == ["settings"]:
        if method == "GET":
            return 200, svc.settings_json(svc.settings())
        if method == "PUT":
            body = h._read_json_body()
            return 200, svc.settings_json(svc.put_settings(body if isinstance(body, dict) else {}))
    if parts == ["exports"] and method == "GET":
        return 200, {"exports": [{"id": e.export_id, "kind": e.kind, "status": e.status, "sha256": e.sha256,
                                  "created_at": e.created_at, **{k: v for k, v in e.summary.items()
                                                                 if k in ("count", "totals", "execution_date",
                                                                          "events")}}
                                 for e in svc.exports()]}
    if len(parts) == 2 and parts[0] == "exports" and method == "GET":
        e = svc.export(parts[1])
        if e.status != "ready":
            raise PaperError("fichier en attente de la carte d'approbation", 409)
        if e.kind == "pain001":
            _send_file(h, e.content.encode("utf-8"), "application/xml", f"paiements-{e.export_id}.xml")
        else:
            _send_file(h, e.content.encode("utf-8"), "text/calendar", f"agenda-{e.export_id}.ics")
        return "sent"
    if parts == ["tax-report"] and method == "GET":
        year = _q(h, "year")
        return 200, svc.tax_report(int(year) if year and year.isdigit() else None)
    return None


__all__ = ["PREFIX", "configure", "enabled", "handle", "is_paper_path", "service"]
