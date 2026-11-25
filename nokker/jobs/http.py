"""Routes du pack Emploi (écran Packs → Emploi, docs/JOBS-PACK.md §8). Compte résolu par la frontière de l'API ; tout se
passe dans le locataire du compte.

    GET    /v1/jobs/profile                 → {active, id, has_cv, cv_name, erase_after}
    POST   /v1/jobs/profile                 → idem          {active: bool} ; désactiver = effacement à J+30
    POST   /v1/jobs/uploads                 → 201 {name}    {name, mime, content_base64} ; CV partagé dans le fil (≤ 5 Mo)
    GET    /v1/jobs/docs/{cvl_…|lt_…}.pdf   → PDF d'une version retouchée
    GET    /v1/jobs/report/{AAAA-MM}.csv    → relevé au format job-room (CSV)
    GET    /v1/jobs/cv/{cv_id}.pdf          → PDF d'une version du service (URL signée relue à chaque fois, jamais montrée)
    GET    /v1/jobs/prep/{prep_id}/{NN}.html → fichier d'une préparation (HTML dans un bac à sable, URL signée)

Drapeau `jobs_pack` éteint : `configure(None)`, aucune route (404 du serveur). Jamais le nom du service emploi dans une
réponse."""
from __future__ import annotations

import base64
import binascii
import re
import threading
import time
from collections import defaultdict, deque
from typing import Any

PREFIX = "/v1/jobs"
RATE_MAX, RATE_WINDOW_S = 20, 60.0

_svc: Any = None
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def configure(source: Any) -> None:
    global _svc
    _svc = source
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


def _owner_profile(account: Any) -> Any:
    p = _svc.store.profile_of(account.account_id)
    return p if p is not None and p.active and p.partner_account_id else None


def _send(h: Any, body: bytes, media: str, filename: str, inline: bool = False) -> None:
    h.send_response(200)
    h.send_header("Content-Type", media)
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
    h.send_header("Content-Disposition", f"{'inline' if inline else 'attachment'}; filename=\"{filename}\"")
    h.end_headers()
    h.wfile.write(body)


def _err(h: Any, status: int, code: str, message: str) -> bool:
    h._json(status, {"error": {"code": code, "message": message}})
    return True


def handle(h: Any, method: str, path: str, account: Any) -> bool:
    from nokker.jobs.source import Attachment, JobsFail, report_csv

    route = path.split("?", 1)[0].rstrip("/")
    if _svc is None or not (route == PREFIX or route.startswith(PREFIX + "/")):
        return False
    parts = [x for x in route[len(PREFIX):].split("/") if x]
    if method != "GET" and not _rate_ok(account.account_id):
        return _err(h, 429, "rate", "trop d'écritures, réessayer dans une minute")
    try:
        if parts == ["profile"] and method == "GET":
            h._json(200, _svc.profile_status())
        elif parts == ["profile"] and method == "POST":
            body = h._read_json_body()
            if not isinstance(body.get("active"), bool):
                return _err(h, 400, "bad_request", "active (booléen) requis")
            h._json(200, _svc.set_active(body["active"]))
        elif parts == ["uploads"] and method == "POST":
            from nokker.tenancy.context import current_tenant

            body = h._read_json_body()
            name = " ".join(str(body.get("name") or "").split())[:200]
            if not name or "/" in name or "\\" in name:
                return _err(h, 400, "bad_request", "name requis, sans chemin")
            try:
                data = base64.b64decode(str(body.get("content_base64") or ""), validate=True)
            except (binascii.Error, ValueError):
                return _err(h, 400, "bad_request", "content_base64 illisible")
            if _svc.uploads is None:
                return _err(h, 404, "not_found", "dépôt de fichiers absent")
            try:
                _svc.uploads.put(current_tenant(), Attachment(name, str(body.get("mime") or ""), data))
            except ValueError as exc:
                return _err(h, 400, "bad_request", str(exc))
            h._json(201, {"name": name})
        elif len(parts) == 2 and parts[0] == "docs" and method == "GET" and parts[1].endswith(".pdf"):
            doc = _svc.store.doc(parts[1][:-4])
            mine = doc is not None and _svc.store.profile_of(account.account_id) is not None \
                and _svc.store.profile_of(account.account_id).id == doc.profile_id
            if not mine or not doc.pdf:
                return _err(h, 404, "not_found", "version introuvable")
            _send(h, doc.pdf, "application/pdf", re.sub(r"[^A-Za-z0-9_.-]", "_", doc.data.get("pdf_name") or
                                                        f"{doc.id}.pdf"))
        elif len(parts) == 2 and parts[0] == "report" and method == "GET" and parts[1].endswith(".csv"):
            month = parts[1][:-4]
            if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
                return _err(h, 400, "bad_request", "mois AAAA-MM")
            p = _svc.store.profile_of(account.account_id)
            if p is None or not p.active or not p.partner_account_id:
                return _err(h, 404, "not_found", "aucun profil emploi actif avec CV")
            r = _svc.client.request("GET", f"/orp/{month}", account=p.partner_account_id)
            _svc._debit(account.account_id, r.usage)
            _send(h, report_csv(r.data or {}).encode("utf-8"), "text/csv; charset=utf-8", f"releve-{month}.csv")
        elif len(parts) == 2 and parts[0] == "cv" and method == "GET" and parts[1].endswith(".pdf"):
            p = _owner_profile(account)
            cv_id = parts[1][:-4]
            if p is None or not re.fullmatch(r"[0-9a-f-]{8,64}", cv_id):
                return _err(h, 404, "not_found", "CV introuvable")
            meta = _svc.client.request("GET", f"/cv/{cv_id}", account=p.partner_account_id)
            pdf = _svc.client.fetch_signed(str((meta.data or {}).get("pdf_url") or ""))
            _svc._debit(account.account_id, meta.usage + pdf.usage)
            _send(h, pdf.content, "application/pdf", "CV.pdf")
        elif len(parts) == 3 and parts[0] == "prep" and method == "GET" and re.fullmatch(r"\d\d\.html", parts[2]):
            p = _owner_profile(account)
            if p is None or not re.fullmatch(r"[0-9a-f-]{8,64}", parts[1]):
                return _err(h, 404, "not_found", "préparation introuvable")
            meta = _svc.client.request("GET", f"/prep/{parts[1]}", account=p.partner_account_id)
            url = ((meta.data or {}).get("files") or {}).get(parts[2][:2])
            if not url:
                return _err(h, 404, "not_found", "fichier introuvable")
            f = _svc.client.fetch_signed(str(url))
            _svc._debit(account.account_id, meta.usage + f.usage)
            _send(h, f.content, "text/html; charset=utf-8", f"preparation-{parts[2]}", inline=True)
        else:
            return False
    except JobsFail as exc:
        return _err(h, 409, exc.text.split(" : ", 1)[0], exc.text)
    except Exception as exc:  # noqa: BLE001
        from nokker.jobs.client import PartnerError

        if isinstance(exc, PartnerError):
            if exc.status == 404 or exc.code == "not_found":
                return _err(h, 404, "not_found", "introuvable")
            return _err(h, 424, "service_indisponible", "le service emploi ne répond pas")
        raise
    return True


__all__ = ["PREFIX", "configure", "handle"]
