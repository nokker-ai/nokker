"""Vouchers d'essai d'un mois (nokker/billing/voucher.py) : règles, Checkout d'essai, webhook, rappels, console."""
import json
from datetime import datetime, timezone
from urllib import parse as urllib_parse

import pytest

from nokker.billing.plans import plans_from_config
from nokker.billing.service import Billing, BillingError
from nokker.billing.store import MemoryBilling
from nokker.billing.stripe import StripeClient, StripeError, sign
from nokker.billing.voucher import (MemoryVouchers, VoucherError, Vouchers, end_of_day, new_single_code, norm_code,
                                    norm_email, trial_end_for)
from nokker.tenancy.store import Account
from tests.unit.billing.test_service import NOW, PRICES, SECRET, FakeStripe

SALT = "sel-de-test-0123456789"
A = Account(tenant_id="tn_aaaaaaaaaaaa", account_id="us_a", email="a@example.org", lang="fr")
B = Account(tenant_id="tn_bbbbbbbbbbbb", account_id="us_b", email="b@example.org", lang="en")
C = Account(tenant_id="tn_cccccccccccc", account_id="us_c", email="A+promo@Example.org", lang="fr")  # même adresse que A
DAY = 86400


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


class TrialStripe(FakeStripe):
    def __init__(self):
        super().__init__()
        self.trials = []

    def checkout_trial(self, customer, price_id, tenant_id, plan, success_url, cancel_url, lang, *, trial_end,
                       expires_at, voucher, currency=None):
        if self.fail:
            raise StripeError("unavailable")
        self.trials.append({"customer": customer, "price": price_id, "plan": plan, "trial_end": trial_end,
                            "expires_at": expires_at, "voucher": voucher, "currency": currency})
        return f"cs_{len(self.trials)}", "https://checkout.stripe.test/trial"


@pytest.fixture
def env():
    clock = [float(NOW)]
    v = Vouchers(MemoryVouchers(), SALT, clock=lambda: clock[0])
    stripe = TrialStripe()
    bill = Billing(store=MemoryBilling(), stripe=stripe, plans=plans_from_config(None), prices=PRICES,
                   webhook_secret=SECRET, app_url="https://app.nokker.ai", clock=lambda: clock[0], coupon="COUPON_BETA",
                   vouchers=v)
    v.create(campaign="lancement", kind="shared", code="NOKKER1", expires="2033-12-31", actor="ops@x")
    return bill, v, stripe, clock


def hook(bill, eid, etype, obj, created=NOW):
    payload = json.dumps({"id": eid, "type": etype, "created": created, "data": {"object": obj}}).encode()
    return bill.handle_webhook(payload, sign(payload, SECRET, int(bill._clock())))


# ── formes ──
def test_codes_and_addresses_are_normalised():
    assert norm_code(" nk-ab cd-2345 ") == "NK-ABCD-2345"
    assert norm_code("a") == "" and norm_code("x" * 41) == "" and norm_code("<script>") == ""
    assert norm_email(" A+promo@Example.ORG ") == "a@example.org"
    c = new_single_code()
    assert norm_code(c) == c and len(c) == 12 and not set(c[3:].replace("-", "")) & set("01OI")


@pytest.mark.parametrize("now, end", [
    (utc(2026, 1, 31, 12), utc(2026, 2, 28, 22, 59, 59)),     # 31 janvier → dernier jour de février (hiver : UTC+1)
    (utc(2026, 12, 15, 9), utc(2027, 1, 15, 22, 59, 59)),     # décembre → janvier de l'année suivante
    (utc(2026, 11, 5, 23, 30), utc(2026, 12, 6, 22, 59, 59)),  # 23:30 UTC = 6 novembre à Zurich
    (utc(2026, 3, 29, 12), utc(2026, 4, 29, 21, 59, 59)),     # été : UTC+2
])
def test_trial_ends_same_day_next_month_end_of_day_zurich(now, end):
    assert trial_end_for(now.timestamp()) == end


def test_end_of_day_is_zurich():
    assert end_of_day("2026-07-01") == utc(2026, 7, 1, 21, 59, 59)


# ── création (console) ──
def test_create_shared_and_single_codes(env):
    _, v, _, _ = env
    assert v.create(campaign="radio", kind="shared", code="radio 26", expires="2034-01-31", actor="o", max_uses=50) == ["RADIO26"]
    batch = v.create(campaign="flyers", kind="single", count=20, expires="2034-01-31", actor="o")
    assert len(set(batch)) == 20 and all(v.store.get(c)["max_uses"] == 1 for c in batch)
    assert v.store.codes("flyers", True) == sorted(batch)


@pytest.mark.parametrize("kw, err", [
    ({"campaign": "Bad Name", "kind": "shared", "code": "OK1", "expires": "2034-01-01"}, "bad_campaign"),
    ({"campaign": "c", "kind": "other", "code": "OK1", "expires": "2034-01-01"}, "bad_kind"),
    ({"campaign": "c", "kind": "shared", "code": "OK1", "expires": "2020-01-01"}, "bad_expires"),
    ({"campaign": "c", "kind": "shared", "code": "OK1", "expires": "demain"}, "bad_expires"),
    ({"campaign": "c", "kind": "shared", "code": "?", "expires": "2034-01-01"}, "bad_code"),
    ({"campaign": "c", "kind": "single", "count": 0, "expires": "2034-01-01"}, "bad_count"),
    ({"campaign": "c", "kind": "single", "count": 5001, "expires": "2034-01-01"}, "bad_count"),
    ({"campaign": "c", "kind": "shared", "code": "NOKKER1", "expires": "2034-01-01"}, "code_exists"),
])
def test_create_refusals(env, kw, err):
    with pytest.raises(VoucherError) as e:
        env[1].create(actor="o", **kw)
    assert e.value.code == err


def test_salt_is_required():
    with pytest.raises(ValueError):
        Vouchers(MemoryVouchers(), "court", clock=lambda: NOW)


# ── vérification (aucune écriture) ──
def test_status_ok_gives_first_charge_date_and_writes_nothing(env):
    bill, v, _, _ = env
    r = bill.voucher_status(A, "nokker1")
    assert r == {"ok": True, "months": 1,
                 "first_charge_on": trial_end_for(NOW).astimezone(__import__("zoneinfo").ZoneInfo("Europe/Zurich")).date().isoformat()}
    assert v.store.uses == []


def test_refusals(env):
    bill, v, _, clock = env
    assert bill.voucher_status(A, "INCONNU")["error"] == "code_invalid"
    assert bill.voucher_status(A, "")["error"] == "code_invalid"
    v.create(campaign="old", kind="shared", code="BIENTOT", expires="2034-01-01", actor="o")
    clock[0] = end_of_day("2034-01-02").timestamp()
    assert bill.voucher_status(A, "BIENTOT")["error"] == "code_expired"
    clock[0] = float(NOW)
    v.store.disable("BIENTOT", utc(2030, 1, 1))
    assert bill.voucher_status(A, "BIENTOT")["error"] == "code_invalid"


def test_no_vouchers_service_means_every_code_is_invalid():
    bill = Billing(store=MemoryBilling(), stripe=TrialStripe(), plans=plans_from_config(None), prices=PRICES,
                   webhook_secret=SECRET, app_url="https://app", clock=lambda: NOW)
    assert bill.voucher_status(A, "NOKKER1") == {"ok": False, "error": "code_invalid"}
    with pytest.raises(BillingError) as e:
        bill.checkout(A, "solo", "NOKKER1")
    assert e.value.code == "code_invalid"


# ── Checkout d'essai ──
def test_trial_checkout_card_always_no_coupon_reservation(env):
    bill, v, stripe, _ = env
    assert bill.checkout(A, "solo", "nokker1") == "https://checkout.stripe.test/trial"
    t = stripe.trials[0]
    assert t["trial_end"] == int(trial_end_for(NOW).timestamp()) and t["expires_at"] == NOW + 1800
    assert t["currency"] == "chf" and t["price"] == "price_solo"
    assert not any(c[0] == "checkout" for c in stripe.calls)  # jamais le Checkout avec coupon bêta
    (use,) = v.store.uses
    assert use["state"] == "reserved" and use["checkout_session"] == "cs_1" and t["voucher"] == str(use["id"])
    assert A.email not in json.dumps(use, default=str) and A.tenant_id not in json.dumps(use, default=str)


def test_one_trial_per_account_and_per_address_all_campaigns(env):
    bill, v, _, _ = env
    v.create(campaign="autre", kind="shared", code="AUTRE1", expires="2033-12-31", actor="o")
    bill.checkout(A, "solo", "NOKKER1")
    assert bill.voucher_status(A, "AUTRE1")["error"] == "code_used"   # même compte, autre campagne
    assert bill.voucher_status(C, "AUTRE1")["error"] == "code_used"   # même adresse (+étiquette, majuscules)
    assert bill.voucher_status(B, "AUTRE1")["ok"] is True


def test_single_code_cannot_go_twice_in_parallel_and_comes_back_after_expiry(env):
    bill, v, _, clock = env
    (code,) = v.create(campaign="flyers", kind="single", count=1, expires="2033-12-31", actor="o")
    bill.checkout(A, "solo", code)
    with pytest.raises(BillingError) as e:
        bill.checkout(B, "solo", code)
    assert e.value.code == "code_exhausted"
    hook(bill, "evt_x", "checkout.session.expired", {"id": "cs_1", "metadata": {"voucher": "1"}})
    assert v.store.uses[0]["state"] == "expired"
    assert bill.checkout(B, "essentiel", code)
    # sans webhook, une réservation périmée (> 30 min) se libère aussi d'elle-même
    (code2,) = v.create(campaign="flyers", kind="single", count=1, expires="2033-12-31", actor="o")
    D = Account(tenant_id="tn_dddddddddddd", account_id="us_d", email="d@example.org", lang="fr")
    E = Account(tenant_id="tn_eeeeeeeeeeee", account_id="us_e", email="e@example.org", lang="fr")
    bill.checkout(D, "solo", code2)
    clock[0] += 1801
    assert bill.checkout(E, "solo", code2)


def test_stripe_down_releases_the_reservation(env):
    bill, v, stripe, _ = env
    stripe.fail = True
    with pytest.raises(BillingError) as e:
        bill.checkout(A, "solo", "NOKKER1")
    assert e.value.code == "stripe_unavailable" and v.store.uses[0]["state"] == "expired"
    stripe.fail = False
    assert bill.checkout(A, "solo", "NOKKER1")


def test_former_subscriber_is_refused(env):
    bill, _, _, _ = env
    bill.store.put_customer("cus_old", A.tenant_id, A.account_id)
    hook(bill, "evt_s", "customer.subscription.deleted",
         {"id": "sub_old", "customer": "cus_old", "status": "canceled", "items": {"data": [{"price": {"id": "price_solo"}}]}})
    assert bill.voucher_status(A, "NOKKER1")["error"] == "code_customer"


def test_shared_code_max_uses(env):
    bill, v, _, _ = env
    v.create(campaign="radio", kind="shared", code="RADIO2", expires="2033-12-31", actor="o", max_uses=1)
    bill.checkout(A, "solo", "RADIO2")
    assert bill.voucher_status(B, "RADIO2")["error"] == "code_exhausted"


def test_checkout_without_code_keeps_the_beta_coupon(env):
    bill, _, stripe, _ = env
    bill.checkout(A, "solo")
    assert stripe.calls[-1][-1] == "COUPON_BETA" and not stripe.trials


# ── webhook ──
def _trial(bill, stripe, acc=A, plan="solo"):
    bill.checkout(acc, plan, "NOKKER1")
    cs = f"cs_{len(stripe.trials)}"
    customer = bill.store.customer_for_tenant(acc.tenant_id)
    hook(bill, f"evt_c{cs}", "checkout.session.completed",
         {"id": cs, "subscription": f"sub_{cs}", "customer": customer, "metadata": {"voucher": "1"}})
    return customer, f"sub_{cs}"


def test_completed_then_trialing_subscription_then_conversion(env):
    bill, v, stripe, _ = env
    customer, sub = _trial(bill, stripe)
    assert v.store.uses[0]["state"] == "trialing" and v.store.uses[0]["stripe_customer"] == customer
    end = int(trial_end_for(NOW).timestamp())
    obj = {"id": sub, "customer": customer, "status": "trialing", "trial_end": end,
           "items": {"data": [{"price": {"id": "price_solo"}, "current_period_end": end}]}}
    hook(bill, "evt_t", "customer.subscription.created", obj, created=NOW + 1)
    st = bill.status(A.tenant_id)
    assert st["plan"] == "solo" and st["status"] == "trialing" and st["trial_end"] == trial_end_for(NOW).isoformat()
    assert bill.caps_for(A.tenant_id) is not None  # l'essai donne les plafonds de la formule
    r = hook(bill, "evt_a", "customer.subscription.updated", {**obj, "status": "active"}, created=NOW + 2)
    assert r["voucher"] == "converted" and v.store.uses[0]["state"] == "converted"
    assert bill.status(A.tenant_id)["trial_end"] is None


def test_cancel_during_trial(env):
    bill, v, stripe, _ = env
    customer, sub = _trial(bill, stripe)
    hook(bill, "evt_d", "customer.subscription.deleted",
         {"id": sub, "customer": customer, "status": "canceled", "items": {"data": [{"price": {"id": "price_solo"}}]}},
         created=NOW + 5)
    assert v.store.uses[0]["state"] == "canceled"
    assert bill.voucher_status(A, "NOKKER1")["error"] in ("code_used", "code_customer")


def test_checkout_event_without_voucher_metadata_is_ignored(env):
    bill, v, _, _ = env
    assert "voucher" not in hook(bill, "evt_n", "checkout.session.completed", {"id": "cs_z", "metadata": {}})


# ── rappels ──
def test_reminders_at_23_and_28_days_once_each(env):
    bill, v, stripe, clock = env
    _trial(bill, stripe)
    sent = []
    send = lambda t, a, kind, info: sent.append((t, a, kind, info["plan"]))  # noqa: E731
    end = trial_end_for(NOW).timestamp()
    clock[0] = end - 8 * DAY
    assert bill.remind_trials(send) == 0
    clock[0] = end - 7 * DAY + 60
    assert bill.remind_trials(send) == 1 and bill.remind_trials(send) == 0
    clock[0] = end - 2 * DAY + 60
    assert bill.remind_trials(send) == 1 and bill.remind_trials(send) == 0
    assert [s[2] for s in sent] == ["trial_7d", "trial_2d"] and sent[0][:2] == (A.tenant_id, A.account_id)


def test_late_pass_sends_only_the_latest_reminder_and_failed_send_is_retried(env):
    bill, v, stripe, clock = env
    _trial(bill, stripe)
    clock[0] = trial_end_for(NOW).timestamp() - DAY
    calls = []

    def boom(*a):
        calls.append(a)
        raise OSError("relais")

    assert bill.remind_trials(boom) == 0 and len(calls) == 1
    sent = []
    assert bill.remind_trials(lambda *a: sent.append(a[2])) == 1 and sent == ["trial_2d"]
    assert v.store.uses[0]["reminded_23"] and v.store.uses[0]["reminded_28"]


def test_cancelled_trial_gets_no_reminder_until_resumed(env):
    """Essai résilié pendant l'essai (`cancel_at_period_end`) : aucun débit à venir, donc pas de « X CHF seront
    débités » ; s'il reprend (résiliation annulée), le rappel dû part."""
    bill, v, stripe, clock = env
    customer, sub = _trial(bill, stripe)
    end = int(trial_end_for(NOW).timestamp())
    obj = {"id": sub, "customer": customer, "status": "trialing", "trial_end": end, "cancel_at_period_end": True,
           "items": {"data": [{"price": {"id": "price_solo"}, "current_period_end": end}]}}
    hook(bill, "evt_t", "customer.subscription.updated", obj, created=NOW + 1)
    clock[0] = end - 7 * DAY + 60
    sent = []
    assert bill.remind_trials(lambda *a: sent.append(a[2])) == 0 and sent == []
    assert not v.store.uses[0]["reminded_23"]
    hook(bill, "evt_r", "customer.subscription.updated", {**obj, "cancel_at_period_end": False}, created=NOW + 2)
    assert bill.remind_trials(lambda *a: sent.append(a[2])) == 1 and sent == ["trial_7d"]


# ── client Stripe ──
def test_checkout_trial_params():
    seen = []

    def transport(method, url, body, headers, timeout):
        seen.append(dict(urllib_parse.parse_qsl(body.decode())))
        return 200, {"id": "cs_1", "url": "https://checkout.stripe.com/x"}

    c = StripeClient("sk_test_x", transport=transport)
    assert c.checkout_trial("cus_1", "price_solo", "tn_a", "solo", "s", "c", "fr", trial_end=2_000_000_000,
                            expires_at=1_999_000_000, voucher="7", currency="chf") == ("cs_1", "https://checkout.stripe.com/x")
    p = seen[0]
    assert p["payment_method_collection"] == "always" and p["subscription_data[trial_end]"] == "2000000000"
    assert p["subscription_data[trial_settings][end_behavior][missing_payment_method]"] == "cancel"
    assert p["metadata[voucher]"] == "7" and p["currency"] == "chf" and p["expires_at"] == "1999000000"
    assert not any(k.startswith("discounts") for k in p)


# ── HTTP ──
class H:
    def __init__(self, path, body=None):
        self.path, self._body, self.out = path, body or {}, None
        self.headers, self.client_address = {}, ("203.0.113.9", 1)

    def _json(self, status, obj):
        self.out = (status, obj)

    def _read_json_body(self):
        return self._body


def test_http_voucher_route_and_rate_limit(env, monkeypatch):
    from nokker.billing import http

    bill = env[0]
    http.configure(bill)
    monkeypatch.setattr(http, "_limits", http._HourLimiter())
    try:
        h = H("/v1/billing/voucher?code=nokker1")
        assert http.handle(h, "GET", "/v1/billing/voucher", A) and h.out == (200, bill.voucher_status(A, "NOKKER1"))
        for _ in range(http.VOUCHER_PER_ACCOUNT_H - 1):
            http.handle(H("/v1/billing/voucher?code=x"), "GET", "/v1/billing/voucher", A)
        h = H("/v1/billing/voucher?code=x")
        http.handle(h, "GET", "/v1/billing/voucher", A)
        assert h.out == (429, {"error": "rate_limited"})
        h = H("/v1/billing/checkout", {"plan": "solo", "code": "NOKKER1"})
        http.handle(h, "POST", "/v1/billing/checkout", B)
        assert h.out == (200, {"url": "https://checkout.stripe.test/trial"})
        h = H("/v1/billing/checkout", {"plan": "solo", "code": "NOPE1"})
        http.handle(h, "POST", "/v1/billing/checkout", C)
        assert h.out == (400, {"error": "code_invalid"})
    finally:
        http.configure(None)


def test_http_voucher_route_absent_when_vouchers_off(env, monkeypatch):
    """Réglage éteint : GET /v1/billing/voucher n'existe pas (404 du serveur), comme POST /v1/onboarding/voucher ; le
    web masque alors le champ du code. Le Checkout sans code reste servi."""
    from nokker.billing import http

    bill = env[0]
    bill.vouchers = None
    http.configure(bill)
    monkeypatch.setattr(http, "_limits", http._HourLimiter())
    try:
        h = H("/v1/billing/voucher?code=nokker1")
        assert http.handle(h, "GET", "/v1/billing/voucher", A) is False and h.out is None
        h = H("/v1/billing/plan")
        assert http.handle(h, "GET", "/v1/billing/plan", A) and h.out[0] == 200
    finally:
        http.configure(None)


# ── le code sert d'invitation (voucher_grants_invite) ──
def test_invite_needs_the_setting(env):
    with pytest.raises(VoucherError) as e:
        env[1].invite("NOKKER1", "new@example.org")
    assert (e.value.code, e.value.status) == ("not_enabled", 404)


def test_invite_reserves_before_any_account_then_first_checkout_takes_it_over(env):
    bill, v, stripe, _ = env
    v.grants_invite = True
    (code,) = v.create(campaign="flyers", kind="single", count=1, expires="2033-12-31", actor="o")
    uid = v.invite(code, "New+x@Example.org")
    assert v.invite(code, "new@example.org") == uid  # redemandée : même réservation, aucun second usage
    with pytest.raises(VoucherError) as e:
        v.invite(code, "other@example.org")  # épuisé AVANT de créer un compte
    assert e.value.code == "code_exhausted"
    newbie = Account(tenant_id="tn_nnnnnnnnnnnn", account_id="us_n", email="new@example.org", lang="fr")
    assert bill.voucher_status(newbie, code)["ok"] is True
    bill.checkout(newbie, "solo", code)
    (use,) = v.store.uses
    assert use["id"] == uid and use["state"] == "reserved" and use["checkout_session"] == "cs_1"
    assert use["plan"] == "solo" and not use["tenant_key"].startswith("pending:")
    assert bill.voucher_status(B, code)["error"] == "code_exhausted"


def test_invite_refusals_are_the_same(env):
    bill, v, _, _ = env
    v.grants_invite = True
    bill.checkout(A, "solo", "NOKKER1")
    with pytest.raises(VoucherError) as e:
        v.invite("NOKKER1", "a+autre@example.org")  # adresse qui a déjà son essai
    assert e.value.code == "code_used"
    with pytest.raises(VoucherError) as e:
        v.invite("NOPE1", "z@example.org")
    assert e.value.code == "code_invalid"


class OH(H):
    pass


def test_public_signup_route(env):
    from nokker.onboarding import http as ob_http
    from nokker.onboarding.front import MemoryFront
    from nokker.onboarding.service import Onboarding
    from nokker.tenancy.store import MemoryTenancy

    bill, v, _, _ = env
    front, tenancy = MemoryFront(), MemoryTenancy()
    tenancy.create_account("deja@example.org", "fr", "inv-1")
    ob_http.configure(Onboarding(front=front, tenancy=tenancy, approvals=None, acceptances=None))
    ob_http.configure_vouchers(v)
    try:
        h = OH("/v1/onboarding/voucher", {"code": "NOKKER1", "email": "p@example.org", "lang": "fr"})
        assert ob_http.handle(h, "POST", "/v1/onboarding/voucher") is False  # réglage éteint : route absente
        v.grants_invite = True
        assert ob_http.handle(h, "POST", "/v1/onboarding/voucher") and h.out == (200, {"sent": True})
        assert front.sent[-1]["to"] == "p@example.org" and front.sent[-1]["voucher"] == "NOKKER1"
        h = OH("/v1/onboarding/voucher", {"code": "NOKKER1", "email": "deja@example.org"})
        ob_http.handle(h, "POST", "/v1/onboarding/voucher")
        assert h.out == (200, {"sent": True}) and len(front.sent) == 1  # compte existant : rien, même réponse
        h = OH("/v1/onboarding/voucher", {"code": "NOPE1", "email": "q@example.org"})
        ob_http.handle(h, "POST", "/v1/onboarding/voucher")
        assert h.out == (400, {"error": "code_invalid"})
        h = OH("/v1/onboarding/voucher", {"code": "NOKKER1", "email": "pas-une-adresse"})
        ob_http.handle(h, "POST", "/v1/onboarding/voucher")
        assert h.out == (400, {"error": "bad_email"})
        front.fail_send = True
        h = OH("/v1/onboarding/voucher", {"code": "NOKKER1", "email": "r@example.org"})
        ob_http.handle(h, "POST", "/v1/onboarding/voucher")
        assert h.out == (503, {"error": "front_unavailable"}) and v.store.uses[-1]["state"] == "expired"
    finally:
        ob_http.configure(None)
        ob_http.configure_vouchers(None)


def test_failed_resend_keeps_the_reservation_of_a_sent_invite(env):
    """Invitation redemandée et refusée par front1 (débit, panne) : le premier lien est déjà parti, sa réservation
    reste ; seule une PREMIÈRE invitation ratée libère le code."""
    from nokker.onboarding import http as ob_http
    from nokker.onboarding.front import MemoryFront
    from nokker.onboarding.service import Onboarding
    from nokker.tenancy.store import MemoryTenancy

    _, v, _, _ = env
    v.grants_invite = True
    (code,) = v.create(campaign="flyers", kind="single", count=1, expires="2099-06-30", actor="ops@x")
    front = MemoryFront()
    ob_http.configure(Onboarding(front=front, tenancy=MemoryTenancy(), approvals=None, acceptances=None))
    ob_http.configure_vouchers(v)
    try:
        h = OH("/v1/onboarding/voucher", {"code": code, "email": "p@example.org", "lang": "fr"})
        ob_http.handle(h, "POST", "/v1/onboarding/voucher")
        assert h.out == (200, {"sent": True})
        uid, held = v.invite_hold(code, "p@example.org")
        assert held
        front.fail_send = True
        h = OH("/v1/onboarding/voucher", {"code": code, "email": "P+re@example.org", "lang": "fr"})
        ob_http.handle(h, "POST", "/v1/onboarding/voucher")
        assert h.out == (503, {"error": "front_unavailable"})
        assert v.store.uses[uid - 1]["state"] == "reserved"
        with pytest.raises(VoucherError) as e:
            v.invite(code, "autre@example.org")  # le code unique reste à p@
        assert e.value.code == "code_exhausted"
    finally:
        ob_http.configure(None)
        ob_http.configure_vouchers(None)
