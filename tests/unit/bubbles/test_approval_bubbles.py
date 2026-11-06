"""Bulle `approval` de chaque carte qui revient aux humains (drapeau `approval_bubbles`) : pose, décision, exécution,
expiration, relecteur automatique sans doublon, locataire, protection contre le modèle et les clients ; drapeau éteint =
rien de changé."""
from __future__ import annotations

import pytest

from nokker.approvals.memory import InMemoryApprovals
from nokker.bubbles.types import BubbleError, BubbleNotFound
from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
from nokker.tenancy.context import scoped_thread_id, tenant_scope

TA, TB = "tn_aaaaaaaa", "tn_bbbbbbbb"
FIL = scoped_thread_id(TA, "th_card")


def _wire(hub, clock, on=True):
    hub.approval_bubbles = on
    b = InMemoryApprovals(clock=clock, account_tenant=lambda u: TA if u == "us_alice" else None)
    if on:
        b.card_observer = hub
    with tenant_scope(TA):
        hub.threads.ensure(FIL, "us_alice", "achète")
    return b


def _card(b, *, notify=True, thread=FIL, strength="strong", tool="pay_request", rule="payments:to_pay"):
    turn = Turn(thread_id=thread, index=0, input=InputCheck(text="achète"), channel="web", sender="us_alice")
    with tenant_scope(TA):
        return b.request(turn, ToolCall(id="c1", name=tool, arguments={"amount": "42.90"}),
                         Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=rule,
                                 reason="à payer", strength=strength), None, notify=notify)


def _bubbles(hub, thread=FIL):
    with tenant_scope(TA):
        return [x for x in hub.bubbles.list_thread(thread) if x.kind == "approval"]


def test_flag_off_opens_no_bubble(hub, clock, rec):
    b = _wire(hub, clock, on=False)
    _card(b)
    assert _bubbles(hub) == [] and "bubble.created" not in rec.types()


def test_a_card_for_humans_opens_its_bubble_then_follows_the_decision(hub, clock, rec):
    b = _wire(hub, clock)
    card = _card(b)
    [bub] = _bubbles(hub)
    assert bub.title == "pay_request" and bub.status == "working"
    assert bub.state == {"approval_id": card.id, "tool_name": "pay_request", "status": "pending"}
    assert "decided_via" not in bub.state and rec.of("bubble.created")
    with tenant_scope(TA):
        b.decide_local(card.id, "approve", "us_alice")
    [bub] = _bubbles(hub)
    assert bub.status == "done" and bub.state["status"] == "approved" and bub.state["decided_via"]
    assert bub.state["decided_via"] != "auto-review"
    with tenant_scope(TA):
        b.mark_executed(card.id, "ok")
    [bub] = _bubbles(hub)
    assert bub.state["status"] == "executed" and bub.status == "done"
    assert rec.of("bubble.updated")


def test_a_rejected_card_is_done_not_failed(hub, clock):
    b = _wire(hub, clock)
    card = _card(b)
    with tenant_scope(TA):
        b.decide_local(card.id, "reject", "us_alice")
    [bub] = _bubbles(hub)
    assert bub.state["status"] == "rejected" and bub.status == "done"


def test_an_expired_card_fails_its_bubble(hub, clock):
    b = _wire(hub, clock)
    card = _card(b)
    clock.t += 10 * 24 * 3600
    with tenant_scope(TA):
        assert b.get(card.id).status == "expired"
    [bub] = _bubbles(hub)
    assert bub.state["status"] == "expired" and bub.status == "failed"


def test_auto_review_never_doubles_the_bubble(hub, clock):
    b = _wire(hub, clock)
    std = {"strength": "standard", "tool": "write_file", "rule": "effect:write"}
    held = _card(b, notify=False, **std)  # tenue par le relecteur : pas encore aux humains
    assert _bubbles(hub) == []
    with tenant_scope(TA):
        b.notify_pending(held)  # escaladée : bulle
    assert len(_bubbles(hub)) == 1
    # approuvée par le relecteur alors que sa bulle existe : mise à jour, pas de seconde bulle
    with tenant_scope(TA):
        b.decide_auto(held.id, "approve", "sûr")
        turn = Turn(thread_id=FIL, index=0, input=InputCheck(text="x"), channel="web", sender="us_alice")
        hub.auto_review_decided(turn, b.get(held.id))
    [bub] = _bubbles(hub)
    assert bub.state["approval_id"] == held.id and bub.status == "done"
    # une carte approuvée par le relecteur SANS passer par les humains : sa bulle unique, comme avant
    other = _card(b, notify=False, **std)
    with tenant_scope(TA):
        b.decide_auto(other.id, "approve", "sûr")
        hub.auto_review_decided(turn, b.get(other.id))
    assert sorted(x.state["approval_id"] for x in _bubbles(hub)) == sorted([held.id, other.id])


def test_the_bubble_stays_in_the_card_tenant(hub, clock):
    b = _wire(hub, clock)
    _card(b)
    with tenant_scope(TB):
        with pytest.raises(Exception):
            hub.list_bubbles(FIL, "us_bob")
    with tenant_scope(TA):
        assert [x.kind for x, _ in hub.list_bubbles(FIL, "us_alice")] == ["approval"]
        with pytest.raises(Exception):  # même locataire, pas membre du fil : rien
            hub.list_bubbles(FIL, "us_carol")


@pytest.mark.parametrize("on", [True, False])
def test_neither_the_model_nor_a_client_can_write_a_card_bubble(hub, clock, on):
    """Sans condition (drapeau allumé ou non, bulle de la carte ou du relecteur) : un client ne crée pas de bulle
    `approval` (400, texte existant) ; pour le modèle, elle n'existe pas (BubbleNotFound, dans son fil comme ailleurs)."""
    b = _wire(hub, clock, on=on)
    card = _card(b) if on else None
    with tenant_scope(TA):
        if card is None:  # drapeau éteint : la bulle d'une décision du relecteur, comme avant
            held = _card(b, notify=False, strength="standard", tool="write_file", rule="effect:write")
            b.decide_auto(held.id, "approve", "sûr")
            hub.auto_review_decided(Turn(thread_id=FIL, index=0, input=InputCheck(text="x"), channel="web",
                                         sender="us_alice"), b.get(held.id))
    [bub] = _bubbles(hub)
    with tenant_scope(TA):
        for thread in (FIL, scoped_thread_id(TA, "autre-fil")):
            with pytest.raises(BubbleNotFound):
                hub.tool_bubble_update(thread, bub.id, state={"approval_id": "autre-carte"})
        with pytest.raises(BubbleError, match="kind non créable par un client : approval"):
            hub.create_bubble(FIL, "us_alice", kind="approval", title="x", state={"approval_id": "autre"})
    assert _bubbles(hub)[0].state == bub.state


def test_the_model_tool_answers_with_the_existing_not_found_text(hub, clock):
    """Par l'outil `bubble_update` : « refusé : BubbleNotFound <id> », gabarit existant, aucun mot nouveau."""
    from nokker.bubbles.tools import BubbleToolSource
    from nokker.core.ports import ToolContext

    b = _wire(hub, clock)
    _card(b)
    [bub] = _bubbles(hub)
    with tenant_scope(TA):
        r = BubbleToolSource(hub).call("bubble_update", {"bubble_id": bub.id, "state": {"approval_id": "x"}},
                                  ToolContext(thread_id=FIL, turn_index=0, channel="web", sender="us_alice"))
    assert r.is_error and r.content == f"refusé : BubbleNotFound {bub.id}"


def test_the_card_bubble_adds_nothing_to_the_model_context(hub, clock):
    b = _wire(hub, clock)
    card = _card(b)
    [bub] = _bubbles(hub)
    with tenant_scope(TA):
        b.decide_local(card.id, "approve", "us_alice")
        ctx = hub.context_for(FIL)  # blocs injectés au prochain tour du fil
    assert all(bub.id not in c for c in ctx) and ctx == []


def test_bubble_show_never_hands_an_approval_bubble_id_to_the_model():
    """Les seuls ids de bulle rendus au modèle sont ceux qu'il vient de créer ou de modifier (bubble_show → media,
    agent_draft → agent_draft, bubble_update → la sienne) : aucun chemin ne lui rend une bulle `approval`."""
    import inspect

    from nokker.bubbles import tools

    src = inspect.getsource(tools)
    assert 'kind="approval"' not in src and "kind == \"approval\"" not in src


def test_a_card_of_a_thread_without_bubbles_is_ignored(hub, clock):
    b = _wire(hub, clock)
    card = _card(b, thread=scoped_thread_id(TA, "crew-x"))
    with tenant_scope(TA):
        assert b.get(card.id).status == "pending"  # la carte existe, aucune bulle ni erreur
    assert _bubbles(hub, scoped_thread_id(TA, "crew-x")) == []


def test_a_broken_bubble_never_blocks_the_card(hub, clock):
    b = _wire(hub, clock)

    class Boom:
        def approval_pending(self, a):
            raise RuntimeError("boom")

    b.card_observer = Boom()
    card = _card(b)
    with tenant_scope(TA):
        assert b.get(card.id).status == "pending"
