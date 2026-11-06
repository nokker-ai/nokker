"""Routes HTTP des abonnements. Montées SEULEMENT si `configure(...)` a reçu un service (`billing_enabled`) : éteint, ces
chemins répondent comme n'importe quel chemin inconnu.

    POST /v1/billing/webhook    (public, signé Stripe)  → 200 {received} | 400 signature | 500 (Stripe rejoue)
    GET  /v1/billing/plan       (session d'appareil)    → 200 {plan, status, current_period_end, cancel_at_period_end, plans}
    POST /v1/billing/checkout   {plan, code?}           → 200 {url}   409 already_subscribed   400 code_* (voucher)
    GET  /v1/billing/voucher?code=                      → 200 {ok, months, first_charge_on} | {ok: false, error}
                                                          404 si les vouchers sont éteints (route absente)
                                                          429 rate_limited (10/h par compte, 30/h par adresse IP)
    POST /v1/billing/portal                             → 200 {url}   404 no_customer
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import parse_qs, urlparse

from nokker.billing.service import Billing, BillingError
from nokker.billing.stripe import StripeError

logger = logging.getLogger("nokker.billing")

PREFIX = "/v1/billing/"
MAX_WEBHOOK_BYTES = 262_144

_billing: Billing | None = None

# Vérifications de code : un code unique (32^8) ne se devine pas sous ces limites.
VOUCHER_PER_ACCOUNT_H, VOUCHER_PER_IP_H = 10, 30


class _HourLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def ok(self, key: str, limit: int, now: float | None = None) -> bool:
        t = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits[key]
            while q and t - q[0] > 3600:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(t)
            return True


_limits = _HourLimiter()


def _voucher_allowed(handler: Any, account: Any) -> bool:
    from nokker.onboarding.http import client_ip

    ip = client_ip(handler) or "?"
    return _limits.ok(f"a:{account.tenant_id}", VOUCHER_PER_ACCOUNT_H) and _limits.ok(f"ip:{ip}", VOUCHER_PER_IP_H)


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
        elif method == "GET" and route == "voucher" and _billing.vouchers is None:
            return False  # réglage éteint : route absente (404), le web masque « J'ai un code »
        elif method == "GET" and route == "voucher":
            if not _voucher_allowed(handler, account):
                handler._json(429, {"error": "rate_limited"})
            else:
                code = (parse_qs(urlparse(handler.path).query).get("code") or [""])[-1][:64]
                handler._json(200, _billing.voucher_status(account, code))
        elif method == "POST" and route == "checkout":
            body = handler._read_json_body()
            code = str(body.get("code") or "")[:64]
            if code and not _voucher_allowed(handler, account):
                handler._json(429, {"error": "rate_limited"})
            else:
                handler._json(200, {"url": _billing.checkout(account, str(body.get("plan") or ""), code)})
        elif method == "POST" and route == "portal":
            handler._json(200, {"url": _billing.portal(account)})
        else:
            return False
    except BillingError as e:
        handler._json(e.status, {"error": e.code})
    return True
