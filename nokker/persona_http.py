"""Routes `/v1/persona*` (V6, « Ton Nokker », `nokker/persona.py`) : session du compte, locataire fixé par
`_account_route`. Drapeau `persona.enabled` éteint : aucune route (404 comme avant)."""
from __future__ import annotations

import json
from typing import Any

from nokker import persona as persona_mod

PATHS = ("/v1/persona", "/v1/persona/catalog", "/v1/persona/preview")


def enabled() -> bool:
    return persona_mod.service() is not None


def is_path(path: str) -> bool:
    return path in PATHS


def _error(handler: Any, status: int, code: str, retry_after: int | None = None) -> None:
    body = json.dumps({"error": {"code": code}}).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    if retry_after is not None:
        handler.send_header("Retry-After", str(retry_after))
    handler.end_headers()
    handler.wfile.write(body)


def handle(handler: Any, method: str, path: str, account: Any, tenant_id: str) -> bool:
    svc = persona_mod.service()
    if svc is None or not is_path(path):
        return False
    acc = account.account_id
    lang = getattr(account, "lang", "fr") or "fr"
    try:
        if path == "/v1/persona/catalog" and method == "GET":
            handler._json(200, svc.catalog.to_json(lang))
        elif path == "/v1/persona" and method == "GET":
            handler._json(200, svc.get(acc))
        elif path == "/v1/persona" and method == "PUT":
            handler._json(200, svc.put(acc, tenant_id, handler._read_json_body()))
        elif path == "/v1/persona/preview" and method == "POST":
            from nokker.voice import web as voice_web

            speech = voice_web.service()
            if speech is None:
                raise persona_mod.PersonaError(503, "tts_off")
            text, plang, voice = svc.preview_text(acc, handler._read_json_body())
            try:
                out = speech.timed(account=acc, tenant=tenant_id, reply=text, lang=plang, voice=voice, rate=False)
            except voice_web.SpeechError as e:
                raise persona_mod.PersonaError(e.status, e.code, e.retry_after) from None
            getattr(handler, "_json_private", handler._json)(200, out)
        else:
            handler._json(404, {"error": {"message": "not found"}})
    except persona_mod.PersonaError as e:
        _error(handler, e.status, e.code, e.retry_after)
    return True


__all__ = ["PATHS", "enabled", "handle", "is_path"]
