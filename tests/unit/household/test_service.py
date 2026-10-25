"""Foyer (lot X23) : service sur le magasin mémoire. Un foyer est un projet d'équipe `household` ; le payeur est
l'owner ; 6 membres au plus, payeur compris ; un foyer par compte ; profils mineurs derrière `household_minors`."""
from __future__ import annotations

import pytest

from nokker.household.model import Settings, consent_text
from nokker.household.service import Household
from nokker.household.store import MemoryHouseholdStore
from nokker.onboarding.consents import MemoryConsents
from nokker.team.model import TeamError
from nokker.team.service import Team
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.store import MemoryTenancy


def world(*, minors: bool = False, max_members: int = 6, n: int = 8):
    tenancy = MemoryTenancy()
    people = [tenancy.create_account(f"p{i}@example.com") for i in range(n)]
    team = Team(store=MemoryHouseholdStore(), tenancy=tenancy)
    changed: list[str] = []
    consents = MemoryConsents()
    hh = Household(team=team, settings=Settings(minors=minors, max_members=max_members), consents=consents,
                   on_change=changed.append)
    return hh, people, changed, consents


def code(fn, *a, **kw) -> tuple[int, str]:
    with pytest.raises(TeamError) as e:
        fn(*a, **kw)
    return e.value.status, e.value.code


def join(hh, payer, who, **body):
    with tenant_scope(payer.tenant_id):
        iv = hh.invite(payer, {"email": who.email, **body})
    with tenant_scope(who.tenant_id):
        return hh.accept(who, iv["id"])


def attest(lang: str = "fr") -> dict:
    return {"version": consent_text(lang).sha256, "lang": lang, "legal_guardian": True}


def test_settings_defaults_and_family_plan_from_billing():
    s = Settings.from_config(None)
    assert (s.plan, s.turns_per_day, s.turns_per_week, s.member_turns_per_day, s.max_members, s.mode) == \
        ("famille", 110, 550, 40, 6, "hard")
    s = Settings.from_config({"member_turns_per_day": 25}, billing_plans={"famille": {"turns_per_day": 120}})
    assert (s.turns_per_day, s.turns_per_week, s.member_turns_per_day) == (120, 550, 25)
    with pytest.raises(ValueError):
        Settings.from_config({"max_members": 7})  # la base refuse au-delà de 6 : la configuration ne peut pas dépasser


def test_payer_creates_one_household_and_it_is_a_household_team_project():
    hh, (pay, *_), _, _ = world()
    with tenant_scope(pay.tenant_id):
        v = hh.create(pay, {"name": "Maison"})
        assert v["you"] == {"account_id": pay.account_id, "payer": True, "profile": "adult"}
        assert [m["account_id"] for m in v["members"]] == [pay.account_id]
        p = hh.team.store.project(pay, v["id"])
        assert p.kind == "household" and p.owner_account == pay.account_id
        assert code(hh.create, pay, {"name": "Deux"}) == (409, "household_exists")
        # invisible pour les routes d'équipe : même réponse qu'un projet inconnu
        assert hh.team.list_projects(pay)["projects"] == []
        assert code(hh.team.get, pay, v["id"]) == (404, "not_found")


def test_members_join_by_invitation_up_to_six_payer_included():
    hh, (pay, *others), changed, _ = world()
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
    for who in others[:5]:
        join(hh, pay, who)
    with tenant_scope(pay.tenant_id):
        v = hh.view(pay)
        assert len(v["members"]) == 6
        assert code(hh.invite, pay, {"email": others[5].email}) == (409, "household_full")
    assert others[0].tenant_id in changed  # registre et préfixe du membre relus


def test_pending_invitations_count_toward_the_limit():
    hh, (pay, *others), _, _ = world(max_members=3)
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
        hh.invite(pay, {"email": others[0].email})
        hh.invite(pay, {"email": others[1].email})
        assert code(hh.invite, pay, {"email": others[2].email}) == (409, "household_full")
        assert code(hh.invite, pay, {"email": others[0].email}) == (409, "already_invited")


def test_one_household_per_account_refused_at_acceptance():
    hh, (a, b, c, *_), _, _ = world()
    for payer in (a, b):
        with tenant_scope(payer.tenant_id):
            hh.create(payer, {})
    join(hh, a, c)
    with tenant_scope(b.tenant_id):
        iv = hh.invite(b, {"email": c.email})  # même réponse que l'adresse ait un foyer ou non (pas d'énumération)
    with tenant_scope(c.tenant_id):
        assert code(hh.accept, c, iv["id"]) == (409, "already_in_household")
        assert hh.view(c)["id"] == hh.view(c)["id"]  # toujours dans le premier


def test_household_invitation_is_not_accepted_through_team_routes():
    hh, (pay, mem, *_), _, _ = world()
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
        iv = hh.invite(pay, {"email": mem.email})
    with tenant_scope(mem.tenant_id):
        assert hh.team.my_invites(mem)["invitations"] == []
        assert code(hh.team.accept, mem, iv["id"]) == (404, "not_found")
        assert [i["id"] for i in hh.received(mem)["invitations"]] == [iv["id"]]


def test_view_shows_counts_never_content_and_members_see_only_their_line():
    hh, (pay, mem, kid, *_), _, _ = world()
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
    join(hh, pay, mem)
    join(hh, pay, kid)
    with tenant_scope(pay.tenant_id):
        v = hh.view(pay)
    assert {m["account_id"] for m in v["members"]} == {pay.account_id, mem.account_id, kid.account_id}
    line = next(m for m in v["members"] if m["account_id"] == mem.account_id)
    assert set(line) == {"account_id", "email", "payer", "profile", "status", "joined_at", "turns", "cap", "you"}
    assert line["cap"] == {"turns_per_day": 40, "source": "default"}
    assert next(m for m in v["members"] if m["payer"])["cap"] == {"turns_per_day": None, "source": "none"}
    assert "invitations" in v and "policy" in v
    with tenant_scope(mem.tenant_id):
        mine = hh.view(mem)
    assert [m["account_id"] for m in mine["members"]] == [mem.account_id]
    assert "invitations" not in mine and "policy" not in mine
    assert mine["quota"]["today"]["cap"] == 110 and mine["quota"]["week"]["cap"] == 550


def test_only_the_payer_sets_member_caps():
    hh, (pay, mem, *_), _, _ = world()
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
    join(hh, pay, mem)
    with tenant_scope(mem.tenant_id):
        assert code(hh.set_cap, mem, mem.account_id, {"turns_per_day": 99}) == (403, "forbidden")
    with tenant_scope(pay.tenant_id):
        assert code(hh.set_cap, pay, mem.account_id, {"turns_per_day": -1}) == (400, "bad_request")
        assert hh.set_cap(pay, mem.account_id, {"turns_per_day": 12})["cap"] == {"turns_per_day": 12,
                                                                                 "source": "custom"}
        assert hh.set_cap(pay, mem.account_id, {"turns_per_day": None})["cap"] == {"turns_per_day": None,
                                                                                   "source": "custom"}
        assert hh.reset_cap(pay, mem.account_id)["cap"] == {"turns_per_day": 40, "source": "default"}
        assert code(hh.set_cap, pay, "us_inconnu0001", {"turns_per_day": 3}) == (404, "not_found")


def test_members_leave_payer_removes_payer_deletes():
    hh, (pay, mem, other, *_), _, _ = world()
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
    join(hh, pay, mem)
    join(hh, pay, other)
    with tenant_scope(mem.tenant_id):
        assert hh.remove(mem, mem.account_id) == {"removed": True}
        assert code(hh.view, mem) == (404, "not_found")
    with tenant_scope(pay.tenant_id):
        assert code(hh.remove, pay, pay.account_id) == (409, "payer_fixed")
        hh.remove(pay, other.account_id)
        assert hh.delete(pay) == {"deleted": True}
        assert code(hh.view, pay) == (404, "not_found")
        hh.create(pay, {})  # un nouveau foyer est possible après suppression


# ─────────────────────────────── profils mineurs ───────────────────────────────


def test_minor_profiles_are_closed_without_the_flag():
    hh, (pay, kid, *_), _, _ = world(minors=False)
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
        assert code(hh.invite, pay, {"email": kid.email, "profile": "teen", "attestation": attest()}) == \
            (400, "minors_disabled")


def test_minor_invitation_requires_a_current_attestation_of_the_legal_guardian():
    hh, (pay, kid, *_), _, _ = world(minors=True)
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
        assert code(hh.invite, pay, {"email": kid.email, "profile": "child"}) == (400, "attestation_required")
        bad = {**attest(), "legal_guardian": False}
        assert code(hh.invite, pay, {"email": kid.email, "profile": "child", "attestation": bad}) == \
            (400, "attestation_required")
        stale = {**attest(), "version": "0" * 64}
        assert code(hh.invite, pay, {"email": kid.email, "profile": "child", "attestation": stale}) == \
            (409, "attestation_stale")
        hh.invite(pay, {"email": kid.email, "profile": "child", "attestation": attest("en")})
        (c,) = hh.team.store.consents(pay, hh.view(pay)["id"])
    assert (c.profile, c.lang, c.version_sha, c.guardian_account, c.minor_email) == \
        ("child", "en", consent_text("en").sha256, pay.account_id, kid.email)
    assert c.granted_at > 0 and c.active


def test_accepted_minor_is_flagged_and_never_keeps_a_training_consent():
    hh, (pay, kid, *_), _, consents = world(minors=True)
    consents.record(kid.account_id, "training", True, "signup", None, None)
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
    join(hh, pay, kid, profile="teen", attestation=attest())
    assert hh.team.store.minor(kid.account_id)["profile"] == "teen"
    assert consents.current(kid.account_id, "training") is False  # retiré à l'entrée dans le profil mineur
    st = hh.minor_state(kid.account_id)
    assert (st.profile, st.active, st.guardian) == ("teen", True, pay.account_id)
    assert hh.minor_state(pay.account_id) is None


def test_revoking_the_consent_suspends_the_profile_and_renewal_reactivates_it():
    hh, (pay, kid, *_), changed, _ = world(minors=True)
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
    join(hh, pay, kid, profile="child", attestation=attest())
    with tenant_scope(kid.tenant_id):
        assert code(hh.revoke_consent, kid, kid.account_id) == (403, "forbidden")
        assert code(hh.remove, kid, kid.account_id) == (403, "minor_cannot_leave")
    with tenant_scope(pay.tenant_id):
        line = hh.revoke_consent(pay, kid.account_id)
        assert line["status"] == "suspended"
        assert hh.minor_state(kid.account_id).active is False
        assert kid.tenant_id in changed
        assert hh.renew_consent(pay, kid.account_id, {"attestation": attest()})["status"] == "active"
        cs = hh.team.store.consents(pay, hh.view(pay)["id"])
    assert [c.active for c in cs] == [False, True]


def test_payer_declares_an_existing_member_minor_then_adult_again():
    hh, (pay, mem, *_), _, consents = world(minors=True)
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
    join(hh, pay, mem)
    consents.record(mem.account_id, "training", True, "account", None, None)
    with tenant_scope(pay.tenant_id):
        assert code(hh.set_profile, pay, mem.account_id, {"profile": "teen"}) == (400, "attestation_required")
        assert hh.set_profile(pay, mem.account_id, {"profile": "teen", "attestation": attest()})["profile"] == "teen"
        assert consents.current(mem.account_id, "training") is False
        assert code(hh.set_profile, pay, pay.account_id, {"profile": "teen", "attestation": attest()}) == \
            (409, "payer_fixed")
        assert hh.set_profile(pay, mem.account_id, {"profile": "adult"})["profile"] == "adult"
    assert hh.minor_state(mem.account_id) is None


def test_minor_state_is_suspended_when_the_flag_goes_off():
    hh, (pay, kid, *_), _, _ = world(minors=True)
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
    join(hh, pay, kid, profile="teen", attestation=attest())
    hh.settings = Settings(minors=False)
    assert hh.minor_state(kid.account_id).active is False  # profil gardé, rien d'ouvert sans le drapeau


def test_policy_is_the_payer_whitelist():
    hh, (pay, mem, *_), _, _ = world(minors=True)
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
        assert hh.policy(pay) == {"minor_connectors": [], "minor_agents": []}
        hh.put_policy(pay, {"minor_connectors": ["calendar"], "minor_agents": ["devoirs"]})
    join(hh, pay, mem, profile="teen", attestation=attest())
    assert hh.minor_state(mem.account_id).policy.minor_connectors == ["calendar"]
    with tenant_scope(mem.tenant_id):
        assert code(hh.put_policy, mem, {"minor_connectors": ["mail"]}) == (403, "forbidden")
