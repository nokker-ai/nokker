"""Routes HTTP des canaux Slack et WhatsApp, accrochées par `api/server.py` (routage stdlib à la main). Canal non
configuré (défaut) : `handle_public` / `handle_account` rendent False et la route n'existe pas (404 du serveur).

Publiques, authentifiées par la signature du fournisseur (corps brut, ≤ 1 Mo) :
- `POST /v1/slack/events` : `url_verification` (→ `{"challenge"}`) et `event_callback` (→ 200 tout de suite, le tour
  part au pool). Signature v0 + horodatage ±5 min, sinon 401.
- `POST /v1/slack/interactivity` : clics sur les boutons-liens des cartes (`payload=` formulaire). 200, rien décidé.
- `GET /v1/whatsapp/webhook` : vérification Meta (`hub.challenge` en texte, sinon 403).
- `POST /v1/whatsapp/webhook` : messages. `X-Hub-Signature-256`, sinon 401 ; 200 sinon (Meta renvoie sur une erreur).

D'un compte (session web, locataire fixé par `_account_route`) :
- `POST /v1/slack/pairing`, `POST /v1/whatsapp/pairing` → `{code, command, link, expires_at}`.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nokker.channels import slack as slack_mod
from nokker.channels import whatsapp as wa_mod

logger = logging.getLogger("nokker.channels.http")

MAX_BODY = 1_000_000
ACCOUNT_PATHS = ("/v1/slack/pairing", "/v1/whatsapp/pairing")


@dataclass
class SlackEndpoint:
    adapter: Any  # SlackAdapter
    pairing: Any  # PairingStore
    signing_secret: Callable[[], str]
    app_id: str = ""
    team_id: str = ""


@dataclass
class WhatsAppEndpoint:
    adapter: Any  # WhatsAppAdapter
    pairing: Any
    app_secret: Callable[[], str]
    verify_token: Callable[[], str]
    display_number: str = ""  # numéro public du compte WhatsApp Business (lien wa.me) ; vide = pas de lien


_cfg: dict[str, Any] = {"slack": None, "whatsapp": None}


def configure(slack: SlackEndpoint | None = None, whatsapp: WhatsAppEndpoint | None = None) -> None:
    _cfg.update(slack=slack, whatsapp=whatsapp)


def enabled(channel: str) -> bool:
    return _cfg.get(channel) is not None


def _send(handler: Any, status: int, body: bytes, ctype: str = "application/json") -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _json(handler: Any, status: int, obj: dict[str, Any]) -> None:
    _send(handler, status, json.dumps(obj, ensure_ascii=False).encode())


def _body(handler: Any) -> bytes | None:
    try:
        length = int(handler.headers.get("Content-Length", 0) or 0)
    except ValueError:
        return None
    if length < 0 or length > MAX_BODY:
        return None
    return handler.rfile.read(length) if length else b""


def handle_public(handler: Any, method: str, path: str, query: str = "") -> bool:
    if path.startswith("/v1/slack/") and _cfg["slack"] is not None:
        return _slack(handler, method, path, _cfg["slack"])
    if path == "/v1/whatsapp/webhook" and _cfg["whatsapp"] is not None:
        return _whatsapp(handler, method, query, _cfg["whatsapp"])
    return False


def _slack(handler: Any, method: str, path: str, ep: SlackEndpoint) -> bool:
    if method != "POST" or path not in ("/v1/slack/events", "/v1/slack/interactivity"):
        return False
    body = _body(handler)
    if body is None:
        _json(handler, 413, {"ok": False})
        return True
    if not slack_mod.verify_signature(ep.signing_secret(), handler.headers.get("X-Slack-Request-Timestamp", ""),
                                      handler.headers.get("X-Slack-Signature", ""), body):
        _json(handler, 401, {"ok": False})
        return True
    try:
        if path == "/v1/slack/interactivity":
            form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            ep.adapter.handle_interaction(json.loads((form.get("payload") or ["{}"])[0]))
            _send(handler, 200, b"", "text/plain")
            return True
        payload = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        _json(handler, 400, {"ok": False})
        return True
    if payload.get("type") == "url_verification":
        _json(handler, 200, {"challenge": str(payload.get("challenge") or "")[:200]})
        return True
    try:
        ep.adapter.handle_event(payload)
    except Exception:  # noqa: BLE001 — Slack réessaie sur une erreur ; l'événement est déjà marqué vu
        logger.exception("slack : événement non traité")
    _json(handler, 200, {"ok": True})
    return True


def _whatsapp(handler: Any, method: str, query: str, ep: WhatsAppEndpoint) -> bool:
    if method == "GET":
        challenge = wa_mod.verify_challenge(urllib.parse.parse_qs(query), ep.verify_token())
        if challenge is None:
            _send(handler, 403, b"forbidden", "text/plain")
        else:
            _send(handler, 200, challenge.encode(), "text/plain")
        return True
    if method != "POST":
        return False
    body = _body(handler)
    if body is None:
        _json(handler, 413, {"ok": False})
        return True
    if not wa_mod.verify_signature(ep.app_secret(), handler.headers.get("X-Hub-Signature-256", ""), body):
        _json(handler, 401, {"ok": False})
        return True
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        _json(handler, 200, {"ok": True})  # illisible : ignoré (Meta renverrait sans fin)
        return True
    try:
        ep.adapter.handle(payload)
    except Exception:  # noqa: BLE001
        logger.exception("whatsapp : événement non traité")
    _json(handler, 200, {"ok": True})
    return True


def is_account_path(path: str) -> bool:
    return path in ACCOUNT_PATHS


def handle_account(handler: Any, path: str, account_id: str | None) -> bool:
    """Code d'appairage pour le compte de la session. Canal éteint : 503 `<canal>_disabled`."""
    if path not in ACCOUNT_PATHS:
        return False
    channel = path.split("/")[2]
    ep = _cfg.get(channel)
    if ep is None:
        _json(handler, 503, {"error": f"{channel}_disabled"})
        return True
    if not account_id or not account_id.startswith("us_"):
        _json(handler, 403, {"error": "account_required"})
        return True
    code, expires_at = ep.pairing.issue(account_id)
    command = f"pair {code}"
    if channel == "slack":
        link = (f"https://slack.com/app_redirect?app={urllib.parse.quote(ep.app_id)}&team={urllib.parse.quote(ep.team_id)}"
                if ep.app_id else "")
    else:
        digits = "".join(c for c in ep.display_number if c.isdigit())
        link = f"https://wa.me/{digits}?text={urllib.parse.quote(command)}" if digits else ""
    _json(handler, 200, {"code": code, "command": command, "link": link, "expires_at": expires_at.isoformat()})
    return True


__all__ = ["SlackEndpoint", "WhatsAppEndpoint", "configure", "enabled", "handle_account", "handle_public",
           "is_account_path"]
