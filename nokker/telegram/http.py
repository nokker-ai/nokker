"""Routes HTTP du canal Telegram, accrochées par `api/server.py` (routage stdlib à la main) :

- `POST /v1/telegram/webhook` : mises à jour du bot de la bêta. Authentifiées par l'en-tête
  `X-Telegram-Bot-Api-Secret-Token` (le `secret_token` donné à setWebhook), comparé en temps constant. Réponse 200
  immédiate, le tour part au pool de l'adaptateur.
- `POST /v1/telegram/pairing` : code d'appairage pour le compte de la session (contrat C4) → `{code, link, expires_at}`.
"""
from __future__ import annotations

import hmac
import json
from typing import Any

from nokker.telegram.adapter import TelegramAdapter
from nokker.telegram.pairing import PairingStore

_cfg: dict[str, Any] = {"adapter": None, "pairing": None, "secret": "", "bot_username": ""}


def configure(adapter: TelegramAdapter | None, pairing: PairingStore | None, secret: str, bot_username: str) -> None:
    """Une fois au démarrage (`api/server.py::serve`). Non configuré : webhook 401, appairage 503."""
    _cfg.update(adapter=adapter, pairing=pairing, secret=secret, bot_username=bot_username)


def webhook(handler: Any) -> None:
    handle_webhook(handler, _cfg["adapter"], _cfg["secret"])


def pairing(handler: Any, account_id: str | None) -> None:
    handle_pairing(handler, _cfg["pairing"], account_id, _cfg["bot_username"])


def _json(handler: Any, status: int, obj: dict[str, Any]) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_webhook(handler: Any, adapter: TelegramAdapter | None, secret: str) -> None:
    given = handler.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if adapter is None or not secret or not hmac.compare_digest(given.encode(), secret.encode()):
        _json(handler, 401, {"ok": False})
        return
    length = int(handler.headers.get("Content-Length", 0) or 0)
    try:
        update = json.loads(handler.rfile.read(length) or b"{}")
    except json.JSONDecodeError:
        _json(handler, 200, {"ok": True})  # Telegram réessaie sur une erreur : une mise à jour illisible est ignorée
        return
    adapter.handle_update(update)
    _json(handler, 200, {"ok": True})


def handle_pairing(handler: Any, pairing: PairingStore | None, account_id: str | None, bot_username: str) -> None:
    if pairing is None or not bot_username:
        _json(handler, 503, {"error": "telegram_disabled"})
        return
    if not account_id or not account_id.startswith("us_"):
        _json(handler, 403, {"error": "account_required"})
        return
    code, expires_at = pairing.issue(account_id)
    _json(handler, 200, {"code": code, "link": f"https://t.me/{bot_username}?start={code}",
                         "expires_at": expires_at.isoformat()})


__all__ = ["configure", "handle_pairing", "handle_webhook", "pairing", "webhook"]
