"""Routes HTTP des abonnements. Montées SEULEMENT si `configure(...)` a reçu un service (`billing_enabled`) : éteint, ces
chemins répondent comme n'importe quel chemin inconnu.

    POST /v1/billing/webhook    (public, signé Stripe)  → 200 {received} | 400 signature | 500 (Stripe rejoue)
    GET  /v1/billing/plan       (session d'appareil)    → 200 {plan, status, current_period_end, cancel_at_period_end, plans}
    POST /v1/billing/checkout   {plan}                  → 200 {url}   409 already_subscribed
    POST /v1/billing/portal                             → 200 {url}   404 no_customer
"""
from __future__ import annotations

import logging
from typing import Any

from nokker.billing.service import Billing, BillingError
from nokker.billing.stripe import StripeError

logger = logging.getLogger("nokker.billing")

PREFIX = "/v1/billing/"
MAX_WEBHOOK_BYTES = 262_144

_billing: Billing | None = None


def configure(billing: Billing | None) -> None:
    global _billing
    _billing = billing


def service() -> Billing | None:
    return _billing


def handle_webhook(handler: Any, method: str, path: str) -> bool:
    if _billing is None or method != "POST" or path != PREFIX + "webhook":
        return False
    length = int(handler.headers.get("Content-Length", 0) or 0)
    if length <= 0 or length > MAX_WEBHOOK_BYTES:
        handler._json(400, {"error": "bad_length"})
        return True
    payload = handler.rfile.read(length)
    try:
        _billing.handle_webhook(payload, handler.headers.get("Stripe-Signature", ""))
    except StripeError:
        handler._json(400, {"error": "signature"})
    except BillingError as e:
        handler._json(e.status if e.status >= 500 else 400, {"error": e.code})
    except Exception:  # noqa: BLE001 — Stripe rejoue sur 5xx ; le détail va au journal, pas à l'appelant
        logger.exception("billing: webhook en échec")
        handler._json(500, {"error": "internal"})
    else:
        handler._json(200, {"received": True})
    return True


def handle(handler: Any, method: str, path: str, account: Any) -> bool:
    """Routes d'un compte authentifié (le locataire est déjà fixé par l'appelant)."""
    if _billing is None or not path.startswith(PREFIX):
        return False
    route = path[len(PREFIX):]
    try:
        if method == "GET" and route == "plan":
            handler._json(200, _billing.status(account.tenant_id))
        elif method == "POST" and route == "checkout":
            handler._json(200, {"url": _billing.checkout(account, str(handler._read_json_body().get("plan") or ""))})
        elif method == "POST" and route == "portal":
            handler._json(200, {"url": _billing.portal(account)})
        else:
            return False
    except BillingError as e:
        handler._json(e.status, {"error": e.code})
    return True
