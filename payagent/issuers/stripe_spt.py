"""Client Stripe minimal de l'agent (bibliothèque standard seulement) : Shared Payment Issued Tokens et lecture d'une
PaymentMethod. Version d'API épinglée `2026-09-30.preview` (docs.stripe.com/agentic-commerce/concepts/
shared-payment-tokens, lue le 02.11.2026). Le transport est injectable : les tests rejouent des réponses enregistrées,
aucun test n'appelle l'API réelle.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlencode

from payagent.config import STRIPE_VERSION

# (méthode, url, en-têtes, corps) -> (statut, corps JSON)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, Any]]]


class StripeError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def _flatten(params: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for k, v in params.items():
        key = f"{prefix}[{k}]" if prefix else k
        if v is None:
            continue
        if isinstance(v, dict):
            out.extend(_flatten(v, key))
        elif isinstance(v, bool):
            out.append((key, "true" if v else "false"))
        else:
            out.append((key, str(v)))
    return out


class Stripe:
    def __init__(self, key: str, *, base: str = "https://api.stripe.com", transport: Transport | None = None) -> None:
        self._key, self._base = key, base.rstrip("/")
        self._transport = transport or urllib_transport

    def _call(self, method: str, path: str, params: dict[str, Any] | None = None, *,
              idempotency_key: str | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._key}", "Stripe-Version": STRIPE_VERSION}
        body = None
        if params is not None:
            body = urlencode(_flatten(params)).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        status, data = self._transport(method, self._base + path, headers, body)
        if status >= 400 or not isinstance(data, dict):
            err = (data or {}).get("error") or {} if isinstance(data, dict) else {}
            raise StripeError(status, str(err.get("code") or err.get("type") or "stripe_error"),
                              str(err.get("message") or f"Stripe {status}"))
        return data

    def payment_method(self, pm_id: str) -> dict[str, Any]:
        return self._call("GET", f"/v1/payment_methods/{quote(pm_id, safe='')}")

    def issue_token(self, *, payment_method: str, seller_profile: str, currency: str, max_amount: int,
                    expires_at: int, return_url: str | None, idempotency_key: str) -> dict[str, Any]:
        return self._call("POST", "/v1/shared_payment/issued_tokens", {
            "payment_method": payment_method,
            "seller_details": {"network_business_profile": seller_profile},
            "usage_limits": {"currency": currency.lower(), "max_amount": max_amount, "expires_at": expires_at},
            "return_url": return_url or None,
        }, idempotency_key=idempotency_key)

    def get_token(self, spt_id: str) -> dict[str, Any]:
        return self._call("GET", f"/v1/shared_payment/issued_tokens/{quote(spt_id, safe='')}")

    def revoke_token(self, spt_id: str) -> dict[str, Any]:
        return self._call("POST", f"/v1/shared_payment/issued_tokens/{quote(spt_id, safe='')}/revoke", {})


def verify_webhook(payload: bytes, header: str, secret: str, *, tolerance_s: int = 300,
                   now: float | None = None) -> dict[str, Any]:
    """Vérifie l'en-tête `Stripe-Signature` (t=…,v1=…, HMAC-SHA256 de « t.payload ») et rend l'événement.
    Lève `ValueError` si la signature, l'horodatage ou le JSON ne tiennent pas."""
    parts: dict[str, list[str]] = {}
    for item in (header or "").split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        ts = int(parts["t"][0])
    except (KeyError, ValueError, IndexError):
        raise ValueError("en-tête Stripe-Signature illisible") from None
    if abs((now if now is not None else time.time()) - ts) > tolerance_s:
        raise ValueError("webhook trop ancien")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, v) for v in parts.get("v1", [])):
        raise ValueError("signature du webhook invalide")
    try:
        event = json.loads(payload)
    except ValueError:
        raise ValueError("webhook illisible") from None
    if not isinstance(event, dict):
        raise ValueError("webhook illisible")
    return event


def sign_webhook(payload: bytes, secret: str, ts: int) -> str:
    """En-tête `Stripe-Signature` d'un événement (tests)."""
    return f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
