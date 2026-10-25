import json

import pytest

from nokker.billing import http
from nokker.billing.plans import DEFAULT_PLANS, plans_from_config
from nokker.billing.service import Billing, BillingError
from nokker.billing.store import MemoryBilling
from nokker.billing.stripe import StripeClient, StripeError, sign
from nokker.tenancy.store import Account

SECRET, NOW = "whsec_t", 2_000_000_000
A = Account(tenant_id="tn_aaaaaaaaaaaa", account_id="us_a", email="a@example.org", lang="fr")
B = Account(tenant_id="tn_bbbbbbbbbbbb", account_id="us_b", email="b@example.org", lang="en")
PRICES = {"essentiel": "price_ess", "solo": "price_solo"}


class FakeStripe(StripeClient):
    def __init__(self):
        self.calls = []
        self.n = 0
        self.fail = False
        self.subs = {}
        self.gone = set()

    def create_customer(self, email, tenant_id, account_id):
        self.n += 1
        self.calls.append(("customer", email))
        return f"cus_{self.n}"

    def checkout_session(self, customer, price_id, tenant_id, plan, success_url, cancel_url, lang):
        if self.fail:
            raise StripeError("unavailable")
        self.calls.append(("checkout", customer, price_id, success_url, lang))
        return "https://checkout.stripe.test/s"

    def list_subscriptions(self, customer):
        self.calls.append(("list", customer))
        if self.fail:
            raise StripeError("unavailable")
        return self.subs.get(customer, [])

    def cancel_subscription(self, subscription_id):
        self.calls.append(("cancel", subscription_id))
        if subscription_id in self.gone:
            raise StripeError("resource_missing", 404)
        return {"id": subscription_id, "status": "canceled"}

    def portal_session(self, customer, return_url):
        self.calls.append(("portal", customer, return_url))
        return "https://billing.stripe.test/p"


@pytest.fixture
def b():
    clock = [NOW]
    bill = Billing(store=MemoryBilling(), stripe=FakeStripe(), plans=plans_from_config(None), prices=PRICES,
                   webhook_secret=SECRET, app_url="https://app.nokker.ai/", clock=lambda: clock[0])
    bill.clock = clock
    return bill


def event(eid, etype, customer, price="price_solo", status="active", sub="sub_1", created=NOW, **extra):
    obj = {"id": sub, "customer": customer, "status": status, "cancel_at_period_end": False,
           "items": {"data": [{"price": {"id": price}, "current_period_end": NOW + 86400}]}, **extra}
    return json.dumps({"id": eid, "type": etype, "created": created, "data": {"object": obj}}).encode()


def send(bill, payload):
    return bill.handle_webhook(payload, sign(payload, SECRET, NOW))


def test_checkout_creates_one_customer_and_uses_the_price(b):
    assert b.checkout(A, "solo") == "https://checkout.stripe.test/s"
    b.checkout(A, "essentiel")
    assert [c[0] for c in b.stripe.calls] == ["customer", "checkout", "checkout"]
    assert b.stripe.calls[1][2:] == ("price_solo", "https://app.nokker.ai/account?billing=success", "fr")


def test_checkout_refuses_unknown_plan_and_stripe_outage(b):
    with pytest.raises(BillingError) as e:
        b.checkout(A, "pro")
    assert e.value.code == "unknown_plan"
    b.stripe.fail = True
    with pytest.raises(BillingError) as e:
        b.checkout(A, "solo")
    assert (e.value.code, e.value.status) == ("stripe_unavailable", 502)


def test_portal_needs_a_customer(b):
    with pytest.raises(BillingError) as e:
        b.portal(A)
    assert (e.value.code, e.value.status) == ("no_customer", 404)
    b.checkout(A, "solo")
    assert b.portal(A) == "https://billing.stripe.test/p"


def test_subscription_lifecycle_sets_then_clears_caps(b):
    b.checkout(A, "solo")
    assert b.caps_for(A.tenant_id) is None
    assert send(b, event("evt_1", "customer.subscription.created", "cus_1"))["applied"] is True
    b.clock[0] += 1
    assert b.caps_for(A.tenant_id) == DEFAULT_PLANS["solo"].caps
    assert b.caps_for(B.tenant_id) is None
    assert b.status(A.tenant_id)["plan"] == "solo" and b.status(A.tenant_id)["current_period_end"]
    send(b, event("evt_2", "customer.subscription.deleted", "cus_1", status="active", created=NOW + 5))
    assert b.caps_for(A.tenant_id) is None  # cache vidé par le webhook, pas d'attente des 30 s
    assert b.status(A.tenant_id)["plan"] is None and b.status(A.tenant_id)["status"] == "canceled"


def test_past_due_keeps_caps_unpaid_does_not(b):
    b.checkout(A, "solo")
    send(b, event("evt_1", "customer.subscription.updated", "cus_1", status="past_due"))
    assert b.caps_for(A.tenant_id) is not None
    send(b, event("evt_2", "customer.subscription.updated", "cus_1", status="unpaid", created=NOW + 1))
    assert b.caps_for(A.tenant_id) is None


def test_already_subscribed_goes_to_portal(b):
    b.checkout(A, "solo")
    send(b, event("evt_1", "customer.subscription.created", "cus_1"))
    with pytest.raises(BillingError) as e:
        b.checkout(A, "essentiel")
    assert (e.value.code, e.value.status) == ("already_subscribed", 409)


def test_duplicate_event_is_processed_once(b):
    b.checkout(A, "solo")
    p = event("evt_1", "customer.subscription.created", "cus_1")
    assert send(b, p)["applied"] is True
    assert send(b, p) == {"duplicate": True}


def test_stale_event_does_not_overwrite_a_newer_state(b):
    b.checkout(A, "solo")
    send(b, event("evt_new", "customer.subscription.updated", "cus_1", status="canceled", created=NOW + 10))
    r = send(b, event("evt_old", "customer.subscription.updated", "cus_1", status="active", created=NOW))
    assert r["applied"] is False and b.status(A.tenant_id)["status"] == "canceled"


def test_foreign_customer_is_ignored_and_unknown_price_is_retried(b):
    assert send(b, event("evt_x", "customer.subscription.created", "cus_inconnu"))["ignored"] == "unknown_customer"
    b.checkout(A, "solo")
    with pytest.raises(BillingError) as e:
        send(b, event("evt_y", "customer.subscription.created", "cus_1", price="price_autre"))
    assert (e.value.code, e.value.status) == ("unknown_price", 500)
    assert not b.store.event_seen("evt_y")  # non marqué : Stripe rejoue


def test_other_event_types_are_acknowledged(b):
    assert send(b, event("evt_i", "invoice.paid", "cus_1")) == {"type": "invoice.paid"}
    assert b.store.event_seen("evt_i")


def test_bad_signature_and_bad_payload(b):
    p = event("evt_1", "invoice.paid", "cus_1")
    with pytest.raises(StripeError):
        b.handle_webhook(p, sign(p, "autre", NOW))
    junk = b"{}"
    with pytest.raises(BillingError) as e:
        b.handle_webhook(junk, sign(junk, SECRET, NOW))
    assert e.value.code == "bad_event"


def test_tenants_do_not_see_each_others_subscription(b):
    b.checkout(A, "solo")
    b.checkout(B, "essentiel")
    send(b, event("evt_a", "customer.subscription.created", "cus_1", sub="sub_a"))
    send(b, event("evt_b", "customer.subscription.created", "cus_2", price="price_ess", sub="sub_b"))
    assert b.status(A.tenant_id)["plan"] == "solo" and b.status(B.tenant_id)["plan"] == "essentiel"


def test_plans_from_config_overrides_caps_without_losing_keys():
    p = plans_from_config({"solo": {"deep_per_day": 40}, "pro": {"turns_per_day": 500, "deep_per_day": 60}})
    assert p["solo"].caps == {"turns_per_day": 150, "deep_per_day": 40} and p["pro"].caps["turns_per_day"] == 500
    assert DEFAULT_PLANS["solo"].caps["deep_per_day"] == 20


class H:
    def __init__(self, raw=b"", sig="", body=None):
        import io
        self.headers = {"Content-Length": str(len(raw)), "Stripe-Signature": sig}
        self.rfile, self._body, self.out = io.BytesIO(raw), body or {}, None

    def _json(self, status, obj):
        self.out = (status, obj)

    def _read_json_body(self):
        return self._body


def test_http_is_inert_until_configured_then_routes(b):
    http.configure(None)
    assert http.handle_webhook(H(), "POST", "/v1/billing/webhook") is False
    assert http.handle(H(), "GET", "/v1/billing/plan", A) is False
    http.configure(b)
    try:
        h = H(body={"plan": "solo"})
        assert http.handle(h, "POST", "/v1/billing/checkout", A) and h.out == (200, {"url": "https://checkout.stripe.test/s"})
        h = H(body={"plan": "x"})
        http.handle(h, "POST", "/v1/billing/checkout", A)
        assert h.out == (400, {"error": "unknown_plan"})
        p = event("evt_h", "customer.subscription.created", "cus_1")
        h = H(p, sign(p, SECRET, NOW))
        assert http.handle_webhook(h, "POST", "/v1/billing/webhook") and h.out == (200, {"received": True})
        h = H(p, "t=1,v1=00")
        http.handle_webhook(h, "POST", "/v1/billing/webhook")
        assert h.out == (400, {"error": "signature"})
        h = H()
        http.handle(h, "GET", "/v1/billing/plan", A)
        assert h.out[0] == 200 and h.out[1]["plan"] == "solo"
        assert http.handle(H(), "GET", "/v1/billing/inconnu", A) is False
    finally:
        http.configure(None)


def test_late_event_on_a_cancelled_subscription_does_not_hide_the_active_one(b):
    b.checkout(A, "solo")
    send(b, event("evt_a1", "customer.subscription.created", "cus_1", sub="sub_A"))
    send(b, event("evt_a2", "customer.subscription.deleted", "cus_1", sub="sub_A", created=NOW + 10))
    send(b, event("evt_b1", "customer.subscription.created", "cus_1", price="price_ess", sub="sub_B", created=NOW + 20))
    send(b, event("evt_a3", "customer.subscription.updated", "cus_1", status="canceled", sub="sub_A", created=NOW + 30))
    assert b.status(A.tenant_id)["plan"] == "essentiel"
    b.clock[0] += 60
    assert b.caps_for(A.tenant_id) == DEFAULT_PLANS["essentiel"].caps


def test_same_second_update_cannot_resurrect_a_cancelled_subscription(b):
    b.checkout(A, "solo")
    send(b, event("evt_1", "customer.subscription.deleted", "cus_1", created=NOW + 5))
    assert send(b, event("evt_2", "customer.subscription.updated", "cus_1", status="active", created=NOW + 5))["applied"] is False
    assert b.status(A.tenant_id)["status"] == "canceled"


def test_cancel_for_without_customer_or_subscription_does_nothing(b):
    assert b.cancel_for(A.tenant_id) == 0
    b.checkout(A, "solo")  # client créé, aucun abonnement chez Stripe
    assert b.cancel_for(A.tenant_id) == 0
    assert [c[0] for c in b.stripe.calls if c[0] == "cancel"] == []


def test_cancel_for_cancels_live_subscriptions_now_and_marks_the_row(b):
    b.checkout(A, "solo")
    send(b, event("evt_1", "customer.subscription.created", "cus_1"))
    assert b.caps_for(A.tenant_id) is not None
    b.stripe.subs["cus_1"] = [{"id": "sub_1", "status": "active"}, {"id": "sub_old", "status": "canceled"}]
    b.clock[0] += 60
    assert b.cancel_for(A.tenant_id) == 1
    assert ("cancel", "sub_1") in b.stripe.calls and ("cancel", "sub_old") not in b.stripe.calls
    assert b.status(A.tenant_id)["status"] == "canceled" and b.status(A.tenant_id)["plan"] is None
    assert b.caps_for(A.tenant_id) is None  # sans attendre le webhook
    # le webhook en retard (créé AVANT la résiliation locale) ne ressuscite rien
    send(b, event("evt_2", "customer.subscription.updated", "cus_1", status="active", created=NOW + 5))
    assert b.status(A.tenant_id)["status"] == "canceled"


def test_cancel_for_is_idempotent_and_tolerates_a_subscription_already_gone(b):
    b.checkout(A, "solo")
    b.stripe.subs["cus_1"] = [{"id": "sub_1", "status": "active"}]
    b.stripe.gone.add("sub_1")
    assert b.cancel_for(A.tenant_id) == 1
    b.stripe.subs["cus_1"] = [{"id": "sub_1", "status": "canceled"}]
    assert b.cancel_for(A.tenant_id) == 0


def test_cancel_for_stripe_outage_raises_so_that_nothing_gets_erased(b):
    b.checkout(A, "solo")
    b.stripe.fail = True
    with pytest.raises(BillingError) as e:
        b.cancel_for(A.tenant_id)
    assert (e.value.code, e.value.status) == ("stripe_unavailable", 502)
