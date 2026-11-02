"""Logique de l'agent de paiement (docs/PAY-AGENT.md §3-4). Sans HTTP : `http.py` l'expose.

Règle d'or : rien de ce que dit le noyau n'est cru sur parole. Une demande de jeton ne passe que si l'enveloppe du mandat
se vérifie ICI (signature de la passkey sur l'origine épinglée de l'agent, clé de l'appareil lue en base par l'agent,
appareil approuvé et au nom du compte, compte = approbateur du mandat, bloc `agent_pay`, vendeur accepté, devise et
plafond de l'agent, mandat non expiré, un seul jeton par mandat). Le moyen de paiement, lui, n'est enregistré que sur
une preuve WebAuthn du même compte : le noyau ne peut ni le substituer ni le prêter à un autre compte.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from nokker.approvals.crypto import SignatureError, WebAuthnError, verify_webauthn_assertion
from nokker.payments.mandate import agent_pay_of, principal_handle, verify_envelope
from payagent.config import AgentConfig
from payagent.store import Method, Token
from payagent.stripe_api import Stripe, StripeError, verify_webhook

logger = logging.getLogger("payagent")

METHOD_AUD = "nokker-pay-method"
METHOD_PROOF_TTL_S = 600
EVENTS = ("shared_payment.issued_token.requires_action", "shared_payment.issued_token.active",
          "shared_payment.issued_token.used", "shared_payment.issued_token.deactivated")


class AgentError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def method_challenge(account: str, payment_method: str, iat: int) -> str:
    """Défi que la passkey signe pour enregistrer un moyen de paiement (JSON canonique)."""
    return json.dumps({"aud": METHOD_AUD, "account": account, "payment_method": payment_method, "iat": iat},
                      sort_keys=True, separators=(",", ":"))


def sign_callback(body: bytes, secret: str, ts: int) -> str:
    return f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


class PayAgent:
    def __init__(self, cfg: AgentConfig, *, store: Any, devices: Any, stripe: Stripe,
                 notify: Callable[[dict[str, Any]], None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg, self.store, self.devices, self.stripe = cfg, store, devices, stripe
        self.notify, self.clock = notify, clock

    # ───────────────────────── appareils ─────────────────────────

    def _device_of(self, device_id: Any, account: str):
        dev = self.devices.get(device_id) if isinstance(device_id, str) and device_id else None
        if dev is None or dev.status != "approved" or dev.user != account:
            raise AgentError(403, "device", "appareil inconnu, non approuvé ou d'un autre compte")
        return dev

    # ───────────────────────── moyen de paiement ─────────────────────────

    def register_method(self, account: str, payment_method: str, proof: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payment_method, str) or not payment_method.startswith("pm_") or len(payment_method) > 255:
            raise AgentError(400, "invalid", "payment_method pm_… attendue")
        if not isinstance(proof, dict):
            raise AgentError(400, "invalid", "preuve WebAuthn attendue")
        try:
            ch = json.loads(proof.get("challenge") or "")
        except (TypeError, ValueError):
            raise AgentError(400, "invalid", "défi illisible") from None
        now = self.clock()
        if not (isinstance(ch, dict) and ch.get("aud") == METHOD_AUD and ch.get("account") == account
                and ch.get("payment_method") == payment_method and isinstance(ch.get("iat"), int)
                and abs(now - ch["iat"]) <= METHOD_PROOF_TTL_S):
            raise AgentError(403, "proof", "le défi signé ne porte pas sur ce moyen de paiement")
        dev = self._device_of(proof.get("device_id"), account)
        try:
            verify_webauthn_assertion(dev.public_key_spki, proof.get("client_data_json") or "",
                                      proof.get("authenticator_data") or "", proof.get("signature") or "",
                                      expected_challenge=hashlib.sha256(f"{proof['challenge']}.approve".encode()).digest(),
                                      expected_origin=self.cfg.webauthn_origin,
                                      expected_rp_id=urlsplit(self.cfg.webauthn_origin).hostname or "",
                                      require_uv=True)
        except (WebAuthnError, SignatureError, ValueError) as e:
            raise AgentError(403, "proof", f"signature de la passkey invalide : {e}") from None
        try:
            pm = self.stripe.payment_method(payment_method)
        except StripeError as e:
            raise AgentError(400 if e.status < 500 else 502, "stripe", "moyen de paiement refusé par Stripe") from None
        kind = str(pm.get("type") or "")
        card = pm.get(kind) if isinstance(pm.get(kind), dict) else {}
        m = Method(account=account, payment_method=payment_method, kind=kind, brand=card.get("brand"),
                   last4=card.get("last4"), exp_month=card.get("exp_month"), exp_year=card.get("exp_year"),
                   device_id=dev.id, created_at=now)
        old = self.store.get_method(account)
        self.store.put_method(m)
        if old is not None and old.payment_method != payment_method:
            self._revoke_all(account, "method_replaced")
        return m.public()

    def method(self, account: str) -> dict[str, Any] | None:
        m = self.store.get_method(account)
        return m.public() if m is not None else None

    def remove_method(self, account: str) -> bool:
        self._revoke_all(account, "method_removed")
        return self.store.delete_method(account)

    def _revoke_all(self, account: str, why: str) -> None:
        for t in self.store.live_tokens(account):
            try:
                self._revoke(t, why)
            except AgentError:
                logger.warning("révocation du jeton %s en échec (%s)", t.id, why)

    # ───────────────────────── jetons ─────────────────────────

    def issue(self, account: str, envelope: dict[str, Any], kernel_ref: str = "") -> dict[str, Any]:
        """Crée UN SPT pour un mandat signé. Idempotent : la même enveloppe rend le même jeton."""
        now = self.clock()
        if not isinstance(envelope, dict) or not isinstance(envelope.get("mandate"), dict):
            raise AgentError(400, "invalid", "enveloppe de mandat attendue")
        m = envelope["mandate"]
        mid = str(m.get("mandate_id") or "")
        proof = envelope.get("proof") or {}
        dev = self._device_of(proof.get("device_id"), account)
        check = verify_envelope(envelope, now=now, trusted_spki=dev.public_key_spki,
                                expected_origin=self.cfg.webauthn_origin)
        if not check.valid or not check.checks.get("device_key_trusted"):
            raise AgentError(403, "mandate", "mandat non vérifiable : " + "; ".join(check.errors or ["clé"]))
        if check.expired:
            raise AgentError(403, "expired", "mandat expiré")
        principal = m.get("principal") or {}
        if principal.get("approver") != principal_handle(mid, account):
            raise AgentError(403, "principal", "ce compte n'est pas l'approbateur du mandat")
        block = agent_pay_of(m)
        if block is None:
            raise AgentError(403, "not_agent_pay", "ce mandat ne confie pas le paiement à l'agent")
        if block["seller_profile"] not in self.cfg.sellers:
            raise AgentError(403, "seller", "vendeur non accepté par l'agent de paiement")
        amount = m.get("max_amount") or {}
        cur, max_minor = str(amount.get("currency") or ""), amount.get("amount")
        if cur not in self.cfg.currencies:
            raise AgentError(403, "currency", "devise non acceptée par l'agent de paiement")
        if not isinstance(max_minor, int) or not 0 < max_minor <= self.cfg.max_amount_minor:
            raise AgentError(403, "amount", "montant au-delà du plafond de l'agent de paiement")
        existing = self.store.token_by_mandate(mid)
        if existing is not None:
            if existing.mandate_sha256 != envelope.get("mandate_sha256") or existing.account != account:
                raise AgentError(409, "conflict", "un autre jeton existe pour ce mandat")
            return self._view(existing)
        method = self.store.get_method(account)
        if method is None:
            raise AgentError(409, "no_method", "aucun moyen de paiement enregistré")
        exp = int(min(now + self.cfg.max_ttl_s, float((m.get("ap2") or {}).get("exp") or 0)))
        if exp < now + 60:
            raise AgentError(403, "expired", "mandat trop proche de son expiration")
        try:
            spt = self.stripe.issue_token(payment_method=method.payment_method, seller_profile=block["seller_profile"],
                                          currency=cur, max_amount=max_minor, expires_at=exp,
                                          return_url=self.cfg.return_url or None,
                                          idempotency_key="nokker-" + str(envelope.get("mandate_sha256"))[:48])
        except StripeError as e:
            logger.warning("création du SPT refusée pour %s : %s %s", mid, e.status, e.code)
            raise AgentError(402 if e.status < 500 else 502, "stripe", "Stripe a refusé le jeton de paiement") from None
        t = Token(id="tk_" + secrets.token_hex(8), mandate_id=mid, mandate_sha256=str(envelope["mandate_sha256"]),
                  account=account, spt_id=str(spt.get("id") or ""), status=str(spt.get("status") or "active"),
                  seller_profile=block["seller_profile"], currency=cur, max_amount_minor=max_minor, expires_at=exp,
                  created_at=now, updated_at=now, kernel_ref=str(kernel_ref or "")[:128])
        if not t.spt_id.startswith("spt_"):
            raise AgentError(502, "stripe", "réponse de Stripe illisible")
        if not self.store.insert_token(t):  # course : un autre appel a gagné, on rend le sien
            won = self.store.token_by_mandate(mid)
            return self._view(won) if won is not None else self._view(t)
        return self._view(t, spt)

    def token(self, account: str, mandate_id: str) -> dict[str, Any]:
        t = self.store.token_by_mandate(mandate_id)
        if t is None or t.account != account:
            raise AgentError(404, "not_found", "jeton inconnu")
        return self._view(t)

    def revoke(self, account: str, mandate_id: str) -> dict[str, Any]:
        t = self.store.token_by_mandate(mandate_id)
        if t is None or t.account != account:
            raise AgentError(404, "not_found", "jeton inconnu")
        return self._view(self._revoke(t, "revoked"))

    def _revoke(self, t: Token, why: str) -> Token:
        if t.status == "deactivated":
            return t
        try:
            self.stripe.revoke_token(t.spt_id)
        except StripeError as e:
            raise AgentError(502, "stripe", f"révocation refusée par Stripe ({e.code})") from None
        t2 = replace(t, status="deactivated", deactivated_reason=why, updated_at=self.clock())
        self.store.update_token(t2)
        self._notify(t2, "deactivated")
        return t2

    def _view(self, t: Token, spt: dict[str, Any] | None = None) -> dict[str, Any]:
        out = t.public()
        na = _hashed_next_action(spt) if spt is not None and t.status == "requires_action" else None
        if na:
            out["next_action"] = na
        return out

    # ───────────────────────── webhooks Stripe ─────────────────────────

    def on_webhook(self, payload: bytes, signature: str) -> dict[str, Any]:
        try:
            event = verify_webhook(payload, signature, self.cfg.webhook_secret, now=self.clock())
        except ValueError as e:
            raise AgentError(400, "signature", str(e)) from None
        etype, eid = str(event.get("type") or ""), str(event.get("id") or "")
        if etype not in EVENTS:
            return {"ignored": etype}
        obj = ((event.get("data") or {}).get("object") or {})
        t = self.store.token_by_spt(str(obj.get("id") or ""))
        if t is None:
            return {"ignored": "unknown_token"}
        if eid and not self.store.first_event(eid):
            return {"duplicate": eid}
        kind = etype.rsplit(".", 1)[-1]
        next_action = None
        if kind == "requires_action":
            try:  # la doc : relire le jeton côté serveur pour obtenir `next_action`
                fresh = self.stripe.get_token(t.spt_id)
            except StripeError:
                fresh = obj
            next_action = _hashed_next_action(fresh)
            t2 = replace(t, status="requires_action", updated_at=self.clock()) if t.status != "deactivated" else t
        elif kind == "active":
            t2 = replace(t, status="active", updated_at=self.clock()) if t.status != "deactivated" else t
        elif kind == "used":
            t2 = replace(t, used=True, updated_at=self.clock())
        else:
            reason = str(obj.get("deactivated_reason") or t.deactivated_reason or "deactivated")
            t2 = replace(t, status="deactivated", deactivated_reason=reason, updated_at=self.clock())
        self.store.update_token(t2)
        self._notify(t2, kind, next_action)
        return {"handled": etype}

    def _notify(self, t: Token, kind: str, next_action: str | None = None) -> None:
        if self.notify is None:
            return
        body = {"mandate_id": t.mandate_id, "account": t.account, "kernel_ref": t.kernel_ref, "event": kind, "status": t.status, "used": t.used,
                "deactivated_reason": t.deactivated_reason}
        if next_action:
            body["next_action"] = next_action
        try:
            self.notify(body)
        except Exception:
            logger.exception("rappel du noyau en échec pour %s", t.mandate_id)


def _hashed_next_action(spt: dict[str, Any] | None) -> str | None:
    """`next_action.use_stripe_sdk.value` : ce que le web passe à `stripe.handleNextAction({hashedValue})`."""
    na = (spt or {}).get("next_action") or {}
    if na.get("type") != "use_stripe_sdk":
        return None
    val = (na.get("use_stripe_sdk") or {}).get("value")
    return val if isinstance(val, str) and 0 < len(val) <= 8192 else None


__all__ = ["AgentError", "EVENTS", "METHOD_AUD", "PayAgent", "method_challenge", "sign_callback"]
