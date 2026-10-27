"""Profils mineurs ouverts foyer par foyer (`household_minors_mode: allowlist`, docs/LOKKAL.md §7.29.4).

Décision produit du 25.10 : pendant la bêta, le mode parent/mineur se teste chez les seuls foyers que l'opérateur a
autorisés un par un. Pour tout autre payeur, c'est comme si le drapeau était éteint : 403 `minors_not_enabled`
(message FR et EN) pour déclarer un profil `teen`/`child` ou inviter avec une attestation, profil déjà déclaré
suspendu, `minors.enabled` faux dans sa vue. `all` (défaut) : le comportement d'avant, drapeau allumé pour tous."""
from __future__ import annotations

import pytest

from nokker.household.model import Settings, consent_text
from nokker.household.service import Household
from nokker.household.store import MemoryHouseholdStore
from nokker.team.model import TeamError
from nokker.team.service import Team
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.store import MemoryTenancy


def world(*, minors: bool = True, mode: str = "allowlist", n: int = 4):
    tenancy = MemoryTenancy()
    people = [tenancy.create_account(f"p{i}@example.com") for i in range(n)]
    team = Team(store=MemoryHouseholdStore(), tenancy=tenancy)
    hh = Household(team=team, settings=Settings(minors=minors, minors_mode=mode))
    return hh, people


def err(fn, *a, **kw) -> TeamError:
    with pytest.raises(TeamError) as e:
        fn(*a, **kw)
    return e.value


def attest() -> dict:
    return {"version": consent_text("fr").sha256, "lang": "fr", "legal_guardian": True}


def grant(hh: Household, acc) -> None:
    hh.store.grant_rows[acc.account_id] = {"account_id": acc.account_id, "tenant_id": acc.tenant_id,
                                           "code": "mg-000001", "granted_at": 1.0, "revoked_at": None}


def household_with_member(hh, pay, mem):
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
        iv = hh.invite(pay, {"email": mem.email})
    with tenant_scope(mem.tenant_id):
        hh.accept(mem, iv["id"])


def test_settings_minors_mode_defaults_to_all_and_is_checked():
    assert Settings.from_config(None, minors=True).minors_mode == "all"
    assert Settings.from_config(None, minors=True, minors_mode="allowlist").minors_mode == "allowlist"
    with pytest.raises(ValueError):
        Settings.from_config(None, minors=True, minors_mode="some")


def test_config_carries_household_minors_mode():
    from nokker.config import NokkerConfig

    assert NokkerConfig().household_minors_mode == "all"
    assert NokkerConfig(household_minors_mode="allowlist").household_minors_mode == "allowlist"


def test_unlisted_payer_cannot_invite_a_minor_403_in_french_and_english():
    hh, (pay, kid, adult, _) = world()
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
        e = err(hh.invite, pay, {"email": kid.email, "profile": "teen", "attestation": attest()})
        assert (e.status, e.code) == (403, "minors_not_enabled")
        assert "foyer" in e.message and "household" in e.message  # FR puis EN
        assert hh.store.consent_rows == []  # aucune attestation rangée
        assert hh.invite(pay, {"email": adult.email})["profile"] == "adult"  # un adulte : rien ne change
        assert hh.view(pay)["minors"] == {"enabled": False}


def test_unlisted_payer_cannot_declare_an_existing_member_minor():
    hh, (pay, mem, *_) = world()
    household_with_member(hh, pay, mem)
    with tenant_scope(pay.tenant_id):
        e = err(hh.set_profile, pay, mem.account_id, {"profile": "child", "attestation": attest()})
        assert (e.status, e.code) == (403, "minors_not_enabled")
        assert hh.set_profile(pay, mem.account_id, {"profile": "adult"})["profile"] == "adult"
    assert hh.store.minor(mem.account_id) is None


def test_listed_payer_opens_minor_profiles_for_their_household_only():
    hh, (pay, kid, other, kid2) = world()
    grant(hh, pay)
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
        assert hh.view(pay)["minors"] == {"enabled": True}
        iv = hh.invite(pay, {"email": kid.email, "profile": "teen", "attestation": attest()})
    with tenant_scope(kid.tenant_id):
        hh.accept(kid, iv["id"])
    st = hh.minor_state(kid.account_id)
    assert (st.profile, st.active) == ("teen", True)
    with tenant_scope(other.tenant_id):  # un autre payeur, non autorisé
        hh.create(other, {})
        assert err(hh.invite, other, {"email": kid2.email, "profile": "child",
                                      "attestation": attest()}).code == "minors_not_enabled"


def test_revoked_grant_suspends_the_minor_and_refuses_renewal_and_acceptance():
    hh, (pay, kid, kid2, _) = world()
    grant(hh, pay)
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
        iv = hh.invite(pay, {"email": kid.email, "profile": "teen", "attestation": attest()})
        iv2 = hh.invite(pay, {"email": kid2.email, "profile": "child", "attestation": attest()})
    with tenant_scope(kid.tenant_id):
        hh.accept(kid, iv["id"])
    hh.store.grant_rows[pay.account_id]["revoked_at"] = 2.0
    assert hh.minor_state(kid.account_id).active is False  # comme drapeau éteint : profil gardé, aucun tour
    with tenant_scope(pay.tenant_id):
        assert err(hh.renew_consent, pay, kid.account_id, {"attestation": attest()}).code == "minors_not_enabled"
        assert hh.view(pay)["minors"] == {"enabled": False}
    with tenant_scope(kid2.tenant_id):
        e = err(hh.accept, kid2, iv2["id"])
        assert (e.status, e.code) == (403, "minors_not_enabled")


def test_mode_all_keeps_the_previous_behaviour():
    hh, (pay, kid, *_) = world(mode="all")
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
        assert hh.invite(pay, {"email": kid.email, "profile": "teen", "attestation": attest()})["profile"] == "teen"
        assert hh.view(pay)["minors"] == {"enabled": True}


def test_flag_off_wins_over_a_grant():
    hh, (pay, kid, *_) = world(minors=False)
    grant(hh, pay)
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
        e = err(hh.invite, pay, {"email": kid.email, "profile": "teen", "attestation": attest()})
        assert (e.status, e.code) == (400, "minors_disabled")


def test_unreadable_allowlist_closes_minor_profiles():
    hh, (pay, kid, *_) = world()

    def broken(_account_id):
        raise RuntimeError("base injoignable")

    hh.store.minor_grant = broken
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
        assert err(hh.invite, pay, {"email": kid.email, "profile": "teen",
                                    "attestation": attest()}).code == "minors_not_enabled"
