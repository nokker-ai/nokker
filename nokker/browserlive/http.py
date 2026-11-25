"""Routes de la vue navigateur en direct (docs/BROWSER-LIVE.md §3-4). Rien n'est servi sans `live.configure(...)`.

    GET  /v1/browser/frames/<id>.webp?exp=&sig=   public (URL signée 60 s) ; 403 signature, 410 expirée/purgée
    GET  /v1/browser/sessions/<bs_…>              titulaire (identité vérifiée) : état, dernière frame, 20 actions
    POST /v1/browser/sessions/<bs_…>/resume       titulaire seulement, idempotent
    POST /v1/browser/sessions/<bs_…>/narrations/<seq>/speech/timed   titulaire : la désignation `seq`, dite par la voix
                                                                     du titulaire (texte émis par le noyau, jamais le client)
    GET|POST /v1/browser/takeover/<code>          titulaire : code à usage unique → cookie lié (GET : 302 vers l'écran)
    GET  /v1/browser/sessions/<bs_…>/screen       cookie de reprise → 302 vers l'écran (ticket noVNC du pool)
"""
from __future__ import annotations

import re
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.browserlive import live as live_mod
from nokker.browserlive.live import COOKIE_NAME
from nokker.tenancy.context import tenant_scope

PREFIX = "/v1/browser/"
_RE_FRAME = re.compile(r"^/v1/browser/frames/(bf_[A-Za-z0-9_-]{8,40})\.webp$")
_RE_SESSION = re.compile(r"^/v1/browser/sessions/(bs_[0-9a-f]{16})(?:/(resume|screen))?$")
_RE_NARRATION = re.compile(r"^/v1/browser/sessions/(bs_[0-9a-f]{16})/narrations/(\d{1,9})/speech/timed$")
_RE_TAKEOVER = re.compile(r"^/v1/browser/takeover/([A-Za-z0-9_-]{6,64})$")


def is_path(path: str) -> bool:
    return path.startswith(PREFIX)


def _err(h: Any, status: int, code: str, message: str) -> None:
    h._json(status, {"error": {"code": code, "message": message}})


def _no_store(h: Any) -> None:
    h.send_header("Cache-Control", "no-store")
    h.send_header("Referrer-Policy", "no-referrer")


def handle(h: Any, method: str, raw_path: str) -> bool:
    lv = live_mod.live()
    path = urlsplit(raw_path).path
    if lv is None or not is_path(path):
        return False
    m = _RE_FRAME.match(path)
    if m and method == "GET":
        q = parse_qs(urlsplit(raw_path).query)
        status, data = lv.frames.fetch(m.group(1), (q.get("exp") or [None])[0], (q.get("sig") or [None])[0])
        if status != 200 or data is None:
            h.send_response(status)
            _no_store(h)
            h.send_header("Content-Length", "0")
            h.end_headers()
            return True
        h.send_response(200)
        h.send_header("Content-Type", "image/webp")
        h.send_header("Content-Length", str(len(data)))
        _no_store(h)
        h.send_header("X-Content-Type-Options", "nosniff")
        h.end_headers()
        h.wfile.write(data)
        return True
    m = _RE_SESSION.match(path)
    if m and m.group(2) == "screen" and method == "GET":
        jar = SimpleCookie()
        try:
            jar.load(h.headers.get("Cookie") or "")
        except Exception:  # noqa: BLE001
            pass
        token = jar[COOKIE_NAME].value if COOKIE_NAME in jar else None
        status, url = lv.screen(m.group(1), token)
        h.send_response(status)
        _no_store(h)
        if status == 302 and url:
            h.send_header("Location", url)
        h.send_header("Content-Length", "0")
        h.end_headers()
        return True
    takeover = _RE_TAKEOVER.match(path)
    narration = _RE_NARRATION.match(path)
    if not m and not takeover and not narration:
        _err(h, 404, "not_found", "not found")
        return True
    body = h._read_json_body() if method == "POST" else {}
    sender = h._resolve_identity(body if isinstance(body, dict) else {})
    if sender is None:
        _err(h, 401, "unauthorized", "unauthorized")
        return True
    tid = h._tenant_for(sender)
    if tid is None:
        return True
    with tenant_scope(tid):
        if takeover and method in ("GET", "POST"):
            status, out = lv.takeover(takeover.group(1), tid, sender)
            if status != 200:
                h._json(status, out)
                return True
            cookie = (f"{COOKIE_NAME}={out['cookie']}; Path=/v1/browser/sessions/{out['session_id']}/screen; "
                      f"Max-Age={out['max_age']}; HttpOnly; Secure; SameSite=Strict")
            payload = {"session_id": out["session_id"], "screen_url": out["screen_url"]}
            if method == "GET":
                h.send_response(302)
                h.send_header("Location", out["screen_url"])
                h.send_header("Set-Cookie", cookie)
                _no_store(h)
                h.send_header("Content-Length", "0")
                h.end_headers()
                return True
            import json

            raw = json.dumps(payload).encode()
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Set-Cookie", cookie)
            _no_store(h)
            h.send_header("Content-Length", str(len(raw)))
            h.end_headers()
            h.wfile.write(raw)
            return True
        if m and m.group(2) is None and method == "GET":
            out = lv.get(m.group(1), tid, sender)
            if out is None:
                _err(h, 404, "not_found", "session navigateur inconnue")
            else:
                h._json(200, out)
            return True
        if narration and method == "POST":
            # Lokkal : sur la machine seulement (comme « Ton Nokker » et Nina live) ; la passerelle → 403.
            if getattr(h, "_lokkal_relayed", lambda: False)():
                _err(h, 403, "narration_on_machine", "la narration se joue sur la machine")
                return True
            text = lv.narration(narration.group(1), tid, sender, int(narration.group(2)))
            if text is None:
                _err(h, 404, "not_found", "rien à dire pour cette désignation")
                return True
            h._narration_speech(narration.group(1), int(narration.group(2)), text,
                                body if isinstance(body, dict) else {}, sender)
            return True
        if m and m.group(2) == "resume" and method == "POST":
            status, out = lv.resume(m.group(1), tid, sender)
            h._json(status, out)
            return True
    _err(h, 405, "method_not_allowed", "method not allowed")
    return True


__all__ = ["handle", "is_path"]
