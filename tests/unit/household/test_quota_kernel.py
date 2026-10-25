"""Quota commun du foyer sur le VRAI noyau (monde de tests/unit/agents, faux modèle) : chaque tour d'un membre porte
l'étiquette du foyer au ledger (le même mécanisme que le budget de projet, aucun second compteur) ; le compteur du
foyer est le nombre de TOURS lus dans le ledger ; au-delà d'un plafond (membre, jour, semaine), refus clair AVANT
l'appel modèle, avec l'heure de remise à zéro."""
from __future__ import annotations

import pytest

from nokker.core.turn import TurnInput
from nokker.household.model import Settings, consent_text
from nokker.household.quota import HouseholdQuota
from nokker.household.service import Household
from nokker.household.store import MemoryHouseholdStore
from nokker.team.service import Team
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.tenancy.store import MemoryTenancy
from tests.unit.agents.conftest import make_world


@pytest.fixture(autouse=True)
def _scope():
    with tenant_scope(INTERNAL_TENANT):
        yield


def build(tmp_path, *, settings: Settings | None = None, minors: bool = False):
    w = make_world(tmp_path, [("text", f"réponse {i}") for i in range(30)])
    tenancy = MemoryTenancy()
    pay, mem, out = (tenancy.create_account(f"{n}@example.com") for n in ("payeur", "membre", "voisin"))
    team = Team(store=MemoryHouseholdStore(), tenancy=tenancy, agents=w.agents)
    hh = Household(team=team, settings=settings or Settings(minors=minors))
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {"name": "Maison"})
        iv = hh.invite(pay, {"email": mem.email})
    with tenant_scope(mem.tenant_id):
        hh.accept(mem, iv["id"])
    quota = HouseholdQuota(household=hh, ledger=w.kernel.ledger)
    hh.quota = quota
    w.kernel.household = quota
    w.__dict__.update(hh=hh, quota=quota, pay=pay, mem=mem, out=out, tenancy=tenancy)
    return w


def turn(w, who, thread=None, text="salut"):
    return w.kernel.run_turn(TurnInput(thread_id=thread or f"th_{who.account_id}", user_text=text, channel="web",
                                       sender=who.account_id))


def test_every_turn_of_a_member_is_tagged_with_the_household_in_the_ledger(tmp_path):
    w = build(tmp_path)
    turn(w, w.mem)
    turn(w, w.pay)
    pid = w.hh.view(w.pay)["id"]
    rows = w.kernel.ledger.fetch_calls()
    assert {(r.household_id, r.household_tenant, r.account) for r in rows} == \
        {(pid, w.pay.tenant_id, w.mem.account_id), (pid, w.pay.tenant_id, w.pay.account_id)}
    assert {r.project_id for r in rows} == {None}  # pas un projet d'équipe : le budget n'y voit rien
    v = w.hh.view(w.pay)
    assert v["counted"] is True and v["quota"]["today"]["used"] == 2
    assert {m["account_id"]: m["turns"] for m in v["members"]} == {w.pay.account_id: {"today": 1, "week": 1},
                                                                   w.mem.account_id: {"today": 1, "week": 1}}


def test_outsider_is_neither_tagged_nor_limited(tmp_path):
    w = build(tmp_path, settings=Settings(turns_per_day=0))
    t = turn(w, w.out)
    assert t.reply.startswith("réponse 0")
    assert [r.household_id for r in w.kernel.ledger.fetch_calls()] == [None]


def test_member_cap_refuses_before_the_model_with_the_reset_time(tmp_path):
    w = build(tmp_path)
    w.hh.set_cap(w.pay, w.mem.account_id, {"turns_per_day": 2})
    turn(w, w.mem)
    turn(w, w.mem)
    calls = w.model.calls
    t = turn(w, w.mem)
    assert w.model.calls == calls  # aucun appel modèle
    assert [v.rule for v in t.verdicts] == ["household:member_turns_per_day"]
    assert "2 messages par jour" in t.reply and "Daily limit" in t.reply
    assert "resets_at=" in t.verdicts[0].reason
    assert w.kernel.store.history(f"th_{w.mem.account_id}")[-1].reply == t.reply  # gardé dans le fil
    assert turn(w, w.pay).reply.startswith("réponse")  # le payeur n'a pas ce plafond


def test_shared_daily_quota_counts_every_member(tmp_path):
    w = build(tmp_path, settings=Settings(turns_per_day=3, member_turns_per_day=None))
    turn(w, w.pay)
    turn(w, w.pay)
    turn(w, w.mem)
    calls = w.model.calls
    for who in (w.mem, w.pay):
        t = turn(w, who)
        assert [v.rule for v in t.verdicts] == ["household:turns_per_day"]
        assert "foyer" in t.reply
    assert w.model.calls == calls
    assert w.hh.view(w.pay)["quota"]["today"]["used"] == 3  # les tours refusés ne comptent pas


def test_weekly_quota(tmp_path):
    w = build(tmp_path, settings=Settings(turns_per_day=None, turns_per_week=2, member_turns_per_day=None))
    turn(w, w.mem)
    turn(w, w.pay)
    t = turn(w, w.mem)
    assert [v.rule for v in t.verdicts] == ["household:turns_per_week"]
    assert "lundi" in t.reply


def test_sub_agent_threads_belong_to_the_parent_turn(tmp_path):
    from nokker.ledger.attribution import SpendTag, spend_tag
    from tests.unit.team.test_budget import call

    w = build(tmp_path, settings=Settings(turns_per_day=2, member_turns_per_day=None))
    pid = w.hh.view(w.pay)["id"]
    turn(w, w.mem, thread="th_parent")
    with spend_tag(SpendTag(None, None, w.mem.account_id, household_id=pid, household_tenant=w.pay.tenant_id)):
        w.kernel.ledger.record("th_parent/sa-1", 0, call(1))  # un sous-agent du tour : pas un tour de plus
        w.kernel.ledger.record("th_parent", 0, call(1))  # un second appel du même tour
    assert w.hh.view(w.pay)["quota"]["today"]["used"] == 1


def test_soft_mode_counts_and_warns_once_but_never_cuts(tmp_path):
    w = build(tmp_path, settings=Settings(turns_per_day=1, member_turns_per_day=None, mode="soft"))
    first = turn(w, w.mem)
    assert "foyer" not in first.reply
    second = turn(w, w.pay)
    assert second.reply.startswith("réponse 1") and "dépasser" in second.reply
    third = turn(w, w.mem)
    assert third.reply.startswith("réponse 2") and "dépasser" not in third.reply


def test_unreadable_ledger_refuses_by_prudence(tmp_path):
    w = build(tmp_path)

    def broken(*a, **kw):
        raise RuntimeError("base injoignable")

    w.kernel.ledger.fetch_household_calls = broken
    calls = w.model.calls
    t = turn(w, w.mem)
    assert [v.rule for v in t.verdicts] == ["household:unavailable"] and w.model.calls == calls


def test_suspended_minor_profile_has_no_turn(tmp_path):
    w = build(tmp_path, minors=True)
    kid = w.tenancy.create_account("enfant@example.com")
    att = {"version": consent_text("fr").sha256, "lang": "fr", "legal_guardian": True}
    with tenant_scope(w.pay.tenant_id):
        iv = w.hh.invite(w.pay, {"email": kid.email, "profile": "child", "attestation": att})
    with tenant_scope(kid.tenant_id):
        w.hh.accept(kid, iv["id"])
    assert turn(w, kid).reply.startswith("réponse")
    w.hh.revoke_consent(w.pay, kid.account_id)
    calls = w.model.calls
    t = turn(w, kid)
    assert [v.rule for v in t.verdicts] == ["household:suspended"] and w.model.calls == calls
    assert "suspendu" in t.reply


def test_agent_passage_of_a_member_counts_and_is_skipped_when_over(tmp_path):
    w = build(tmp_path, settings=Settings(turns_per_day=1, member_turns_per_day=None))
    a = w.agents.create_agent(name="veille", role="r", model="", created_by=w.mem.account_id)
    run = w.run(w.agents.run_now(a.id, w.mem.account_id).id)
    assert run.status == "succeeded"
    assert {r.household_id for r in w.kernel.ledger.fetch_calls(thread_id=run.thread_id)} == {w.hh.view(w.pay)["id"]}
    calls = w.model.calls
    run2 = w.run(w.agents.run_now(a.id, w.mem.account_id).id)
    assert run2.status == "skipped" and run2.thread_id is None and w.model.calls == calls
    assert "foyer" in run2.summary


def test_tenant_limits_of_a_member_keep_only_the_safety_cap():
    from nokker.tenancy.limits import MemoryUsage, TenantLimits

    lim = TenantLimits(counter=MemoryUsage(), mode="hard", turns_per_day=1, turns_per_week=1, hard_turns_per_day=3)
    with tenant_scope("tn_aaaaaaaaaaaa"):
        assert lim.check_turn("us_a", shared=True) is None
        assert lim.check_turn("us_a", shared=True) is None  # le plafond Solo ne s'applique pas dans un foyer
        assert lim.check_turn("us_a", shared=True) is None
        v = lim.check_turn("us_a", shared=True)
        assert v is not None and v.rule == "limits:hard_turns_per_day"
        assert lim.notice("us_a", shared=True) is None
