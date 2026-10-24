"""Console opérateur, lot A0 : second écouteur du processus (bibliothèque standard, comme `api/server.py`).

Routes (toutes sauf `/admin/healthz` exigent un jeton Cloudflare Access vérifié ET un e-mail présent dans
`operators`) :
  GET /admin/            interface (HTML/JS natif, `nokker/admin/ui/index.html`)
  GET /admin/v1/me       e-mail et rôle de l'opérateur
  GET /admin/v1/applications?statut=&q=&limit=&offset=   liste
  GET /admin/v1/applications/<id>                        fiche (journalisée : application.view)
  GET /admin/v1/stats    compteurs (profils, langues, « nous a connus par », jours)
  GET /admin/v1/audit    journal (owner seulement)
  GET  /admin/v1/applications/<id>/invitations          invitations de la candidature (front1)
  GET  /admin/v1/applications/<id>/invite/preview?lang=  aperçu du mail (front1, sans envoi)
  POST /admin/v1/applications/<id>/invite {lang}        ENVOI (ops/owner, en-tête X-Nokker-Console) ; audit après l'appel
Le rôle `lecteur` ne reçoit jamais une adresse e-mail ni un nom de famille en clair (espace-admin §6).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from nokker.admin.access import AccessDenied, AccessVerifier
from nokker.admin.store import AdminStore

logger = logging.getLogger("nokker.admin.server")

UI_DIR = Path(__file__).resolve().parent / "ui"
_APP_RE = re.compile(r"^/admin/v1/applications/(\d+)$")
_INVITE_RE = re.compile(r"^/admin/v1/applications/(\d+)/(invitations|invite/preview|invite)$")
WRITE_ROLES = ("owner", "ops")


def mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def for_role(app: dict[str, Any], role: str) -> dict[str, Any]:
    out = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in app.items()}
    if role == "lecteur":
        out["email"] = mask_email(str(app.get("email", "")))
        out["nom"] = (str(app.get("nom") or "")[:1] + ".") if app.get("nom") else ""
        out["referrer"] = ""
    return out


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(type(obj).__name__)


def make_admin_handler(store: AdminStore, verifier: AccessVerifier, front: Any = None) -> type[BaseHTTPRequestHandler]:
    """`front` : client des invitations de front1 (`nokker/onboarding/front.py::FrontInvitations`) ; None = pas
    d'invitation depuis cette console."""
    class AdminHandler(BaseHTTPRequestHandler):
        server_version = "nokker-admin/0"

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.info("admin %s", fmt % args)

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False, default=_jsonable).encode(), "application/json")

        def _operator(self) -> tuple[str, str] | None:
            """(e-mail, rôle) d'un jeton Access valide pour un opérateur connu ; sinon répond 401/403 et None."""
            token = self.headers.get("Cf-Access-Jwt-Assertion", "")
            if not token:  # le navigateur passe aussi le cookie CF_Authorization
                for part in self.headers.get("Cookie", "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "CF_Authorization":
                        token = v
            try:
                claims = verifier.verify(token)
            except AccessDenied as e:
                self._json(401, {"error": "access", "detail": str(e)})
                return None
            email = str(claims["email"]).strip().lower()
            role = store.role_of(email)
            if role is None:
                logger.warning("console : e-mail authentifié mais pas opérateur")
                self._json(403, {"error": "not_operator"})
                return None
            return email, role

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/admin/healthz":
                self._json(200, {"ok": True})
                return
            if path == "/favicon.ico":  # l'icône est dans la page (SVG inline) : rien à servir, pas de 404 au journal
                self.send_response(204)
                self.end_headers()
                return
            if path in ("/", "/admin"):
                if self._operator() is None:
                    return
                self._send(200, (UI_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
                return
            if not path.startswith("/admin/v1/"):
                self._json(404, {"error": "not_found"})
                return
            op = self._operator()
            if op is None:
                return
            email, role = op
            qs = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if path == "/admin/v1/me":
                    self._json(200, {"email": email, "role": role})
                elif path == "/admin/v1/applications":
                    limit = max(1, min(500, int(qs.get("limit", 100))))
                    items, total = store.list_applications(statut=qs.get("statut", ""), q=qs.get("q", "")[:200],
                                                           limit=limit, offset=max(0, int(qs.get("offset", 0))))
                    self._json(200, {"total": total, "items": [for_role(a, role) for a in items]})
                elif m := _INVITE_RE.match(path):
                    self._invite_get(int(m.group(1)), m.group(2), role, qs)
                elif m := _APP_RE.match(path):
                    app = store.view_application(int(m.group(1)), email)
                    if app is None:
                        self._json(404, {"error": "not_found"})
                    else:
                        self._json(200, for_role(app, role))
                elif path == "/admin/v1/stats":
                    self._json(200, store.stats())
                elif path == "/admin/v1/audit":
                    if role != "owner":
                        self._json(403, {"error": "role"})
                    else:
                        self._json(200, {"items": store.audit(limit=max(1, min(500, int(qs.get("limit", 100)))))})
                else:
                    self._json(404, {"error": "not_found"})
            except ValueError:
                self._json(400, {"error": "bad_request"})
            except Exception:  # noqa: BLE001
                logger.exception("console : %s en échec", path)
                self._json(500, {"error": "internal"})

        def _front_app(self, app_id: int, role: str) -> dict[str, Any] | None:
            """Candidature venue de front1, opérateur qui peut agir ; sinon répond et None."""
            if role not in WRITE_ROLES:
                self._json(403, {"error": "role"})
                return None
            if front is None:
                self._json(503, {"error": "front_unconfigured"})
                return None
            app = store.application(app_id)
            if app is None or app.get("source") != "front1":
                self._json(404, {"error": "not_found"})
                return None
            return app

        def _invite_get(self, app_id: int, what: str, role: str, qs: dict[str, str]) -> None:
            from nokker.onboarding.front import FrontError

            app = self._front_app(app_id, role)
            if app is None:
                return
            try:
                if what == "invitations":
                    self._json(200, {"items": front.invitations(int(app["source_id"]))})
                elif what == "invite/preview":
                    self._json(200, front.preview(int(app["source_id"]), qs.get("lang") or None))
                else:
                    self._json(405, {"error": "method"})
            except FrontError as e:
                self._json(502 if e.code in ("unavailable", "unauthorized") else 409, {"error": e.code})

        def do_POST(self) -> None:  # noqa: N802
            from nokker.onboarding.front import FrontError

            path = urlparse(self.path).path.rstrip("/")
            m = _INVITE_RE.match(path)
            if not m or m.group(2) != "invite":
                self._json(404, {"error": "not_found"})
                return
            # Une page tierce ne peut pas poser cet en-tête sans pré-vol CORS, refusé : pas de requête forgée avec le
            # cookie Access de l'opérateur.
            if self.headers.get("X-Nokker-Console") != "1":
                self._json(403, {"error": "csrf"})
                return
            op = self._operator()
            if op is None:
                return
            email, role = op
            app = self._front_app(int(m.group(1)), role)
            if app is None:
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except json.JSONDecodeError:
                body = {}
            lang = body.get("lang") if body.get("lang") in ("fr", "en") else None
            obj = f"application:{app['id']}"
            try:
                sent = front.send(int(app["source_id"]), lang, email)
            except FrontError as e:
                store.record(email, "invitation.send_failed", obj, {"code": e.code})
                self._json(502 if e.code in ("unavailable", "unauthorized", "send_failed") else 409, {"error": e.code})
                return
            store.record(email, "invitation.sent", obj, sent)
            self._json(200, sent)

    return AdminHandler


def serve_admin(store: AdminStore, verifier: AccessVerifier, host: str, port: int, front: Any = None) -> ThreadingHTTPServer:
    """Démarre l'écouteur dans un thread (le processus principal garde l'API du noyau)."""
    import threading

    httpd = ThreadingHTTPServer((host, port), make_admin_handler(store, verifier, front))
    threading.Thread(target=httpd.serve_forever, name="nokker-admin", daemon=True).start()
    logger.info("console opérateur sur %s:%d", host, port)
    return httpd


__all__ = ["for_role", "make_admin_handler", "mask_email", "serve_admin"]
