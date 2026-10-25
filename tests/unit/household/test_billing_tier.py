"""Foyer branché sur le palier ACTIF du payeur (lot X23, docs/LOKKAL.md §7.29.6) : avec la facturation, le quota commun
et les sièges sont ceux du palier de foyer du payeur (`HOUSEHOLD_PLANS`, plafonds du palier dans la facturation) ;
payeur hors palier de foyer (Solo, résilié, statut hors `ACTIVE_STATUSES` ; `past_due` garde le palier) → foyer
« suspended » : chaque membre revient à ses propres plafonds, rien n'est effacé, les invitations sont bloquées, le payeur
lit un message clair. Facturation éteinte : `household_settings` (Famille 110/550 par défaut)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nokker.billing.plans import ACTIVE_STATUSES, HOUSEHOLD_PLANS, Plan, plans_from_config
from nokker.billing.service import Billing
from nokker.billing.store import MemoryBilling
from nokker.billing.stripe import StripeClient
from nokker.core.turn import TurnInput
from nokker.household.model import Settings
from nokker.household.quota import HouseholdQuota
from nokker.household.service import Household
from nokker.household.store import MemoryHouseholdStore
from nokker.team.model import TeamError
from nokker.team.service import Team
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.store import MemoryTenancy

T0 = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
# Plafonds de test, pas ceux du produit : petits, pour voir lequel s'applique.
PLANS = {"solo": {"turns_per_day": 40}, "famille": {"turns_per_day": 5, "turns_per_week": 20, "household_members": 3},
         "large": {"turns_per_day": 9, "turns_per_week": 30, "household_members": 4}}


def billing() -> Billing:
    clock = [1_900_000_000.0]
    b = Billing(store=MemoryBilling(), stripe=StripeClient("sk_test_x"),
                plans={**plans_from_config(None), **{k: Plan(k, v) for k, v in PLANS.items()}},
                prices={k: f"price_{k}" for k in PLANS}, webhook_secret="whsec_t", app_url="https://app.example",
                clock=lambda: clock[0])
    b.clock = clock
    return b


def subscribe(b: Billing, tenant_id: str, plan: str, status: str = "active", n: int = 1) -> None:
    """Ligne d'abonnement telle que le webhook l'écrit, puis le cache des plafonds vidé (comme le webhook)."""
    with tenant_scope(tenant_id):
        b.store.upsert_subscription({"stripe_subscription": f"sub_{tenant_id}", "stripe_customer": f"cus_{tenant_id}",
                                     "plan": plan, "status": status, "current_period_end": T0 + timedelta(days=30),
                                     "cancel_at_period_end": False, "last_event_at": T0 + timedelta(seconds=n)})
    b._caps.pop(tenant_id, None)


def world(*, bill: Billing | None, plan: str | None = "famille", n: int = 6):
    tenancy = MemoryTenancy()
    people = [tenancy.create_account(f"p{i}@example.com") for i in range(n)]
    hh = Household(team=Team(store=MemoryHouseholdStore(), tenancy=tenancy), settings=Settings())
    if bill is not None:
        hh.attach_billing(bill)
        if plan is not None:
            subscribe(bill, people[0].tenant_id, plan)
    return hh, people


def code(fn, *a, **kw) -> tuple[int, str]:
    with pytest.raises(TeamError) as e:
        fn(*a, **kw)
    return e.value.status, e.value.code


def invite(hh, pay, who):
    with tenant_scope(pay.tenant_id):
        return hh.invite(pay, {"email": who.email})


def join(hh, pay, who):
    iv = invite(hh, pay, who)
    with tenant_scope(who.tenant_id):
        return hh.accept(who, iv["id"])


def test_household_plans_are_the_family_tiers():
    assert {"famille", "large"} <= HOUSEHOLD_PLANS and "solo" not in HOUSEHOLD_PLANS
    assert "past_due" in ACTIVE_STATUSES and "canceled" not in ACTIVE_STATUSES


def test_billing_reports_the_active_plan_with_the_caps_cache():
    b = billing()
    tid = "tn_aaaaaaaaaaaa"
    assert b.active_plan(tid) is None
    subscribe(b, tid, "famille")
    assert b.active_plan(tid) == "famille" and b.caps_for(tid)["household_members"] == 3
    subscribe(b, tid, "famille", status="past_due", n=2)
    assert b.active_plan(tid) == "famille"  # Stripe relance le paiement : le palier reste
    subscribe(b, tid, "famille", status="canceled", n=3)
    assert b.active_plan(tid) is None and b.caps_for(tid) is None


def test_billing_without_a_household_tier_on_sale_keeps_the_settings():
    """Facturation allumée mais aucun price_id Famille/Large (bêta : création Stripe = décision) : aucun payeur ne
    pourrait avoir de foyer ; `household_settings` s'applique, comme facturation éteinte."""
    b = billing()
    b.prices = {"solo": "price_solo"}
    hh, (pay, *_) = world(bill=None)
    assert hh.attach_billing(b) is False and hh.payer_plan is None
    subscribe(b, pay.tenant_id, "solo")
    with tenant_scope(pay.tenant_id):
        v = hh.create(pay, {})
    assert v["state"] == "active" and v["quota"]["today"]["cap"] == 110


def test_without_billing_the_household_settings_apply():
    hh, (pay, *_) = world(bill=None)
    with tenant_scope(pay.tenant_id):
        v = hh.create(pay, {})
    assert v["state"] == "active" and v["plan"] == "famille"
    assert (v["quota"]["today"]["cap"], v["quota"]["week"]["cap"], v["max_members"]) == (110, 550, 6)


def test_quota_and_seats_follow_the_payer_tier():
    b = billing()
    hh, (pay, a, c, d, *_) = world(bill=b, plan="famille")
    with tenant_scope(pay.tenant_id):
        v = hh.create(pay, {})
    assert v["state"] == "active" and v["plan"] == "famille"
    assert (v["quota"]["today"]["cap"], v["quota"]["week"]["cap"], v["max_members"]) == (5, 20, 3)
    join(hh, pay, a)
    join(hh, pay, c)
    with tenant_scope(pay.tenant_id):
        assert code(hh.invite, pay, {"email": d.email}) == (409, "household_full")  # 3 sièges, payeur compris
    subscribe(b, pay.tenant_id, "large", n=2)  # changement de palier par le portail
    with tenant_scope(pay.tenant_id):
        v = hh.view(pay)
        assert (v["plan"], v["quota"]["today"]["cap"], v["quota"]["week"]["cap"], v["max_members"]) == ("large", 9, 30, 4)
        hh.invite(pay, {"email": d.email})


def test_creating_a_household_needs_a_household_tier():
    b = billing()
    hh, (pay, other, *_) = world(bill=b, plan="solo")
    with tenant_scope(pay.tenant_id):
        assert code(hh.create, pay, {}) == (402, "plan_required")
    subscribe(b, other.tenant_id, "large")
    with tenant_scope(other.tenant_id):
        assert hh.create(other, {})["plan"] == "large"


@pytest.mark.parametrize("plan,status", [("solo", "active"), ("famille", "canceled"), ("famille", "incomplete"),
                                         ("famille", "unpaid")])
def test_payer_out_of_a_household_tier_suspends_without_erasing(plan, status):
    b = billing()
    hh, (pay, a, c, *_) = world(bill=b, plan="famille")
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
    join(hh, pay, a)
    pending = invite(hh, pay, c)
    subscribe(b, pay.tenant_id, plan, status=status, n=2)
    with tenant_scope(pay.tenant_id):
        v = hh.view(pay)
        assert v["state"] == "suspended"
        assert "suspendu" in v["notice"] and "suspended" in v["notice"]  # message clair au payeur, FR et EN
        assert [m["account_id"] for m in v["members"]] == [pay.account_id, a.account_id]  # rien n'est effacé
        assert v["quota"]["today"]["cap"] is None and v["quota"]["week"]["cap"] is None
        assert code(hh.invite, pay, {"email": "nouveau@example.com"}) == (402, "household_suspended")
    with tenant_scope(c.tenant_id):  # une invitation déjà envoyée ne s'accepte pas non plus
        assert code(hh.accept, c, pending["id"]) == (402, "household_suspended")
    with tenant_scope(a.tenant_id):
        mine = hh.view(a)
        assert mine["state"] == "suspended" and "notice" not in mine  # le message vise le payeur
    subscribe(b, pay.tenant_id, "famille", n=3)  # réabonné : le foyer revient tel quel
    with tenant_scope(pay.tenant_id):
        v = hh.view(pay)
        assert v["state"] == "active" and len(v["members"]) == 2 and "notice" not in v


def test_past_due_keeps_the_household_active():
    b = billing()
    hh, (pay, a, *_) = world(bill=b, plan="famille")
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
    subscribe(b, pay.tenant_id, "famille", status="past_due", n=2)
    join(hh, pay, a)
    with tenant_scope(pay.tenant_id):
        assert hh.view(pay)["state"] == "active"


def test_unreachable_billing_falls_back_on_the_settings():
    b = billing()
    hh, (pay, *_) = world(bill=b, plan="famille")
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})

    def down(tenant_id):
        raise RuntimeError("base injoignable")

    b.active_plan = down  # paiement injoignable : jamais de blocage, comme les plafonds de compte
    with tenant_scope(pay.tenant_id):
        v = hh.view(pay)
    assert v["state"] == "active" and v["quota"]["today"]["cap"] == 110


# ── sur le vrai noyau : les tours ──


def kernel_world(tmp_path, b: Billing):
    from nokker.tenancy.limits import MemoryUsage, TenantLimits
    from tests.unit.agents.conftest import make_world

    w = make_world(tmp_path, [("text", f"réponse {i}") for i in range(30)])
    tenancy = MemoryTenancy()
    pay, mem = (tenancy.create_account(f"{n}@example.com") for n in ("payeur", "membre"))
    hh = Household(team=Team(store=MemoryHouseholdStore(), tenancy=tenancy, agents=w.agents), settings=Settings())
    hh.attach_billing(b)
    subscribe(b, pay.tenant_id, "famille")
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {"name": "Maison"})
    join(hh, pay, mem)
    hh.quota = HouseholdQuota(household=hh, ledger=w.kernel.ledger)
    w.kernel.household = hh.quota
    w.kernel.limits = TenantLimits(counter=MemoryUsage(), mode="hard", turns_per_day=2, turns_per_week=100,
                                   hard_turns_per_day=50)
    w.__dict__.update(hh=hh, pay=pay, mem=mem, bill=b)
    return w


def turn(w, who):
    with tenant_scope(who.tenant_id):
        return w.kernel.run_turn(TurnInput(thread_id=f"th_{who.account_id}", user_text="salut", channel="web",
                                           sender=who.account_id))


def test_turns_use_the_tier_quota_then_own_caps_once_suspended(tmp_path):
    w = kernel_world(tmp_path, billing())
    pid = w.hh.membership(w.mem.account_id)[1].id
    for _ in range(3):  # quota du foyer (5/jour) : le plafond Solo du compte (2/jour) ne coupe pas un membre
        assert turn(w, w.mem).reply.startswith("réponse")
    assert {r.household_id for r in w.kernel.ledger.fetch_calls()} == {pid}
    subscribe(w.bill, w.pay.tenant_id, "solo", n=2)
    t = turn(w, w.mem)  # suspendu : le membre revient à SES plafonds (3 tours déjà comptés au compte, plafond 2)
    assert [v.rule for v in t.verdicts] == ["limits:turns_per_day"]
    other = w.kernel.limits
    w.kernel.limits = None
    t = turn(w, w.mem)
    assert t.reply.startswith("réponse")
    w.kernel.limits = other
    assert [r.household_id for r in w.kernel.ledger.fetch_calls()][-1] is None  # plus attribué au foyer


def test_server_wires_the_billing_into_the_household(monkeypatch):
    """`configure_household` après `configure_billing` : facturation allumée → le foyer lit le palier du payeur ;
    éteinte → aucun contrôle de palier (household_settings)."""
    from types import SimpleNamespace

    from nokker.api import server
    from nokker.billing import http as billing_http
    from nokker.config import NokkerConfig
    from nokker.household import http as household_http
    from nokker.onboarding import http as onboarding_http
    from nokker.team import http as team_http

    monkeypatch.delenv("NOKKER_PG_DSN", raising=False)
    config = NokkerConfig(team=True, household=True)
    tenancy = MemoryTenancy()
    kernel = SimpleNamespace(ledger=None, memory=object(), gates=None, tools=object(), context=object(),
                             household=None)
    try:
        for bill in (None, billing()):
            billing_http.configure(bill)
            server.configure_team(config, tenancy)
            server.configure_household(config, kernel, tenancy, None)
            house = household_http.service()
            assert house is not None
            assert (house.payer_plan is None) is (bill is None)
            if bill is not None:
                subscribe(bill, "tn_aaaaaaaaaaaa", "large")
                assert house.tier("tn_aaaaaaaaaaaa").plan == "large"
                assert not house.tier("tn_bbbbbbbbbbbb").active
    finally:
        household_http.configure(None)
        team_http.configure(None)
        billing_http.configure(None)
        onboarding_http.configure_minors(None)


def test_one_source_for_the_household_tiers():
    """Les paliers du foyer n'existent qu'à un endroit (nokker/billing/plans.py) : les réglages sans facturation, la
    facturation et `billing_plans` lisent la même table."""
    from nokker.billing.plans import DEFAULT_PLANS

    fam, large = DEFAULT_PLANS["famille"].caps, DEFAULT_PLANS["large"].caps
    s = Settings.from_config(None)
    assert (s.turns_per_day, s.turns_per_week, s.max_members) == \
        (fam["turns_per_day"], fam["turns_per_week"], fam["household_members"])
    s = Settings.from_config({"plan": "large"})
    assert (s.turns_per_day, s.turns_per_week) == (large["turns_per_day"], large["turns_per_week"])
    s = Settings.from_config(None, billing_plans={"famille": {"household_members": 4}})
    assert s.max_members == 4 and s.turns_per_day == fam["turns_per_day"]
    assert all(p in DEFAULT_PLANS for p in HOUSEHOLD_PLANS)
    tier = Settings().for_plan("large", plans_from_config(None)["large"].caps)
    assert (tier.turns_per_day, tier.turns_per_week, tier.max_members) == (230, 1150, 6)
