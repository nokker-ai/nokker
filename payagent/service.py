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
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from nokker.approvals.crypto import SignatureError, WebAuthnError, verify_webauthn_assertion
from nokker.payments.mandate import AGENT_PAY_VIA_CARD, AGENT_PAY_VIA_OWN, agent_pay_of, principal_handle, verify_envelope
from payagent.checkout import CheckoutPage, precheck, run_checkout
from payagent.owncard import OTP_RE, CardError, CardVault, Ephemeral, clean_cvv
from payagent.config import AgentConfig
from payagent.issuers import IssuerError, ThreeDSChallenge
from payagent.issuers.stripe_spt import Stripe, StripeError, verify_webhook
from payagent.store import Card, Link, Method, ThreeDS, Token
from payagent.vault import CheckoutFields, VaultCard, VaultError

logger = logging.getLogger("payagent")

METHOD_AUD = "nokker-pay-method"
METHOD_PROOF_TTL_S = 600
THREEDS_AUD = "nokker-3ds"
_WISE_PROFILE = re.compile(r"^wise:[0-9]{1,20}$")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_ISO3 = re.compile(r"^[a-z]{3}$")
_PHONE = re.compile(r"^\+[1-9][0-9]{6,14}$")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
KYC_DONE = ("PASSED", "PASSED_WITH_REQUIREMENTS")
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


def threeds_challenge(t: ThreeDS) -> str:
    """Défi que la passkey signe pour confirmer un paiement 3-D Secure (JSON canonique). Il porte les détails TELS
    QUE L'ÉMETTEUR LES A ENVOYÉS (montant, devise, marchand) et l'identifiant du défi : liaison dynamique à cette
    opération, distincte de la signature du mandat (docs/PAY-AGENT.md §10.3)."""
    return json.dumps({"aud": THREEDS_AUD, "challenge_id": t.id, "mandate_id": t.mandate_id,
                       "amount_minor": t.amount_minor, "currency": t.currency, "merchant": t.merchant_name,
                       "merchant_url": t.merchant_url, "expires_at": int(t.expires_at)},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class PayAgent:
    def __init__(self, cfg: AgentConfig, *, store: Any, devices: Any, stripe: Stripe | None = None,
                 issuer: Any = None, vault: Any = None,
                 notify: Callable[[dict[str, Any]], None] | None = None,
                 clock: Callable[[], float] = time.time, background: bool = False,
                 cards: CardVault | None = None, open_page: Callable[[str], Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        """`stripe` : émetteur `stripe_spt` ; `issuer` + `vault` : émetteur de cartes (`wise`) ; `cards` : carte de la
        personne dans le coffre local (`own_card`, Lokkal), `open_page(url)` : page du navigateur de checkout dédié.
        `background` : rappels au noyau et checkout own_card dans des fils à part."""
        self.cfg, self.store, self.devices, self.stripe = cfg, store, devices, stripe
        self.issuer, self.vault = issuer, vault
        self.notify, self.clock, self.background = notify, clock, background
        self.cards, self.open_page, self.sleep = cards, open_page, sleep
        self.ephemeral = Ephemeral(clock=clock)
        self._prompts: dict[str, dict[str, Any]] = {}  # mandat → {kind: cvv|otp|bank_app, expires_at}

    # ───────────────────────── appareils ─────────────────────────

    def _device_of(self, device_id: Any, account: str):
        dev = self.devices.get(device_id) if isinstance(device_id, str) and device_id else None
        if dev is None or dev.status != "approved" or dev.user != account:
            raise AgentError(403, "device", "appareil inconnu, non approuvé ou d'un autre compte")
        return dev

    # ───────────────────────── moyen de paiement ─────────────────────────

    def register_method(self, account: str, payment_method: str, proof: dict[str, Any],
                        holder_name: str | None = None) -> dict[str, Any]:
        wise = isinstance(payment_method, str) and payment_method.startswith("wise:")
        if wise:
            if self.issuer is None or not _WISE_PROFILE.match(payment_method):
                raise AgentError(400, "invalid", "profil Wise « wise:<id> » attendu")
            if not isinstance(holder_name, str) or not 1 <= len(holder_name.strip()) <= 100 \
                    or not holder_name.isprintable():
                raise AgentError(400, "invalid", "nom du titulaire attendu (1 à 100 caractères)")
        elif not isinstance(payment_method, str) or not payment_method.startswith("pm_") or len(payment_method) > 255:
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
        if wise:
            link = self.store.get_link(account)
            if link is None or link.profile_ref != payment_method:
                raise AgentError(403, "profile", "ce profil n'a pas été ouvert pour ce compte")
            if link.state not in KYC_DONE:
                raise AgentError(409, "kyc", "vérification d'identité pas encore terminée chez l'émetteur")
            try:
                prof = self.issuer.check_profile(payment_method)
            except IssuerError as e:
                raise AgentError(400 if 0 < e.status < 500 else 502, "issuer", "profil refusé par l'émetteur") from None
            m = Method(account=account, payment_method=payment_method, kind="wise_card", brand=prof.get("scheme"),
                       last4=None, exp_month=None, exp_year=None, device_id=dev.id, created_at=now, issuer="wise",
                       holder_name=(holder_name or "").strip())
        else:
            if self.stripe is None:
                raise AgentError(400, "invalid", "émetteur stripe_spt non configuré")
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
        if self.cards is not None:
            try:
                self.cards.forget(account)
            except Exception:  # noqa: BLE001 — coffre scellé : la méthode part quand même, la carte sera effacée au retour
                logger.warning("carte locale non effacée du coffre (coffre indisponible)")
        return self.store.delete_method(account)

    # ───────────────────────── carte de la personne (own_card, Lokkal, docs/PAY-AGENT.md §11) ─────────────────────────

    def register_card(self, account: str, data: Any) -> dict[str, Any]:
        """Enregistre la carte de la personne dans le coffre local (machine de la personne, propriétaire local). Rend la
        marque, les 4 derniers chiffres et l'expiration ; jamais le numéro. Le CVV n'est gardé que si `keep_cvv`."""
        if self.cards is None:
            raise AgentError(404, "not_found", "route inconnue")
        if not isinstance(data, dict):
            raise AgentError(400, "invalid", "carte attendue")
        try:
            meta = self.cards.put(account, number=data.get("number"), exp_month=data.get("exp_month"),
                                  exp_year=data.get("exp_year"), holder=data.get("holder"),
                                  cvv=data.get("cvv") if data.get("keep_cvv") is True else None)
        except CardError as e:
            raise AgentError(400, "card_" + e.code, e.message) from None
        except Exception:  # noqa: BLE001 — coffre scellé ou injoignable : jamais de repli
            raise AgentError(503, "vault", "coffre local indisponible") from None
        now = self.clock()
        old = self.store.get_method(account)
        m = Method(account=account, payment_method="own:" + hashlib.sha256(account.encode()).hexdigest()[:24],
                   kind="own_card", brand=meta.brand, last4=meta.last4, exp_month=meta.exp_month,
                   exp_year=meta.exp_year, device_id="", created_at=now, issuer="own_card")
        self.store.put_method(m)
        if old is not None and old.issuer != "own_card":
            self._revoke_all(account, "method_replaced")
        return {**m.public(), "cvv_kept": meta.cvv_kept}

    def set_cvv_mode(self, account: str, keep: bool, cvv: Any = None) -> dict[str, Any]:
        """CVV gardé dans le coffre local (`keep`, avec `cvv`) ou demandé à chaque paiement (défaut)."""
        if self.cards is None:
            raise AgentError(404, "not_found", "route inconnue")
        try:
            kept = self.cards.set_cvv(account, clean_cvv(cvv) if keep else None)
        except CardError as e:
            raise AgentError(400 if e.code != "no_card" else 409, "card_" + e.code, e.message) from None
        return {"cvv_kept": kept}

    def card_meta(self, account: str) -> dict[str, Any] | None:
        m = self.store.get_method(account)
        if m is None or m.issuer != "own_card" or self.cards is None:
            return None
        try:
            f = self.cards.fields(account)
        except Exception:  # noqa: BLE001
            f = None
        return {**m.public(), "cvv_kept": bool(f and f.get("cvv")), "vault_ok": f is not None}

    def provide(self, account: str, mandate_id: str, kind: str, value: Any) -> dict[str, Any]:
        """La personne donne le CVV (`cvv`) ou le code de la banque (`otp`) demandé pour CE paiement. En mémoire seulement,
        lu une fois par le composant de checkout ; jamais écrit ni journalisé ; refusé si rien n'est demandé."""
        c = self.store.card_by_mandate(mandate_id)
        p = self._prompts.get(mandate_id)
        if c is None or c.account != account or p is None or p.get("kind") != kind or p["expires_at"] <= self.clock():
            raise AgentError(404, "not_found", "rien n'est demandé pour ce paiement")
        v = str(value or "").strip()
        try:
            v = clean_cvv(v) if kind == "cvv" else v
        except CardError:
            raise AgentError(400, "invalid", "code de sécurité illisible") from None
        if kind == "otp" and not OTP_RE.match(v):
            raise AgentError(400, "invalid", "code illisible")
        self.ephemeral.put(account, mandate_id, kind, v, ttl_s=max(1.0, p["expires_at"] - self.clock()))
        return {"accepted": True}

    def _ask(self, c: Card, kind: str, ttl_s: float) -> str | None:
        """Demande `kind` à la personne (rappel au noyau → bandeau du web) et attend sa réponse jusqu'à l'échéance."""
        due = self.clock() + ttl_s
        self._prompts[c.mandate_id] = {"kind": kind, "expires_at": int(due)}
        self._notify_card(c, "prompt", prompt=self._prompts[c.mandate_id])
        try:
            while self.clock() < due:
                got = self.ephemeral.take(c.account, c.mandate_id, kind)
                if got:
                    return got
                self.sleep(0.5)
            return None
        finally:
            self._prompts.pop(c.mandate_id, None)
            self._notify_card(c, "prompt_done")

    def _issue_own(self, account: str, envelope: dict[str, Any], m: dict[str, Any], mid: str,
                   block: dict[str, Any], now: float, kernel_ref: str) -> dict[str, Any]:
        """Mandat vérifié payé avec la carte de la personne : aucune carte émise ; un paiement (même vocabulaire d'état
        que les cartes) suivi par l'agent, et le checkout lancé dans le navigateur dédié."""
        if self.cards is None:
            raise AgentError(403, "issuer", "carte locale non configurée")
        host = str(block["merchant_host"])
        if any(host == d or host.endswith("." + d) for d in self.cfg.deny_hosts):
            raise AgentError(403, "seller", "marchand refusé par l'agent de paiement")
        amount = m.get("max_amount") or {}
        cur, max_minor = str(amount.get("currency") or ""), amount.get("amount")
        if cur not in self.cfg.currencies:
            raise AgentError(403, "currency", "devise non acceptée par l'agent de paiement")
        if not isinstance(max_minor, int) or not 0 < max_minor <= self.cfg.max_amount_minor:
            raise AgentError(403, "amount", "montant au-delà du plafond de l'agent de paiement")
        existing = self.store.card_by_mandate(mid)
        if existing is not None:
            if existing.mandate_sha256 != envelope.get("mandate_sha256") or existing.account != account:
                raise AgentError(409, "conflict", "un autre paiement existe pour ce mandat")
            return self._card_view(existing)
        method = self.store.get_method(account)
        if method is None or method.issuer != "own_card":
            raise AgentError(409, "no_method", "aucune carte enregistrée")
        exp = int(min(now + self.cfg.max_ttl_s, float((m.get("ap2") or {}).get("exp") or 0)))
        if exp < now + 60:
            raise AgentError(403, "expired", "mandat trop proche de son expiration")
        c = Card(id="cd_" + secrets.token_hex(8), mandate_id=mid, mandate_sha256=str(envelope["mandate_sha256"]),
                 account=account, issuer="own_card", profile_ref="", card_ref="own:" + mid, vault_ref="",
                 merchant_host=host, currency=cur, max_amount_minor=max_minor, expires_at=exp, status="active",
                 created_at=now, updated_at=now, kernel_ref=str(kernel_ref or "")[:128])
        if not self.store.insert_card(c):
            won = self.store.card_by_mandate(mid)
            return self._card_view(won) if won is not None else self._card_view(c)
        url = str((m.get("cart") or {}).get("url") or "")
        view = self._card_view(c)  # l'état AU LANCEMENT : le paiement commence (le checkout le fera évoluer)
        if self.open_page is not None and url:
            if self.background:
                threading.Thread(target=self._run_own, args=(c, url), daemon=True).start()
            else:
                self._run_own(c, url)
        return view

    def _run_own(self, c: Card, url: str) -> dict[str, Any]:
        """Checkout own_card : page du marchand dans le navigateur dédié ; vérifications AVANT de lire la carte (hôte,
        total ≤ mandat, pas de CSE) ; CVV du coffre ou demandé ; code de la banque demandé à la personne et tapé par le
        composant ; confirmation dans l'appli de la banque : attente sans rien toucher."""
        try:
            page = self.open_page(url)
        except Exception:  # noqa: BLE001
            logger.warning("navigateur de checkout indisponible pour %s", c.mandate_id)
            return self._own_end(c, "handoff", "navigateur de checkout indisponible")
        try:
            hop = getattr(page, "to_checkout", None)
            if callable(hop):
                hop()  # lien du panier → page de paiement (même hôte) AVANT les vérifications et la carte
            pre = precheck(page, merchant_host=c.merchant_host, max_amount_minor=c.max_amount_minor,
                           currency=c.currency)
            if pre is not None:
                return self._own_end(c, pre.outcome, pre.reason)
            f = self.cards.fields(c.account) if self.cards is not None else None
            if not f:
                return self._own_end(c, "handoff", "carte absente du coffre local")
            cvv = f.get("cvv") or self._ask(c, "cvv", 300.0)
            if not cvv:
                return self._own_end(c, "handoff", "code de sécurité non donné à temps")
            fields = CheckoutFields(number=f["number"], cvc=cvv, expiry=f["expiry"], holder=f["holder"],
                                    allowed_hosts=(c.merchant_host,), expires_at=self.clock() + 900)
            cvv = f = None  # plus rien de la carte dans ce cadre une fois tapé
            res = run_checkout(page, fields=fields, merchant_host=c.merchant_host, max_amount_minor=c.max_amount_minor,
                               currency=c.currency, challenge_deadline=lambda: None, clock=self.clock,
                               sleep=self.sleep, ask_otp=lambda: self._ask(c, "otp", 300.0),
                               on_bank_app=lambda: self._notify_card(
                                   c, "prompt", prompt={"kind": "bank_app", "expires_at": int(self.clock() + 600)}))
            return self._own_end(c, res.outcome, res.reason)
        except Exception as e:  # noqa: BLE001 — jamais un paiement suspendu : carte désactivée, personne prévenue
            # le TYPE seulement : le message d'une erreur du navigateur peut citer la valeur d'un champ (la carte)
            logger.warning("checkout own_card interrompu pour %s (%s)", c.mandate_id, type(e).__name__)
            return self._own_end(c, "error", "checkout interrompu : vérifie chez le marchand avant de payer toi-même")
        finally:
            self.ephemeral.wipe(c.account, c.mandate_id)
            close = getattr(page, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass

    def _own_end(self, c: Card, outcome: str, reason: str) -> dict[str, Any]:
        fresh = self.store.card_by_mandate(c.mandate_id) or c
        if outcome == "paid":
            c2 = replace(fresh, used=True, status="deactivated", deactivated_reason="used", updated_at=self.clock())
            self.store.update_card(c2)
            self._notify_card(c2, "used")
        elif outcome == "handoff":
            c2 = replace(fresh, status="deactivated", deactivated_reason="handoff", updated_at=self.clock())
            self.store.update_card(c2)
            self._notify_card(c2, "handoff")
        else:
            c2 = replace(fresh, status="deactivated", deactivated_reason="checkout_" + outcome, updated_at=self.clock())
            self.store.update_card(c2)
            self._notify_card(c2, "deactivated")
        return {"outcome": outcome, "reason": reason}

    def _revoke_all(self, account: str, why: str) -> None:
        for t in self.store.live_tokens(account):
            try:
                self._revoke(t, why)
            except AgentError:
                logger.warning("révocation du jeton %s en échec (%s)", t.id, why)
        for c in self.store.live_cards(account):
            try:
                self._block(c, why)
            except AgentError:
                logger.warning("blocage de la carte %s en échec (%s)", c.id, why)

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
        if block.get("via") == AGENT_PAY_VIA_CARD:
            return self._issue_card(account, envelope, m, mid, block, now, kernel_ref)
        if block.get("via") == AGENT_PAY_VIA_OWN:
            return self._issue_own(account, envelope, m, mid, block, now, kernel_ref)
        if self.stripe is None:
            raise AgentError(403, "seller", "émetteur stripe_spt non configuré")
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
        c = self.store.card_by_mandate(mandate_id)
        if c is not None and c.account == account:
            return self._card_view(c)
        t = self.store.token_by_mandate(mandate_id)
        if t is None or t.account != account:
            raise AgentError(404, "not_found", "jeton inconnu")
        return self._view(t)

    def revoke(self, account: str, mandate_id: str) -> dict[str, Any]:
        c = self.store.card_by_mandate(mandate_id)
        if c is not None and c.account == account:
            return self._card_view(self._block(c, "revoked"))
        t = self.store.token_by_mandate(mandate_id)
        if t is None or t.account != account:
            raise AgentError(404, "not_found", "jeton inconnu")
        return self._view(self._revoke(t, "revoked"))

    # ───────────────────────── compte émetteur (wise) : ouverture et KYC chez Wise ─────────────────────────

    def link_start(self, account: str, person: Any) -> dict[str, Any]:
        """Ouvre le profil Wise de la personne (données d'identité transmises à Wise, jamais gardées ici) et lance sa
        revue KYC « carte ». Idempotent : un compte qui a déjà un profil le garde."""
        if self.issuer is None:
            raise AgentError(404, "not_found", "route inconnue")
        link = self.store.get_link(account)
        if link is None:
            p = _person(person)
            ext = "nk_" + hashlib.sha256(account.encode()).hexdigest()[:24]
            try:
                ref = self.issuer.create_profile(p, external_id=ext)
                self.issuer.start_kyc(ref)
            except IssuerError as e:
                raise AgentError(400 if 0 < e.status < 500 else 502, "issuer",
                                 "l'émetteur a refusé l'ouverture du compte") from None
            now = self.clock()
            link = Link(account=account, profile_ref=ref, review_id=None, state="NEW", created_at=now, updated_at=now)
            self.store.put_link(link)
        return self.link_view(account)

    def link_view(self, account: str) -> dict[str, Any] | None:
        """État du compte émetteur, relu chez Wise tant que la revue n'est pas terminée."""
        link = self.store.get_link(account)
        if link is None or self.issuer is None:
            return None
        if link.state not in KYC_DONE:
            link = self._refresh_link(link)
        return {**link.public(), "profile_ref": link.profile_ref}

    def _refresh_link(self, link: Link) -> Link:
        try:
            review = link.review_id
            if review is None:
                items = self.issuer.kyc_reviews(link.profile_ref)
                if not items:
                    return link
                review = str(items[0]["id"])
            state = self.issuer.kyc_status(link.profile_ref, review) or link.state
        except IssuerError:
            return link
        if state == link.state and review == link.review_id:
            return link
        link2 = replace(link, review_id=review, state=state, updated_at=self.clock())
        self.store.put_link(link2)
        return link2

    def link_url(self, account: str, redirect_url: str) -> dict[str, Any]:
        """Lien KYC hébergé par Wise pour finir la vérification (court : 5 min, `expires_at` fait foi)."""
        link = self.store.get_link(account)
        if link is None or self.issuer is None:
            raise AgentError(404, "not_found", "aucun compte émetteur ouvert")
        if not isinstance(redirect_url, str) or not redirect_url.startswith("https://") or len(redirect_url) > 500:
            raise AgentError(400, "invalid", "adresse de retour https:// attendue")
        link = self._refresh_link(link)
        if link.review_id is None:
            raise AgentError(409, "kyc_pending", "la vérification n'est pas encore prête chez l'émetteur")
        try:
            url, expires, state = self.issuer.kyc_link(link.profile_ref, link.review_id, redirect_url)
        except IssuerError as e:
            raise AgentError(409 if e.status == 409 else 502, e.code if e.status == 409 else "issuer",
                             "pas de lien de vérification pour l'instant") from None
        if state and state != link.state:
            self.store.put_link(replace(link, state=state, updated_at=self.clock()))
        return {"url": url, "expires_at": expires}

    def balance(self, account: str) -> list[dict[str, Any]]:
        link = self.store.get_link(account)
        if link is None or self.issuer is None:
            raise AgentError(404, "not_found", "aucun compte émetteur ouvert")
        try:
            return [{"currency": b.currency, "amount_minor": b.amount_minor}
                    for b in self.issuer.balances(profile_ref=link.profile_ref)]
        except IssuerError as e:
            raise AgentError(502, "issuer", f"solde indisponible ({e.code})") from None

    def _on_kyc(self, data: dict[str, Any]) -> dict[str, Any]:
        ref, state = data.get("profile_ref"), data.get("state")
        link = self.store.link_by_profile(ref) if isinstance(ref, str) else None
        if link is None or not isinstance(state, str):
            return {"ignored": "unknown_profile"}
        rid = data.get("review_id") if isinstance(data.get("review_id"), str) else link.review_id
        rb = data.get("required_by") if isinstance(data.get("required_by"), str) else link.required_by
        self.store.put_link(replace(link, review_id=rid, state=state, required_by=rb, updated_at=self.clock()))
        return {"handled": "kyc"}

    # ───────────────────────── cartes (émetteur `wise`, docs/PAY-AGENT.md §10) ─────────────────────────

    def _issue_card(self, account: str, envelope: dict[str, Any], m: dict[str, Any], mid: str,
                    block: dict[str, Any], now: float, kernel_ref: str) -> dict[str, Any]:
        """Émet UNE carte virtuelle pour un mandat vérifié : limite LIFETIME = montant du mandat, achats en ligne
        seulement, données sensibles remises au coffre. Idempotent : le même mandat rend la même carte."""
        if self.issuer is None or self.vault is None or block.get("issuer") != getattr(self.issuer, "name", None):
            raise AgentError(403, "issuer", "émetteur de carte non configuré")
        host = str(block["merchant_host"])
        if any(host == d or host.endswith("." + d) for d in self.cfg.deny_hosts):
            raise AgentError(403, "seller", "marchand refusé par l'agent de paiement")
        amount = m.get("max_amount") or {}
        cur, max_minor = str(amount.get("currency") or ""), amount.get("amount")
        if cur not in self.cfg.currencies:
            raise AgentError(403, "currency", "devise non acceptée par l'agent de paiement")
        if not isinstance(max_minor, int) or not 0 < max_minor <= self.cfg.max_amount_minor:
            raise AgentError(403, "amount", "montant au-delà du plafond de l'agent de paiement")
        existing = self.store.card_by_mandate(mid)
        if existing is not None:
            if existing.mandate_sha256 != envelope.get("mandate_sha256") or existing.account != account:
                raise AgentError(409, "conflict", "une autre carte existe pour ce mandat")
            return self._card_view(existing)
        method = self.store.get_method(account)
        if method is None or method.issuer != "wise":
            raise AgentError(409, "no_method", "aucun compte émetteur enregistré")
        exp = int(min(now + self.cfg.max_ttl_s, float((m.get("ap2") or {}).get("exp") or 0)))
        if exp < now + 60:
            raise AgentError(403, "expired", "mandat trop proche de son expiration")
        try:
            issued = self.issuer.issue_card(profile_ref=method.payment_method, currency=cur, limit_minor=max_minor,
                                            idempotency_key=str(envelope.get("mandate_sha256")), vault=self.vault,
                                            holder_name=method.holder_name or "")
        except IssuerError as e:
            logger.warning("carte refusée pour %s : %s %s", mid, e.status, e.code)
            raise AgentError(402 if 0 < e.status < 500 else 502, "issuer", "l'émetteur a refusé la carte") from None
        vcard = issued.vault_card
        if not isinstance(vcard, VaultCard):
            raise AgentError(502, "issuer", "carte sans référence du coffre")
        c = Card(id="cd_" + secrets.token_hex(8), mandate_id=mid, mandate_sha256=str(envelope["mandate_sha256"]),
                 account=account, issuer=self.issuer.name, profile_ref=method.payment_method,
                 card_ref=issued.card_ref, vault_ref=vcard.vault_ref, merchant_host=host, currency=cur,
                 max_amount_minor=max_minor, expires_at=exp, status="active", created_at=now, updated_at=now,
                 kernel_ref=str(kernel_ref or "")[:128], vault_sealed=dict(vcard.sealed))
        if not self.store.insert_card(c):  # course : un autre appel a gagné ; la carte en trop est bloquée
            self._block(c, "duplicate", store=False)
            won = self.store.card_by_mandate(mid)
            return self._card_view(won) if won is not None else self._card_view(c)
        return self._card_view(c)

    def _card_view(self, c: Card) -> dict[str, Any]:
        out = c.public()
        if c.issuer == "own_card":
            out["kind"] = "own_card"
            p = self._prompts.get(c.mandate_id)
            if p is not None:
                out["prompt"] = dict(p)
        pending = self.store.pending_threeds(c.id) if c.status == "requires_action" else None
        if pending is not None:
            out["threeds"] = _threeds_public(pending)
        return out

    def _vault_card(self, c: Card) -> VaultCard:
        return VaultCard(vault_ref=c.vault_ref, issuer=c.issuer, card_ref=c.card_ref, sealed=dict(c.vault_sealed))

    def _block(self, c: Card, why: str, *, store: bool = True) -> Card:
        """Bloque la carte chez l'émetteur (irréversible) et l'efface du coffre. Une carte bloquée ne compte plus
        dans le quota de cartes virtuelles du profil (3 chez Wise)."""
        if c.status == "deactivated":
            return c
        if c.issuer == "own_card":  # rien à bloquer chez un émetteur : le paiement s'arrête, la carte reste celle de la personne
            c2 = replace(c, status="deactivated", deactivated_reason=why, updated_at=self.clock())
            if store:
                self.store.update_card(c2)
                self._notify_card(c2, "deactivated")
            return c2
        try:
            self.issuer.block_card(profile_ref=c.profile_ref, card_ref=c.card_ref)
        except IssuerError as e:
            raise AgentError(502, "issuer", f"blocage refusé par l'émetteur ({e.code})") from None
        try:
            self.vault.forget(self._vault_card(c))
        except VaultError:
            logger.warning("effacement du coffre en échec pour la carte %s", c.id)
        c2 = replace(c, status="deactivated", deactivated_reason=why, updated_at=self.clock(), vault_sealed={})
        if store:
            self.store.update_card(c2)
            self._notify_card(c2, "deactivated")
        return c2

    def expire_due(self) -> int:
        """Bloque les cartes dont le mandat a expiré (à appeler périodiquement). Rend le nombre de cartes bloquées."""
        n, now = 0, self.clock()
        for c in self.store.live_cards():
            if c.expires_at <= now:
                try:
                    self._block(c, "expired")
                    n += 1
                except AgentError:
                    logger.warning("carte %s expirée mais pas encore bloquée", c.id)
        return n

    # ── webhooks de l'émetteur ──

    def on_issuer_webhook(self, payload: bytes, headers: Any) -> dict[str, Any]:
        if self.issuer is None:
            raise AgentError(404, "not_found", "route inconnue")
        try:
            ev = self.issuer.parse_webhook(payload, headers)
        except ValueError as e:
            raise AgentError(400, "signature", str(e)) from None
        if ev.kind == "ignored":
            return {"ignored": ev.data.get("event_type")}
        if ev.kind == "kyc":
            return self._on_kyc(ev.data)
        c = self.store.card_by_ref(ev.card_ref or "")
        if ev.kind == "3ds_challenge":
            return self._on_challenge(c, ev.challenge)
        if c is None:
            return {"ignored": "unknown_card"}
        if ev.event_id and not self.store.first_event(f"{self.issuer.name}:{ev.event_id}"):
            return {"duplicate": ev.event_id}
        if ev.kind == "card_status" and ev.data.get("card_status") in ("BLOCKED", "EXPIRED", "PURGED"):
            c2 = replace(c, status="deactivated", deactivated_reason="issuer_" + str(ev.data["card_status"]).lower(),
                         updated_at=self.clock(), vault_sealed={})
            try:
                self.vault.forget(self._vault_card(c))
            except VaultError:
                logger.warning("effacement du coffre en échec pour la carte %s", c.id)
            self.store.update_card(c2)
            self._notify_card(c2, "deactivated")
        elif ev.kind == "transaction" and ev.data.get("transaction_state") == "COMPLETED" \
                and ev.data.get("transaction_type") == "ECOM_PURCHASE" and c.status != "deactivated":
            # achat débité : la carte réservée à cet achat a servi, elle est bloquée
            self._block(replace(c, used=True), "used")
        return {"handled": ev.kind}

    def _on_challenge(self, c: Card | None, ch: ThreeDSChallenge | None) -> dict[str, Any]:
        """Défi 3-D Secure délégué : refus immédiat si la carte, le montant, la devise ou le marchand ne
        correspondent pas au mandat ; sinon défi en attente de la passkey de la personne (pas d'auto-approbation :
        la signature du mandat ne vaut pas réponse 3-D Secure)."""
        if ch is None:
            return {"ignored": "no_challenge"}
        why = None
        if c is None:
            why = "unknown_card"
        elif c.status == "deactivated" or c.used:
            why = "card_inactive"
        elif ch.profile_ref != c.profile_ref:
            why = "profile"
        elif ch.currency != c.currency:
            why = "currency"
        elif not 0 < ch.amount_minor <= c.max_amount_minor:
            why = "amount"
        elif ch.expires_at <= self.clock():
            why = "expired"
        match = "unverified"
        if why is None and ch.merchant_url:
            host = (urlsplit(ch.merchant_url).hostname or "").lower()
            if host == c.merchant_host or host.endswith("." + c.merchant_host):
                match = "host"
            else:
                why = "merchant"
        if why is not None:
            self._answer_issuer(ch, approved=False)
            if c is not None:
                logger.warning("défi 3-D Secure refusé pour la carte %s : %s", c.id, why)
            return {"rejected": why}
        assert c is not None
        t = ThreeDS(id="3ds_" + secrets.token_hex(8), card_id=c.id, mandate_id=c.mandate_id, account=c.account,
                    challenge_ref=ch.challenge_ref, profile_ref=ch.profile_ref, amount_minor=ch.amount_minor,
                    currency=ch.currency, merchant_name=ch.merchant_name[:200], merchant_url=ch.merchant_url,
                    merchant_country=ch.merchant_country, merchant_match=match, expires_at=ch.expires_at,
                    status="pending", created_at=self.clock())
        if not self.store.put_threeds(t):
            return {"duplicate": ch.challenge_ref}
        c2 = replace(c, status="requires_action", updated_at=self.clock())
        self.store.update_card(c2)
        self._notify_card(c2, "3ds_pending", threeds=_threeds_public(t))
        return {"pending": t.id}

    def _answer_issuer(self, ch: ThreeDSChallenge, *, approved: bool) -> bool:
        try:
            self.issuer.challenge_result(challenge=ch, approved=approved)
            return True
        except IssuerError as e:
            logger.warning("réponse 3-D Secure refusée par l'émetteur : %s %s", e.status, e.code)
            return False

    # ── réponse de la personne ──

    def threeds(self, account: str, mandate_id: str) -> dict[str, Any]:
        c = self.store.card_by_mandate(mandate_id)
        t = self.store.pending_threeds(c.id) if c is not None and c.account == account else None
        if t is None:
            raise AgentError(404, "not_found", "aucune confirmation en attente")
        return {**_threeds_public(t), "merchant_url": t.merchant_url, "merchant_country": t.merchant_country,
                "merchant_match": t.merchant_match, "challenge": threeds_challenge(t)}

    def answer_threeds(self, account: str, mandate_id: str, challenge_id: str, proof: Any,
                       decline: bool = False) -> dict[str, Any]:
        c = self.store.card_by_mandate(mandate_id)
        t = self.store.pending_threeds(c.id) if c is not None and c.account == account else None
        if c is None or t is None or t.id != challenge_id:
            raise AgentError(404, "not_found", "aucune confirmation en attente")
        now = self.clock()
        ch = ThreeDSChallenge(challenge_ref=t.challenge_ref, card_ref=c.card_ref, profile_ref=t.profile_ref,
                              amount_minor=t.amount_minor, currency=t.currency, merchant_name=t.merchant_name,
                              merchant_url=t.merchant_url, merchant_country=t.merchant_country,
                              expires_at=t.expires_at)
        if now >= t.expires_at:
            self.store.update_threeds(replace(t, status="expired", decided_at=now))
            self._settle_card(c, "3ds_done")
            raise AgentError(410, "expired", "délai de confirmation dépassé")
        if decline:
            self._answer_issuer(ch, approved=False)
            self.store.update_threeds(replace(t, status="rejected", decided_at=now))
            return self._card_view(self._settle_card(c, "3ds_done"))
        if not isinstance(proof, dict) or proof.get("challenge") != threeds_challenge(t):
            raise AgentError(403, "proof", "le défi signé ne porte pas sur ce paiement")
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
        ok = self._answer_issuer(ch, approved=True)
        self.store.update_threeds(replace(t, status="approved" if ok else "failed", decided_at=now, device_id=dev.id))
        if not ok:
            self._settle_card(c, "3ds_done")
            raise AgentError(502, "issuer", "l'émetteur n'a pas pris la confirmation")
        return self._card_view(self._settle_card(c, "3ds_done"))

    def _settle_card(self, c: Card, event: str) -> Card:
        fresh = self.store.card_by_mandate(c.mandate_id) or c
        c2 = replace(fresh, status="active", updated_at=self.clock()) if fresh.status == "requires_action" else fresh
        self.store.update_card(c2)
        self._notify_card(c2, event)
        return c2

    # ── navigateur de checkout ──

    def checkout(self, account: str, mandate_id: str, page: CheckoutPage, **kw: Any) -> dict[str, Any]:
        """Paie la carte du mandat sur la page du marchand (lane dédié, derrière le proxy du coffre)."""
        c = self.store.card_by_mandate(mandate_id)
        if c is None or c.account != account or c.status == "deactivated" or c.used:
            raise AgentError(404, "not_found", "carte inconnue ou inactive")
        pre = precheck(page, merchant_host=c.merchant_host, max_amount_minor=c.max_amount_minor, currency=c.currency)
        if pre is not None:  # avant de demander quoi que ce soit au coffre
            fresh = self.store.card_by_mandate(mandate_id) or c
            if pre.outcome == "refused":
                self._block(fresh, "checkout_refused")
            else:
                self._notify_card(fresh, "handoff")
            return {"outcome": pre.outcome, "reason": pre.reason}
        try:
            fields = self.vault.checkout_fields(self._vault_card(c),
                                                allowed_hosts=(c.merchant_host, *self.cfg.psp_hosts),
                                                ttl_s=self.cfg.checkout_ttl_s)
        except VaultError as e:
            raise AgentError(502, "vault", f"coffre indisponible ({e.code})") from None

        def deadline() -> float | None:
            p = self.store.pending_threeds(c.id)
            return p.expires_at if p is not None else None

        res = run_checkout(page, fields=fields, merchant_host=c.merchant_host, max_amount_minor=c.max_amount_minor,
                           currency=c.currency, challenge_deadline=deadline, clock=self.clock, **kw)
        fresh = self.store.card_by_mandate(mandate_id) or c
        if res.outcome == "paid":
            # le débit arrive par le webhook de transaction (COMPLETED → carte bloquée) ; plafond LIFETIME en attendant
            c2 = replace(fresh, used=True, updated_at=self.clock())
            self.store.update_card(c2)
            self._notify_card(c2, "used")
        elif res.outcome in ("failed", "refused", "timeout"):
            self._block(fresh, "checkout_" + res.outcome)
        else:  # handoff : la personne finit elle-même ; la carte reste bornée par sa limite et son échéance
            self._notify_card(fresh, "handoff")
        return {"outcome": res.outcome, "reason": res.reason}

    def _notify_card(self, c: Card, kind: str, *, threeds: dict[str, Any] | None = None,
                     prompt: dict[str, Any] | None = None) -> None:
        if self.notify is None:
            return
        body = {"mandate_id": c.mandate_id, "account": c.account, "kernel_ref": c.kernel_ref, "event": kind,
                "status": c.status, "used": c.used, "deactivated_reason": c.deactivated_reason}
        if threeds is not None:
            body["threeds"] = threeds
        if prompt is not None:  # own_card : ce que la banque demande à la personne (sorte, échéance), jamais une valeur
            body["prompt"] = {"kind": prompt.get("kind"), "expires_at": prompt.get("expires_at")}
        self._send(body, c.mandate_id)

    def _send(self, body: dict[str, Any], ref: str) -> None:
        def go() -> None:
            try:
                self.notify(body)
            except Exception:
                logger.exception("rappel du noyau en échec pour %s", ref)

        if self.background:
            threading.Thread(target=go, daemon=True).start()
        else:
            go()

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


def _person(p: Any) -> dict[str, str]:
    """Données d'identité pour ouvrir le profil chez l'émetteur : validées ici, transmises, jamais gardées."""
    if not isinstance(p, dict):
        raise AgentError(400, "invalid", "identité attendue")

    def txt(k: str, n: int, req: bool = True) -> str:
        v = p.get(k)
        v = v.strip() if isinstance(v, str) else ""
        if (req and not v) or len(v) > n or (v and not v.isprintable()):
            raise AgentError(400, "invalid", f"champ {k} manquant ou illisible")
        return v

    out = {"first_name": txt("first_name", 100), "last_name": txt("last_name", 100),
           "date_of_birth": txt("date_of_birth", 10), "address_line": txt("address_line", 200),
           "city": txt("city", 100), "post_code": txt("post_code", 20, False),
           "country_iso3": txt("country_iso3", 3).lower(), "email": txt("email", 254), "phone": txt("phone", 16)}
    if not _DATE.match(out["date_of_birth"]) or not _ISO3.match(out["country_iso3"]) \
            or not _PHONE.match(out["phone"]) or not _EMAIL.match(out["email"]):
        raise AgentError(400, "invalid", "date (AAAA-MM-JJ), pays (ISO 3), téléphone (+…) ou e-mail illisible")
    return out


def _threeds_public(t: ThreeDS) -> dict[str, Any]:
    """Ce que le noyau et le web voient d'un défi : détails de l'émetteur, ni référence de transaction ni carte."""
    return {"challenge_id": t.id, "amount_minor": t.amount_minor, "currency": t.currency,
            "merchant": t.merchant_name, "expires_at": int(t.expires_at)}


def _hashed_next_action(spt: dict[str, Any] | None) -> str | None:
    """`next_action.use_stripe_sdk.value` : ce que le web passe à `stripe.handleNextAction({hashedValue})`."""
    na = (spt or {}).get("next_action") or {}
    if na.get("type") != "use_stripe_sdk":
        return None
    val = (na.get("use_stripe_sdk") or {}).get("value")
    return val if isinstance(val, str) and 0 < len(val) <= 8192 else None


__all__ = ["AgentError", "EVENTS", "METHOD_AUD", "THREEDS_AUD", "PayAgent", "method_challenge", "sign_callback",
           "threeds_challenge"]
