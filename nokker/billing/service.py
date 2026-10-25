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

from nokker.billing.plans import ACTIVE_STATUSES, Plan
from nokker.billing.store import BillingStore, ts
from nokker.billing.stripe import StripeClient, StripeError, verify_signature
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.store import Account

logger = logging.getLogger("nokker.billing")

SUB_EVENTS = ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted")
CAPS_TTL_S = 30.0


class BillingError(Exception):
    """`code` : `unknown_plan`, `already_subscribed`, `no_customer`, `stripe_unavailable`, `unknown_price`, `bad_event`."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


class Billing:
    def __init__(self, *, store: BillingStore, stripe: StripeClient, plans: dict[str, Plan], prices: dict[str, str],
                 webhook_secret: str, app_url: str, clock: Callable[[], float] = time.time, coupon: str = "") -> None:
        if not webhook_secret:
            raise ValueError("secret de webhook requis")
        self.store, self.stripe, self.plans, self.prices = store, stripe, plans, prices
        # Coupon de bêta (`billing_coupon`, décision du 25.10) : id d'un coupon Stripe posé sur CHAQUE Checkout. Vide =
        # aucune remise.
        self.coupon = (coupon or "").strip()
        self._by_price = {v: k for k, v in prices.items()}
        self._secret, self._app_url, self._clock = webhook_secret, app_url.rstrip("/"), clock
        self._caps: dict[str, tuple[float, str | None, dict[str, int] | None]] = {}
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
                "plans": [{"id": p, "caps": self.plans[p].caps} for p in self.prices if p in self.plans]}

    # ── Checkout et portail ───────────────────────────────────────────────────────────────────────────────────────
    def _customer(self, account: Account) -> str:
        known = self.store.customer_for_tenant(account.tenant_id)
        if known:
            return known
        created = self.stripe.create_customer(account.email, account.tenant_id, account.account_id)
        return self.store.put_customer(created, account.tenant_id, account.account_id)

    def coupon_for(self, account: Account) -> str | None:
        """Coupon Stripe à appliquer au Checkout de ce compte, ou None. Bêta : le coupon global `billing_coupon` pour
        tous. L'ouverture publique demandera un coupon PAR COMPTE éligible (docs/LOKKAL-ROADMAP.md, 25.10) : ce
        résolveur est le seul endroit à changer."""
        return self.coupon or None

    def checkout(self, account: Account, plan: str) -> str:
        if plan not in self.prices or plan not in self.plans:
            raise BillingError("unknown_plan")
        with tenant_scope(account.tenant_id):
            if self._live(account.tenant_id):
                raise BillingError("already_subscribed", 409)  # changer de palier = portail Stripe
        coupon = self.coupon_for(account)
        try:
            customer = self._customer(account)

            def session(with_coupon: str | None) -> str:
                return self.stripe.checkout_session(
                    customer, self.prices[plan], account.tenant_id, plan, f"{self._app_url}/account?billing=success",
                    f"{self._app_url}/account?billing=cancel", account.lang,
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
                "cancel_at_period_end": bool(obj.get("cancel_at_period_end")), "last_event_at": ts(created)})
        with self._lock:
            self._caps.pop(tenant_id, None)
        return {"tenant": tenant_id, "plan": plan, "status": status, "applied": applied}
