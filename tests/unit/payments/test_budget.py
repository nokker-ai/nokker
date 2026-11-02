"""Budgets de dépense par membre du foyer (docs/PAYMENTS.md §4) : refus AVANT la carte, mineurs exclus par défaut,
réglés par le payeur ; cloisonnement entre locataires et entre membres."""
from __future__ import annotations

import json

import pytest

from nokker.core.ports import ToolSpec
from nokker.core.turn import Decision, Effect, Gate, Verdict
from nokker.payments import http as payments_http
from nokker.payments.gates import PaymentGates
from nokker.payments.service import GUARDIAN_RULE, PAY_RULE, PaymentError
from nokker.tenancy.context import tenant_scope
from tests.unit.payments.conftest import ALICE, BOB, CAROL, KID, TENANT, pay_call, turn_of


class AllowAll:
    def action(self, call, spec, turn):
        return [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="ok")]


SPEC = ToolSpec("pay_request", "", {}, Effect.SPEND)


def gate(world, account, **over):
    with tenant_scope(TENANT[account]):
        return PaymentGates(inner=AllowAll(), payments=world.svc).action(pay_call(**over), SPEC, turn_of(account))


def put(world, actor, member, **body):
    with tenant_scope(TENANT[actor]):
        return world.svc.set_budget(actor, member, {"currency": "CHF", "per_purchase": 100, "per_day": 150,
                                                    "per_month": 400, **body})


def test_payer_default_budget_gives_a_strong_card(world):
    [v] = gate(world, ALICE)
    assert v.decision is Decision.HITL and v.strength == "strong" and v.rule == PAY_RULE
    assert "129.90 CHF chez Boutique Example" in v.reason


@pytest.mark.parametrize("amount,why", [("250", "par achat"), ("1.00", None)])
def test_per_purchase_cap(world, amount, why):
    [v] = gate(world, ALICE, amount=amount)
    if why:
        assert v.decision is Decision.DENY and v.rule == "payments:budget" and why in v.reason
    else:
        assert v.decision is Decision.HITL


def test_day_and_month_caps_count_pending_and_signed_cards(world):
    world.request(ALICE, amount="150")  # carte en attente : réserve son montant
    [v] = gate(world, ALICE, amount="160")
    assert v.decision is Decision.DENY and "plafond du jour" in v.reason and "150.00 CHF déjà engagés" in v.reason
    [ok] = gate(world, ALICE, amount="150")
    assert ok.decision is Decision.HITL



def test_month_cap_counts_earlier_days(world):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    day = lambda d: datetime(2026, 11, d, 12, tzinfo=ZoneInfo("Europe/Zurich")).timestamp()
    world.backend.clock = world.svc.clock = lambda: day(10)
    put(world, ALICE, ALICE, per_purchase=200, per_day=200, per_month=300)
    world.approve(ALICE, world.request(ALICE, amount="190").id)  # signée : compte pour le mois entier
    world.svc.clock = world.backend.clock = lambda: day(11)  # autre jour, même mois
    [m] = gate(world, ALICE, amount="150")
    assert m.decision is Decision.DENY and "plafond du mois" in m.reason and "190.00 CHF" in m.reason
    [ok] = gate(world, ALICE, amount="100")
    assert ok.decision is Decision.HITL


def test_member_without_budget_is_refused_until_the_payer_sets_one(world):
    [v] = gate(world, BOB)
    assert v.decision is Decision.DENY and "aucun budget" in v.reason
    with pytest.raises(PaymentError) as e:  # bob ne règle pas son propre budget
        put(world, BOB, BOB)
    assert e.value.status == 403
    line = put(world, ALICE, BOB, per_purchase=200, per_day=300, per_month=500)
    assert line["source"] == "set" and line["budget"]["set_by"] == ALICE
    [ok] = gate(world, BOB)
    assert ok.decision is Decision.HITL and ok.rule == PAY_RULE


def test_currency_mismatch_is_refused(world):
    [v] = gate(world, ALICE, currency="EUR")
    assert v.decision is Decision.DENY and "en CHF, la dépense en EUR" in v.reason


def test_minor_is_refused_by_default_and_needs_explicit_payer_consent(world):
    [v] = gate(world, KID, amount="5")
    assert v.decision is Decision.DENY and v.rule == "payments:minor"
    put(world, ALICE, KID, per_purchase=20, per_day=20, per_month=50)  # budget sans accord explicite : toujours non
    [v2] = gate(world, KID, amount="5")
    assert v2.decision is Decision.DENY and v2.rule == "payments:minor"
    put(world, ALICE, KID, per_purchase=20, per_day=20, per_month=50, minor_allowed=True)
    [v3] = gate(world, KID, amount="5")
    assert v3.decision is Decision.HITL and v3.rule == GUARDIAN_RULE  # la carte va au représentant
    [v4] = gate(world, KID, amount="25")
    assert v4.decision is Decision.DENY and "par achat" in v4.reason


def test_minor_card_is_signed_by_the_guardian_only(world):
    put(world, ALICE, KID, per_purchase=20, per_day=20, per_month=50, minor_allowed=True)
    world.backend.guardian_of = lambda tid: ALICE if tid == TENANT[KID] else None
    from nokker.core.turn import Decision as D
    from nokker.core.turn import Gate as G
    from nokker.core.turn import Verdict as V

    with tenant_scope(TENANT[KID]):
        card = world.backend.request(turn_of(KID), pay_call(amount="5"),
                                     V(gate=G.ACTION, decision=D.HITL, layer=1, rule=GUARDIAN_RULE, strength="strong"),
                                     None)
        assert world.svc.store.by_approval(card.id).approver == ALICE
    from nokker.approvals.errors import ProofInvalid

    with pytest.raises(ProofInvalid):
        world.approve(KID, card.id)  # le mineur lui-même : refusé
    world.approve(KID, card.id, signer=ALICE)
    with tenant_scope(TENANT[KID]):
        rec = world.svc.store.by_approval(card.id)
    assert rec.status == "signed" and rec.envelope["signed_by"] == ALICE


def test_refusal_emits_an_event_and_no_card(world):
    events = []
    world.svc.publish = lambda t, d: events.append((t, d))
    gate(world, ALICE, amount="999")
    assert events and events[0][0] == "payment.refused" and events[0][1]["rule"] == "payments:budget"
    with tenant_scope(TENANT[ALICE]):
        assert world.backend.pending() == []


def test_tenants_and_members_are_isolated(world):
    put(world, ALICE, BOB, per_purchase=200, per_day=300, per_month=500)
    card = world.request(ALICE)
    with tenant_scope(TENANT[ALICE]):
        rec = world.svc.store.by_approval(card.id)
    # locataire : bob ne voit ni la carte ni le mandat d'alice, même par son id
    with tenant_scope(TENANT[BOB]):
        assert world.svc.store.get_mandate(rec.id) is None and world.svc.store.list_mandates(None) == []
        assert world.svc.store.spent(ALICE, "CHF", 0, world.svc.clock()) == 0
    # membres : bob ne lit pas les mandats d'alice par la route, carol (hors foyer) non plus
    for who in (BOB, CAROL):
        with tenant_scope(TENANT[who]):
            with pytest.raises(PaymentError) as e:
                world.svc.mandate(who, rec.id)
            assert e.value.status == 404
            with pytest.raises(PaymentError):
                world.svc.mandates(who, ALICE)
    # carol ne règle pas le budget de bob ; bob ne voit que sa ligne ; alice (payeuse) voit son foyer
    with pytest.raises(PaymentError):
        put(world, CAROL, BOB)
    with tenant_scope(TENANT[BOB]):
        assert [m["account"] for m in world.svc.budgets_view(BOB)["members"]] == [BOB]
    with tenant_scope(TENANT[ALICE]):
        view = world.svc.budgets_view(ALICE)
    assert {m["account"] for m in view["members"]} == {ALICE, BOB, KID} and view["is_payer"]
    # un budget posé pour bob est rangé chez bob, pas chez alice
    with tenant_scope(TENANT[ALICE]):
        assert world.svc.store.get_budget(BOB) is None
    with tenant_scope(TENANT[BOB]):
        assert world.svc.store.get_budget(BOB).per_purchase_minor == 20000
    # le payeur lit les mandats d'un membre ; bob ne lit pas ceux d'alice par ?member=
    bcard = world.request(BOB, amount="20")
    with tenant_scope(TENANT[ALICE]):
        assert [m["approval_id"] for m in world.svc.mandates(ALICE, BOB)] == [bcard.id]


def test_http_budget_routes(world):
    payments_http.configure(world.svc)
    try:
        with tenant_scope(TENANT[ALICE]):
            st, line = payments_http.dispatch(world.svc, "PUT", ["budgets", BOB], ALICE, None,
                                              lambda: {"currency": "chf", "per_purchase": "50", "per_day": "80",
                                                       "per_month": "300"})
            assert st == 200 and line["budget"]["currency"] == "CHF" and line["budget"]["per_day_minor"] == 8000
            with pytest.raises(PaymentError) as e:
                payments_http.dispatch(world.svc, "PUT", ["budgets", BOB], ALICE, None,
                                       lambda: {"currency": "CHF", "per_purchase": 90, "per_day": 80, "per_month": 300})
            assert e.value.status == 400
            st, line = payments_http.dispatch(world.svc, "DELETE", ["budgets", BOB], ALICE, None, dict)
            assert line["source"] == "none" and not line["can_spend"]
            assert payments_http.dispatch(world.svc, "GET", ["nope"], ALICE, None, dict) is None
    finally:
        payments_http.configure(None)
    assert not payments_http.is_payments_path("/v1/payments/budgets")


# ─────────── routes de mandat : le titulaire seulement (audit public, 02.11) ───────────


def _route(world, who, mid, verify=False, member=None):
    from nokker.payments import http as payments_http

    with tenant_scope(TENANT[who]):
        try:
            return payments_http.dispatch(world.svc, "GET", ["mandates", mid] + (["verify"] if verify else []),
                                          who, member, dict)
        except PaymentError as e:
            return e.status, None


def _no_foreign_ids(body, me):
    blob = json.dumps(body, ensure_ascii=False)
    assert "tenant_id" not in blob
    for acc, tid in TENANT.items():
        assert tid not in blob
        if acc != me:
            assert acc not in blob, (me, acc)


def test_mandate_routes_answer_only_the_holder(world):
    """`/v1/payments/mandates/{id}` et `/verify` : 404 pour tout autre compte, même du foyer (payeur compris, avec ou
    sans ?member=) ; la réponse ne porte jamais tenant_id ni l'identifiant d'un autre membre."""
    put(world, ALICE, BOB, per_purchase=200, per_day=300, per_month=500)
    bcard = world.request(BOB, amount="20")
    with tenant_scope(TENANT[BOB]):
        rec = world.svc.store.by_approval(bcard.id)
    for verify in (False, True):
        st, body = _route(world, BOB, rec.id, verify)
        assert st == 200
        _no_foreign_ids(body, BOB)
        for who, member in ((ALICE, None), (ALICE, BOB), (CAROL, None), (CAROL, BOB), (KID, BOB)):
            assert _route(world, who, rec.id, verify, member)[0] == 404, (who, member, verify)
    st, body = _route(world, BOB, rec.id)
    assert body["account"] == BOB and body["mandate"]["principal"]["account"].startswith("pr_")


def test_minor_mandate_visible_to_the_guardian_who_signed_it_only(world):
    put(world, ALICE, KID, per_purchase=20, per_day=20, per_month=50, minor_allowed=True)
    world.backend.guardian_of = lambda tid: ALICE if tid == TENANT[KID] else None
    from nokker.core.turn import Decision as D
    from nokker.core.turn import Gate as G
    from nokker.core.turn import Verdict as V

    with tenant_scope(TENANT[KID]):
        card = world.backend.request(turn_of(KID), pay_call(amount="5"),
                                     V(gate=G.ACTION, decision=D.HITL, layer=1, rule=GUARDIAN_RULE, strength="strong"),
                                     None)
        rec = world.svc.store.by_approval(card.id)
    # pas encore signée : le représentant ne la lit pas par ces routes
    assert _route(world, ALICE, rec.id, member=KID)[0] == 404
    world.approve(KID, card.id, signer=ALICE)
    for verify in (False, True):
        st, body = _route(world, ALICE, rec.id, verify, member=KID)
        assert st == 200
        _no_foreign_ids(body, ALICE)   # ni le compte du mineur, ni son locataire
        st, own = _route(world, KID, rec.id, verify)
        assert st == 200
        _no_foreign_ids(own, KID)      # ni le compte du représentant
        assert _route(world, BOB, rec.id, verify, member=KID)[0] == 404
        assert _route(world, ALICE, rec.id, verify)[0] == 404  # sans ?member= : pas chez elle
    st, body = _route(world, ALICE, rec.id, True, member=KID)
    assert body["valid"] is True  # l'enveloppe rendue (signataire opaque) se vérifie toujours
    st, full = _route(world, KID, rec.id)
    assert full["by_representative"] is True and full["approver"] is None and full["account"] == KID
    assert full["envelope"]["signed_by"].startswith("pr_")
