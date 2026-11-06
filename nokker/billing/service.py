"""Abonnements : Checkout hébergé (Stripe garde la carte), portail client Stripe (factures, résiliation), webhook.

Le webhook est la SEULE source de vérité de l'état d'un abonnement : le retour du navigateur sur `success_url` ne
change rien. Il arrive sans locataire ; le locataire se retrouve par `stripe_customer` (`billing_customers`), et un
client inconnu (le compte Stripe de l'éditeur sert aussi d'autres produits) est ignoré sans erreur.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from nokker.billing.plans import ACTIVE_STATUSES, Plan
from nokker.billing.store import BillingStore, ts
from nokker.billing.stripe import StripeClient, StripeError, verify_signature
from nokker.billing.voucher import RESERVE_S, VoucherError, Vouchers, trial_end_for
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.store import Account

logger = logging.getLogger("nokker.billing")

SUB_EVENTS = ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted")
CHECKOUT_EVENTS = ("checkout.session.completed", "checkout.session.expired")
CAPS_TTL_S = 30.0
# Monnaie du Checkout et des prix affichés (décision produit du 05.11 : CHF partout). Les prix Stripe gardent leurs
# `currency_options` EUR ; la session impose le franc.
CURRENCY = "chf"
PRICE_TTL_S = 3600.0  # prix lus sur Stripe, gardés une heure
PRICE_RETRY_S = 300.0  # Stripe n'a pas répondu : pas de prix affiché, nouvel essai après 5 min
# Paliers hors coupon bêta (décision du 05.11 : le coupon ne vaut pas pour BYOK).
NO_COUPON_PLANS = frozenset({"byok"})


class BillingError(Exception):
    """`code` : `unknown_plan`, `already_subscribed`, `no_customer`, `stripe_unavailable`, `unknown_price`, `bad_event`,
    et les refus de voucher `code_invalid|code_expired|code_exhausted|code_used|code_customer`."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


class Billing:
    def __init__(self, *, store: BillingStore, stripe: StripeClient, plans: dict[str, Plan], prices: dict[str, str],
                 webhook_secret: str, app_url: str, clock: Callable[[], float] = time.time, coupon: str = "",
                 vouchers: Vouchers | None = None) -> None:
        if not webhook_secret:
            raise ValueError("secret de webhook requis")
        self.store, self.stripe, self.plans, self.prices = store, stripe, plans, prices
        # Coupon de bêta (`billing_coupon`, décision du 25.10) : id d'un coupon Stripe posé sur CHAQUE Checkout. Vide =
        # aucune remise.
        self.coupon = (coupon or "").strip()
        # Vouchers d'essai (nokker/billing/voucher.py) : None = pas d'essai possible (tout code est `code_invalid`).
        self.vouchers = vouchers
        self._by_price = {v: k for k, v in prices.items()}
        self._secret, self._app_url, self._clock = webhook_secret, app_url.rstrip("/"), clock
        self._caps: dict[str, tuple[float, str | None, dict[str, int] | None]] = {}
        self._prices: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._lock = threading.Lock()

    # ── état ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def _live(self, tenant_id: str) -> dict[str, Any] | None:
        sub = self.store.subscription(tenant_id)
        return sub if sub and sub["status"] in ACTIVE_STATUSES and sub["plan"] in self.plans else None

    def _tier(self, tenant_id: str) -> tuple[str | None, dict[str, int] | None]:
        """(palier actif, ses plafonds), ou (None, None). Gardé `CAPS_TTL_S` secondes, vidé par le webhook."""
        now = self._clock()
        with self._lock:
            hit = self._caps.get(tenant_id)
            if hit and now - hit[0] < CAPS_TTL_S:
                return hit[1], hit[2]
        sub = self._live(tenant_id)
        plan = sub["plan"] if sub else None
        caps = dict(self.plans[plan].caps) if plan else None
        with self._lock:
            self._caps[tenant_id] = (now, plan, caps)
        return plan, caps

    def caps_for(self, tenant_id: str) -> dict[str, int] | None:
        """Plafonds du palier payé du locataire, ou None (pas d'abonnement actif : plafonds par défaut de la bêta).
        Appelé à chaque tour : résultat gardé `CAPS_TTL_S` secondes, vidé par le webhook du locataire."""
        return self._tier(tenant_id)[1]

    def active_plan(self, tenant_id: str) -> str | None:
        """Palier ACTIF du locataire (statut dans `ACTIVE_STATUSES` : `past_due` le garde), ou None. Même cache que
        `caps_for` : le foyer le lit à chaque tour d'un membre (nokker/household/service.py::Household.tier)."""
        return self._tier(tenant_id)[0]

    def status(self, tenant_id: str) -> dict[str, Any]:
        sub = self.store.subscription(tenant_id)
        end = sub["current_period_end"] if sub else None
        return {"plan": sub["plan"] if sub and sub["status"] in ACTIVE_STATUSES else None,
                "status": sub["status"] if sub else None,
                "current_period_end": end.isoformat() if end else None,
                "cancel_at_period_end": bool(sub and sub["cancel_at_period_end"]),
                "trial_end": sub["trial_end"].isoformat() if sub and sub.get("trial_end") else None,
                "plans": [{"id": p, "caps": self.plans[p].caps, **self._price_field(p)}
                          for p in self.prices if p in self.plans]}

    # ── prix affichés ─────────────────────────────────────────────────────────────────────────────────────────────
    def _price_field(self, plan: str) -> dict[str, Any]:
        info = self.price(plan)
        return {"price": info} if info else {}

    def price(self, plan: str) -> dict[str, Any] | None:
        """`{amount, currency, interval}` du palier lu sur Stripe (seule source), en unités de la monnaie (19 = 19 CHF,
        9.5 = 9,50 CHF), monnaie en majuscules, `interval` de Stripe (`month`). None si Stripe ne répond pas ou si le
        prix n'a pas de montant en `CURRENCY` : l'écran n'affiche alors pas de prix. Gardé `PRICE_TTL_S`."""
        pid = self.prices.get(plan)
        if not pid:
            return None
        now = self._clock()
        with self._lock:
            hit = self._prices.get(pid)
        if hit and now < hit[0]:
            return hit[1]
        info: dict[str, Any] | None = None
        try:
            info = price_info(self.stripe.get_price(pid), CURRENCY)
            ttl = PRICE_TTL_S if info else PRICE_RETRY_S
        except StripeError as e:
            logger.warning("billing: prix %s illisible chez Stripe (%s) : pas de prix affiché pour %s", pid, e.code, plan)
            ttl = PRICE_RETRY_S
        with self._lock:
            self._prices[pid] = (now + ttl, info)
        return info

    def warm_prices(self) -> None:
        """Lit les prix de tous les paliers (au démarrage, hors du chemin d'une requête)."""
        for plan in self.prices:
            self.price(plan)

    # ── Checkout et portail ───────────────────────────────────────────────────────────────────────────────────────
    def _customer(self, account: Account) -> str:
        known = self.store.customer_for_tenant(account.tenant_id)
        if known:
            return known
        created = self.stripe.create_customer(account.email, account.tenant_id, account.account_id)
        return self.store.put_customer(created, account.tenant_id, account.account_id)

    def coupon_for(self, account: Account, plan: str = "") -> str | None:
        """Coupon Stripe à appliquer au Checkout de ce compte, ou None. Bêta : le coupon global `billing_coupon` pour
        tous les paliers hors `NO_COUPON_PLANS`. L'ouverture publique demandera un coupon PAR COMPTE éligible
        (docs/LOKKAL-ROADMAP.md, 25.10) : ce résolveur est le seul endroit à changer."""
        if plan in NO_COUPON_PLANS:
            return None
        return self.coupon or None

    # ── vouchers d'essai ──────────────────────────────────────────────────────────────────────────────────────────
    def _voucher_refusal(self, account: Account, code: str) -> str | None:
        if self.vouchers is None:
            return "code_invalid"
        with tenant_scope(account.tenant_id):
            if self.store.subscription(account.tenant_id) is not None:  # même annulé : l'essai est pour les nouveaux
                return "code_customer"
        return self.vouchers.check(code, account.tenant_id, account.email)

    def voucher_status(self, account: Account, code: str) -> dict[str, Any]:
        """Vérification sans écriture (`GET /v1/billing/voucher`) : `{ok, months, first_charge_on}` ou `{ok: false,
        error}`. La date est celle d'un Checkout fait maintenant."""
        why = self._voucher_refusal(account, code)
        if why:
            return {"ok": False, "error": why}
        assert self.vouchers is not None
        end = trial_end_for(self._clock(), self.vouchers.tz)
        return {"ok": True, "months": 1, "first_charge_on": end.astimezone(ZoneInfo(self.vouchers.tz)).date().isoformat()}

    def _trial_checkout(self, account: Account, plan: str, code: str) -> str:
        why = self._voucher_refusal(account, code)
        if why:
            raise BillingError(why)
        assert self.vouchers is not None
        try:
            use_id, trial_end = self.vouchers.reserve(code, account.tenant_id, account.email, plan)
        except VoucherError as e:
            raise BillingError(e.code, e.status) from None
        try:
            customer = self._customer(account)
            session, url = self.stripe.checkout_trial(
                customer, self.prices[plan], account.tenant_id, plan, f"{self._app_url}/account?billing=success",
                f"{self._app_url}/account?billing=cancel", account.lang, trial_end=int(trial_end.timestamp()),
                expires_at=int(self._clock()) + RESERVE_S, voucher=str(use_id), currency=CURRENCY)
        except StripeError:
            self.vouchers.release(use_id)  # le code unique n'est pas perdu si Stripe ne répond pas
            raise BillingError("stripe_unavailable", 502) from None
        self.vouchers.attach_session(use_id, session)
        return url

    def checkout(self, account: Account, plan: str, code: str = "") -> str:
        if plan not in self.prices or plan not in self.plans:
            raise BillingError("unknown_plan")
        with tenant_scope(account.tenant_id):
            if self._live(account.tenant_id):
                raise BillingError("already_subscribed", 409)  # changer de palier = portail Stripe
        if code:  # essai : jamais de coupon (le coupon bêta n'est pas cumulable)
            return self._trial_checkout(account, plan, code)
        coupon = self.coupon_for(account, plan)
        try:
            customer = self._customer(account)

            def session(with_coupon: str | None) -> str:
                return self.stripe.checkout_session(
                    customer, self.prices[plan], account.tenant_id, plan, f"{self._app_url}/account?billing=success",
                    f"{self._app_url}/account?billing=cancel", account.lang, currency=CURRENCY,
                    **({"coupon": with_coupon} if with_coupon else {}))

            if not coupon:
                return session(None)
            try:
                return session(coupon)
            except StripeError as e:
                if not 400 <= e.status < 500:  # Stripe injoignable ou en panne : pas un refus du coupon
                    raise
                # Coupon refusé (inconnu, expiré, épuisé…) : l'abonnement passe quand même, sans remise.
                logger.warning("billing: coupon %r refusé par Stripe (%s, %s) pour %s : Checkout sans remise",
                               coupon, e.code, e.status, account.tenant_id)
                return session(None)
        except StripeError:
            raise BillingError("stripe_unavailable", 502) from None

    def portal(self, account: Account) -> str:
        customer = self.store.customer_for_tenant(account.tenant_id)
        if not customer:
            raise BillingError("no_customer", 404)
        try:
            return self.stripe.portal_session(customer, f"{self._app_url}/account")
        except StripeError:
            raise BillingError("stripe_unavailable", 502) from None

    def cancel_for(self, tenant_id: str) -> int:
        """Résilie TOUT de suite les abonnements vivants du locataire (suppression du compte). Rend le nombre résilié.
        Idempotent : ne fait rien sans client Stripe ni abonnement vivant, et un abonnement déjà parti chez Stripe
        (resource_missing) compte comme résilié. La liste vient de Stripe et non de notre table, pour attraper un
        abonnement dont le webhook de création n'est pas encore arrivé. Aucun remboursement. Lève
        BillingError("stripe_unavailable") : l'appelant n'efface RIEN tant que ça échoue."""
        customer = self.store.customer_for_tenant(tenant_id)
        if not customer:
            return 0
        done = 0
        try:
            for sub in self.stripe.list_subscriptions(customer):
                if sub.get("status") in ("canceled", "incomplete_expired"):
                    continue
                try:
                    self.stripe.cancel_subscription(str(sub["id"]))
                except StripeError as e:
                    if e.code != "resource_missing":
                        raise
                done += 1
                self._mark_canceled(tenant_id, str(sub["id"]))
        except StripeError:
            raise BillingError("stripe_unavailable", 502) from None
        with self._lock:
            self._caps.pop(tenant_id, None)
        return done

    def _mark_canceled(self, tenant_id: str, subscription_id: str) -> None:
        """Ligne locale à « canceled » sans attendre le webhook ; l'horodatage local dépasse tout événement passé."""
        with tenant_scope(tenant_id):
            known = self.store.subscription(tenant_id)
            if known and known["stripe_subscription"] == subscription_id:
                self.store.upsert_subscription({**known, "status": "canceled", "cancel_at_period_end": False,
                                                "last_event_at": datetime.fromtimestamp(self._clock(), tz=timezone.utc)})

    # ── webhook ───────────────────────────────────────────────────────────────────────────────────────────────────
    def handle_webhook(self, payload: bytes, signature: str) -> dict[str, Any]:
        """Lève `StripeError("signature")` (400) ou `BillingError` (500 : Stripe rejouera). Sinon un petit compte rendu."""
        verify_signature(payload, signature, self._secret, now=self._clock())
        try:
            event = json.loads(payload)
            event_id, etype, created = str(event["id"]), str(event["type"]), int(event["created"])
            obj = event["data"]["object"]
        except (ValueError, KeyError, TypeError):
            raise BillingError("bad_event") from None
        if self.store.event_seen(event_id):
            return {"duplicate": True}
        result: dict[str, Any] = {"type": etype}
        if etype in SUB_EVENTS:
            result.update(self._subscription(etype, obj, created))
        elif etype in CHECKOUT_EVENTS and self.vouchers is not None and (obj.get("metadata") or {}).get("voucher"):
            result.update(self.vouchers.on_checkout(obj, completed=etype.endswith(".completed")))
        self.store.mark_event(event_id, etype)  # après le traitement : un échec laisse Stripe rejouer l'événement
        return result

    def _subscription(self, etype: str, obj: dict[str, Any], created: int) -> dict[str, Any]:
        owner = self.store.tenant_for_customer(str(obj.get("customer") or ""))
        if owner is None:
            return {"ignored": "unknown_customer"}
        tenant_id, _ = owner
        item = ((obj.get("items") or {}).get("data") or [{}])[0]
        price = str((item.get("price") or {}).get("id") or "")
        plan = self._by_price.get(price)
        if plan is None:
            logger.error("billing: prix Stripe inconnu %r pour %s (événement %s)", price, tenant_id, etype)
            raise BillingError("unknown_price", 500)
        status = "canceled" if etype.endswith(".deleted") else str(obj.get("status") or "")
        # L'API récente porte la fin de période sur l'élément ; l'ancienne, sur l'abonnement.
        end = obj.get("current_period_end") or item.get("current_period_end")
        with tenant_scope(tenant_id):
            applied = self.store.upsert_subscription({
                "stripe_subscription": str(obj["id"]), "stripe_customer": str(obj["customer"]), "plan": plan,
                "status": status, "current_period_end": ts(end),
                "cancel_at_period_end": bool(obj.get("cancel_at_period_end")), "last_event_at": ts(created),
                "trial_end": ts(obj.get("trial_end")) if status == "trialing" else None})
        with self._lock:
            self._caps.pop(tenant_id, None)
        out = {"tenant": tenant_id, "plan": plan, "status": status, "applied": applied}
        if self.vouchers is not None and applied:
            moved = self.vouchers.on_subscription(obj, status)
            if moved:
                out["voucher"] = moved
        return out

    # ── rappels de fin d'essai (J+23, J+28) ──────────────────────────────────────────────────────────────────────
    def remind_trials(self, send: Callable[[str, str, str, dict[str, Any]], None]) -> int:
        """Envoie les rappels dus : `send(tenant_id, account_id, kind, info)` avec `kind` = `trial_7d` | `trial_2d` et
        `info` = {plan, first_charge_at (ISO), price}. Marqué envoyé seulement après un envoi réussi ; un client
        inconnu ou un envoi en échec est retenté au passage suivant. Rend le nombre envoyé."""
        if self.vouchers is None:
            return 0
        self.vouchers.store.expire_reservations(datetime.fromtimestamp(self._clock(), tz=timezone.utc))
        sent = 0
        for use, col in self.vouchers.due_reminders():
            owner = self.store.tenant_for_customer(str(use.get("stripe_customer") or ""))
            if owner is None:
                continue
            with tenant_scope(owner[0]):
                sub = self.store.subscription(owner[0])
            if sub is not None and sub.get("cancel_at_period_end"):
                continue  # essai résilié : rien ne sera débité, pas de rappel (il repart si la résiliation est annulée)
            kind = "trial_7d" if col == "reminded_23" else "trial_2d"
            info = {"plan": use["plan"], "first_charge_at": use["trial_end"].isoformat(), "price": self.price(use["plan"])}
            try:
                send(owner[0], owner[1], kind, info)
            except Exception:  # noqa: BLE001 — un envoi raté est retenté ; il ne bloque pas les autres
                logger.warning("billing: rappel d'essai %s non envoyé", kind, exc_info=True)
                continue
            self.vouchers.mark_reminded(use, col)
            sent += 1
        return sent


def price_info(price: dict[str, Any], currency: str) -> dict[str, Any] | None:
    """`{amount, currency, interval}` d'un objet Price de Stripe dans `currency` (monnaie principale ou
    `currency_options`), ou None s'il n'y a pas de montant dans cette monnaie."""
    cur = currency.lower()
    if str(price.get("currency") or "").lower() == cur:
        cents = price.get("unit_amount")
    else:
        cents = ((price.get("currency_options") or {}).get(cur) or {}).get("unit_amount")
    if not isinstance(cents, int):
        return None
    amount: float | int = cents // 100 if cents % 100 == 0 else cents / 100
    interval = str((price.get("recurring") or {}).get("interval") or "") or None
    return {"amount": amount, "currency": cur.upper(), "interval": interval}
