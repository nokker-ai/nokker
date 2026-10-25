"""Client Stripe minimal en HTTP (pas de SDK, comme nokker/onboarding/front.py) et vérification de signature.

Stripe attend du `application/x-www-form-urlencoded` ; les champs imbriqués s'écrivent `a[b][0][c]`. Le `transport` est
injectable : les tests n'ouvrent aucun réseau.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

API = "https://api.stripe.com/v1"
TOLERANCE_S = 300

Transport = Callable[[str, str, bytes, dict[str, str], float], tuple[int, dict[str, Any]]]


class StripeError(Exception):
    """`code` : `signature` (webhook) ; `unavailable` (réseau) ; sinon le `code` ou le `type` renvoyé par Stripe."""

    def __init__(self, code: str, status: int = 0):
        super().__init__(code)
        self.code, self.status = code, status


def verify_signature(payload: bytes, header: str, secret: str, *, now: float | None = None,
                     tolerance_s: int = TOLERANCE_S) -> None:
    """Lève `StripeError("signature")` si l'en-tête `Stripe-Signature` (`t=…,v1=…[,v1=…]`) ne signe pas `payload`
    avec `secret`, ou si l'horodatage s'écarte de plus de `tolerance_s` (rejeu)."""
    if not secret:
        raise StripeError("signature")
    parts = [p.partition("=") for p in (header or "").split(",")]
    stamps = [v for k, _, v in parts if k.strip() == "t"]
    sigs = [v.strip() for k, _, v in parts if k.strip() == "v1"]
    try:
        ts = int(stamps[0])
    except (IndexError, ValueError):
        raise StripeError("signature") from None
    if abs((time.time() if now is None else now) - ts) > tolerance_s:
        raise StripeError("signature")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise StripeError("signature")


def sign(payload: bytes, secret: str, ts: int) -> str:
    """En-tête `Stripe-Signature` valide (tests, et Stripe CLI en local)."""
    return f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()


def encode(params: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for k, v in params.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out += encode(v, key)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                out += encode(item, f"{key}[{i}]") if isinstance(item, dict) else [(f"{key}[{i}]", str(item))]
        elif isinstance(v, bool):
            out.append((key, "true" if v else "false"))
        elif v is not None:
            out.append((key, str(v)))
    return out


def _urllib(method: str, url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> tuple[int, dict[str, Any]]:
    req = urllib_request.Request(url, data=body or None, method=method, headers=headers)
    try:
        with urllib_request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 — URL fixée (API)
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib_error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


class StripeClient:
    def __init__(self, secret_key: str, timeout_s: float = 20.0, transport: Transport | None = None,
                 base_url: str = API):
        if not secret_key:
            raise ValueError("clé secrète Stripe requise")
        self._base, self._timeout, self._send = base_url.rstrip("/"), timeout_s, transport or _urllib
        self._headers = {"Authorization": f"Bearer {secret_key}", "User-Agent": "nokker-core/1 (billing)",
                         "Content-Type": "application/x-www-form-urlencoded"}

    def _call(self, method: str, path: str, params: dict[str, Any] | None = None,
              idempotency_key: str | None = None) -> dict[str, Any]:
        body = urllib_parse.urlencode(encode(params or {})).encode()
        headers = {**self._headers, **({"Idempotency-Key": idempotency_key} if idempotency_key else {})}
        try:
            status, data = self._send(method, f"{self._base}/{path}", body, headers, self._timeout)
        except (OSError, ValueError):
            raise StripeError("unavailable") from None
        if 200 <= status < 300:
            return data
        err = data.get("error") or {}
        raise StripeError(str(err.get("code") or err.get("type") or "unavailable"), status)

    def create_customer(self, email: str, tenant_id: str, account_id: str) -> str:
        # Clé d'idempotence = locataire : deux clics simultanés ne créent pas deux clients.
        r = self._call("POST", "customers", {"email": email, "metadata": {"tenant_id": tenant_id, "account_id": account_id}},
                       idempotency_key=f"nokker-customer-{tenant_id}")
        return str(r["id"])

    def checkout_session(self, customer: str, price_id: str, tenant_id: str, plan: str, success_url: str,
                         cancel_url: str, lang: str, coupon: str | None = None) -> str:
        """Session Checkout d'abonnement. `coupon` (id d'un coupon Stripe) : remise appliquée d'office par
        `discounts` ; Stripe refuse `discounts` avec `allow_promotion_codes`, qui n'est donc jamais envoyé."""
        params: dict[str, Any] = {
            "mode": "subscription", "customer": customer, "client_reference_id": tenant_id,
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": success_url, "cancel_url": cancel_url, "locale": "fr" if lang == "fr" else "en",
            "subscription_data": {"metadata": {"tenant_id": tenant_id, "plan": plan}},
        }
        if coupon:
            params["discounts"] = [{"coupon": coupon}]
        return str(self._call("POST", "checkout/sessions", params)["url"])

    def portal_session(self, customer: str, return_url: str) -> str:
        return str(self._call("POST", "billing_portal/sessions", {"customer": customer, "return_url": return_url})["url"])

    def list_subscriptions(self, customer: str) -> list[dict[str, Any]]:
        """Tous les abonnements du client, y compris résiliés (l'appelant filtre). 100 suffisent : un locataire en a 1."""
        return list(self._call("GET", f"subscriptions?customer={urllib_parse.quote(customer, safe='')}&status=all&limit=100")
                    .get("data") or [])

    def cancel_subscription(self, subscription_id: str) -> dict[str, Any]:
        """Résiliation immédiate, sans prorata ni facture finale : le remboursement n'est jamais automatique."""
        return self._call("DELETE", f"subscriptions/{urllib_parse.quote(subscription_id, safe='')}",
                          idempotency_key=f"nokker-cancel-{subscription_id}")
