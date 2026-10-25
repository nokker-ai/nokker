"""Mémoire commune du foyer : les notes du foyer (notes de projet de l'équipe, mêmes règles : caviardage, la dernière
reçue gagne, quarantaine) sont rappelées dans les tours de CHAQUE membre ; la mémoire de profil (portée `user`) ne
passe jamais par le foyer ; un non-membre, un ancien membre ou un profil suspendu ne rappellent rien."""
from __future__ import annotations

import pytest

from nokker.household.memory import HouseholdRecall
from nokker.household.model import Settings, consent_text
from nokker.household.service import Household
from nokker.household.store import MemoryHouseholdStore
from nokker.team.model import TeamError
from nokker.team.service import Team
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.tenancy.resources import NullMemory, TenantMemory
from nokker.tenancy.store import MemoryTenancy


@pytest.fixture
def w():
    tenancy = MemoryTenancy()
    pay, mem, kid, out = (tenancy.create_account(f"{n}@example.com") for n in ("payeur", "membre", "enfant", "voisin"))
    team = Team(store=MemoryHouseholdStore(), tenancy=tenancy, scrub=lambda s: s.replace("sk-SECRET", "[caviardé]"))
    hh = Household(team=team, settings=Settings(minors=True))
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
        iv = hh.invite(pay, {"email": mem.email})
        ivk = hh.invite(pay, {"email": kid.email, "profile": "teen",
                              "attestation": {"version": consent_text("fr").sha256, "lang": "fr",
                                              "legal_guardian": True}})
    with tenant_scope(mem.tenant_id):
        hh.accept(mem, iv["id"])
    with tenant_scope(kid.tenant_id):
        hh.accept(kid, ivk["id"])
    recall = HouseholdRecall(household=hh)
    memory = TenantMemory(internal=NullMemory(), tenant_memory=recall.for_tenant)
    return {"hh": hh, "pay": pay, "mem": mem, "kid": kid, "out": out, "memory": memory}


def note(hh, who, name, text, **kw):
    with tenant_scope(who.tenant_id):
        return hh.put_note(who, name, {"kind": "fact", "title": name, "text": text, **kw})


def recall(w, who, query):
    with tenant_scope(who.tenant_id):
        return w["memory"].recall_for(f"{who.tenant_id}:th", query, 5)


def test_a_household_note_is_recalled_in_every_member_turn(w):
    note(w["hh"], w["pay"], "allergie-lea", "Léa est allergique aux arachides, pas de recette avec des cacahuètes.")
    for who in (w["pay"], w["mem"], w["kid"]):
        got = recall(w, who, "une recette de biscuits aux cacahuètes ?")
        assert [r.note for r in got] == ["foyer:allergie-lea"], who.email
        assert "arachides" in got[0].text and got[0].text.startswith("[Mémoire du foyer]")
    assert recall(w, w["out"], "une recette de biscuits aux cacahuètes ?") == []
    assert recall(w, w["mem"], "quel temps fera-t-il demain à Genève ?") == []  # hors sujet : rien
    with tenant_scope(INTERNAL_TENANT):
        assert w["memory"].for_tenant() is w["memory"].internal  # l'interne garde sa mémoire


def test_profile_memory_never_goes_through_the_household(w):
    with tenant_scope(w["mem"].tenant_id), pytest.raises(TeamError) as e:
        w["hh"].put_note(w["mem"], "prive", {"kind": "fact", "title": "t", "text": "mon journal", "scope": "user"})
    assert e.value.code == "bad_note"
    assert recall(w, w["pay"], "journal") == []


def test_notes_are_scrubbed_and_conflicts_go_to_the_payer_quarantine(w):
    hh = w["hh"]
    first = note(hh, w["mem"], "wifi", "Le code du wifi est dans le tiroir, clé sk-SECRET")
    assert "sk-SECRET" not in first["note"]["text"]
    note(hh, w["pay"], "wifi", "Le code du wifi est sur le routeur", base_sha="0" * 64)  # écrit sans la bonne base
    with tenant_scope(w["pay"].tenant_id):
        q = hh.quarantined(w["pay"])["quarantine"]
        assert [x["name"] for x in q] == ["wifi"] and "tiroir" in q[0]["text"]
        assert hh.notes(w["pay"], None)["notes"][0]["text"] == "Le code du wifi est sur le routeur"
    with tenant_scope(w["mem"].tenant_id), pytest.raises(TeamError) as e:
        hh.quarantined(w["mem"])
    assert e.value.status == 403


def test_forgotten_note_is_no_longer_recalled(w):
    note(w["hh"], w["mem"], "dentiste", "Rendez-vous chez le dentiste le jeudi après l'école.")
    assert recall(w, w["pay"], "quand est le dentiste ?")
    with tenant_scope(w["mem"].tenant_id):
        assert w["hh"].forget_note(w["mem"], "dentiste") == {"forgotten": True}
    assert recall(w, w["pay"], "quand est le dentiste ?") == []


def test_former_member_and_suspended_minor_recall_nothing(w):
    note(w["hh"], w["pay"], "cantine", "La cantine ferme le mercredi.")
    with tenant_scope(w["pay"].tenant_id):
        w["hh"].remove(w["pay"], w["mem"].account_id)
        w["hh"].revoke_consent(w["pay"], w["kid"].account_id)
    assert recall(w, w["mem"], "la cantine mercredi") == []
    assert recall(w, w["kid"], "la cantine mercredi") == []
