"""Routes HTTP de l'ambiant (docs/AMBIENT.md §5), accrochées par `api/server.py`. Drapeau éteint (défaut) : `enabled()`
est faux, aucune route n'existe (404 du serveur), rien ne change.

Membre du foyer (session web ; en Lokkal, jeton local du propriétaire) :
  GET    /v1/ambient/displays            écrans du foyer
  POST   /v1/ambient/displays            enregistrer un écran (adulte) → {display, token} (jeton montré une fois)
  DELETE /v1/ambient/displays/{id}       révoquer (adulte) : le jeton est refusé tout de suite
  GET    /v1/ambient/board               tableau commun du foyer
  PUT    /v1/ambient/board               l'écrire (tout membre sauf profil enfant)
  POST   /v1/ambient/guests              {on, hours} : mode « invités présents » (voix de la pièce)

Appareils (sans session, le jeton d'appareil fait foi ; 401 s'il est inconnu ou révoqué) :
  GET  /v1/eink/{jeton}.png              image du foyer (Kindle, cadre générique : « simple URL d'image »)
  GET  /v1/eink/trmnl/api/setup          appairage TRMNL (en-tête ID = MAC)
  GET  /v1/eink/trmnl/api/display        protocole TRMNL (en-tête Access-Token) → {image_url, filename, refresh_rate…}
  POST /v1/eink/trmnl/api/log            journaux de l'appareil : acceptés et jetés (204)
URL de base à donner à un TRMNL : `<adresse publique>/v1/eink/trmnl`.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from nokker.ambient.displays import DisplayError, Displays, Membership

logger = logging.getLogger("nokker.ambient.http")

PREFIX = "/v1/ambient/"
DEVICE_PREFIX = "/v1/eink/"
MAX_BODY = 64 * 1024
_RE_PNG = re.compile(r"^/v1/eink/(nkd_[A-Za-z0-9_-]{32,64})\.png$")
_RE_DISPLAY = re.compile(r"^/v1/ambient/displays/(dp_[0-9a-f]{16})$")
TRMNL = "/v1/eink/trmnl/api/"
GLANCE = "/v1/glance"
_RE_GLANCE_DEVICE = re.compile(r"^/v1/glance/devices/(dp_[0-9a-f]{16})$")


@dataclass
class Ambient:
    displays: Displays | None = None
    room_audio: Any = None  # RoomAudio
    public_url: str = ""  # adresse publique du noyau (image_url TRMNL) ; vide = déduite de Host
    glance: Any = None  # Glance (vue lunettes, lecture seule) ; None = routes /v1/glance* absentes


_state: dict[str, Ambient | None] = {"a": None}


def configure(a: Ambient | None) -> None:
    _state["a"] = a


def service() -> Ambient | None:
    return _state["a"]


def enabled() -> bool:
    a = _state["a"]
    return a is not None and (a.displays is not None or a.room_audio is not None or a.glance is not None)


def is_ambient_path(path: str) -> bool:
    return enabled() and path.startswith(PREFIX)


def is_glance_path(path: str) -> bool:
    a = _state["a"]
    return a is not None and a.glance is not None and (path == GLANCE or path.startswith(GLANCE + "/"))


def is_device_path(path: str) -> bool:
    a = _state["a"]
    return a is not None and a.displays is not None and path.startswith(DEVICE_PREFIX)


def _send(h: Any, status: int, body: bytes, ctype: str = "application/json", extra: dict[str, str] | None = None) -> None:
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    for k, v in (extra or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(body)


def _json(h: Any, status: int, obj: dict[str, Any]) -> None:
    _send(h, status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _err(h: Any, e: DisplayError) -> None:
    _json(h, e.status, {"error": {"code": e.code, "message": e.message}})


def _body(h: Any) -> Any:
    try:
        n = int(h.headers.get("Content-Length", 0) or 0)
    except ValueError:
        return None
    if n < 0 or n > MAX_BODY:
        return None
    raw = h.rfile.read(n) if n else b""
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return None


def _header(h: Any, *names: str) -> str:
    for n in names:
        v = h.headers.get(n)
        if v:
            return str(v).strip()
    return ""


def _base_url(h: Any, a: Ambient) -> str:
    if a.public_url:
        return a.public_url.rstrip("/")
    proto = _header(h, "X-Forwarded-Proto") or "http"
    host = _header(h, "X-Forwarded-Host", "Host") or "localhost"
    return f"{proto if proto in ('http', 'https') else 'http'}://{host}"


# ─────────────────────────── membres ───────────────────────────

def handle_member(h: Any, method: str, path: str, who: Membership | None) -> bool:
    """Routes /v1/ambient/* pour un membre déjà authentifié. `who` None : pas de foyer → 403."""
    a = _state["a"]
    if a is None or not path.startswith(PREFIX):
        return False
    if who is None:
        _json(h, 403, {"error": {"code": "household_required", "message": "réservé aux membres d'un foyer"}})
        return True
    try:
        if path == "/v1/ambient/guests" and method == "POST":
            if a.room_audio is None:
                raise DisplayError(404, "not_found", "voix de la pièce éteinte")
            body = _body(h) or {}
            until = a.room_audio.set_guests(bool(body.get("on")), float(body.get("hours") or 6))
            _json(h, 200, {"guests": bool(body.get("on")), "until": until or None})
            return True
        d = a.displays
        if d is None:
            raise DisplayError(404, "not_found", "écrans éteints")
        if path == "/v1/ambient/displays" and method == "GET":
            _json(h, 200, d.list(who))
        elif path == "/v1/ambient/displays" and method == "POST":
            _json(h, 201, d.register(who, _body(h)))
        elif method == "DELETE" and _RE_DISPLAY.match(path):
            _json(h, 200, d.revoke(who, _RE_DISPLAY.match(path).group(1)))
        elif path == "/v1/ambient/board" and method == "GET":
            _json(h, 200, d.board(who))
        elif path == "/v1/ambient/board" and method == "PUT":
            _json(h, 200, d.put_board(who, _body(h)))
        else:
            raise DisplayError(404, "not_found", "not found")
    except DisplayError as e:
        _err(h, e)
    return True


# ─────────────────────────── appareils ───────────────────────────

def handle_device(h: Any, method: str, path: str) -> bool:
    a = _state["a"]
    if a is None or a.displays is None or not path.startswith(DEVICE_PREFIX):
        return False
    d = a.displays
    m = _RE_PNG.match(path)
    if m and method == "GET":
        disp = d.resolve(m.group(1))
        if disp is None:
            _json(h, 401, {"error": {"code": "bad_device_token", "message": "jeton d'écran inconnu ou révoqué"}})
            return True
        f = d.frame(disp)
        _send(h, 200, f.png, "image/png", {"X-Refresh-Rate": str(f.refresh_s)})
        return True
    if path == TRMNL + "setup" and method == "GET":
        token = d.trmnl_setup(_header(h, "ID"))
        if token is None:
            _json(h, 404, {"status": 404, "api_key": None, "friendly_id": None, "image_url": None,
                           "message": "écran inconnu : déclarez-le d'abord dans l'app (adresse MAC)"})
        else:
            disp = d.resolve(token)
            _json(h, 200, {"status": 200, "api_key": token, "friendly_id": disp.id if disp else "",
                           "image_url": f"{_base_url(h, a)}/v1/eink/{token}.png", "filename": "setup",
                           "message": "Écran relié au foyer"})
        return True
    if path == TRMNL + "display" and method == "GET":
        token = _header(h, "Access-Token", "Access_Token", "ACCESS_TOKEN")
        disp = d.resolve(token)
        if disp is None:
            _json(h, 401, {"status": 401, "error": "jeton d'écran inconnu ou révoqué"})
            return True
        f = d.frame(disp)
        _json(h, 200, {"status": 0, "image_url": f"{_base_url(h, a)}/v1/eink/{token}.png?v={f.filename}",
                       "filename": f.filename, "refresh_rate": f.refresh_s, "reset_firmware": False,
                       "update_firmware": False, "firmware_url": None, "special_function": "none"})
        return True
    if path == TRMNL + "log" and method == "POST":
        token = _header(h, "Access-Token", "Access_Token", "ACCESS_TOKEN")
        _body(h)  # lu et jeté : rien n'est gardé
        if d.resolve(token) is None:
            _json(h, 401, {"status": 401})
        else:
            _send(h, 204, b"")
        return True
    _json(h, 404, {"error": {"message": "not found"}})
    return True


# ─────────────────────────── vue glance (lunettes) ───────────────────────────

def handle_glance_claim(h: Any, method: str, path: str) -> bool:
    """`POST /v1/glance/claim {code}` : public (les lunettes n'ont encore rien) ; le code fait foi, une fois."""
    a = _state["a"]
    if a is None or a.glance is None or path != GLANCE + "/claim":
        return False
    if method != "POST":
        _json(h, 405, {"error": {"code": "method_not_allowed", "message": "POST attendu"}})
        return True
    body = _body(h) or {}
    try:
        _json(h, 201, a.glance.claim(str(body.get("code") or "") if isinstance(body, dict) else ""))
    except DisplayError as e:
        _err(h, e)
    return True


def handle_glance_owner(h: Any, method: str, path: str, account_id: str, tenant_id: str,
                        who: Membership | None) -> bool:
    """Routes du titulaire (session du téléphone) : offre d'appairage, liste, révocation de SES vues."""
    a = _state["a"]
    if a is None or a.glance is None or not path.startswith(GLANCE + "/"):
        return False
    try:
        m = _RE_GLANCE_DEVICE.match(path)
        if path == GLANCE + "/pair" and method == "POST":
            body = _body(h) or {}
            name = body.get("name") if isinstance(body, dict) else None
            if who is not None:  # rattachée au foyer : un adulte du foyer peut la révoquer
                out = a.glance.offer(account_id, who.household_tenant, who.household_id, str(name or ""))
            else:
                out = a.glance.offer(account_id, tenant_id, "", str(name or ""))
            _json(h, 201, out)
        elif path == GLANCE + "/devices" and method == "GET":
            _json(h, 200, a.glance.devices(account_id))
        elif m and method == "DELETE":
            _json(h, 200, a.glance.revoke(account_id, m.group(1)))
        else:
            raise DisplayError(404, "not_found", "not found")
    except DisplayError as e:
        _err(h, e)
    return True


__all__ = ["Ambient", "configure", "enabled", "handle_device", "handle_glance_claim", "handle_glance_owner",
           "handle_member", "is_ambient_path", "is_device_path", "is_glance_path", "service"]
