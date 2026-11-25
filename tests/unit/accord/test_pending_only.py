"""ACCORD — l'appareil ne voit et ne tranche que des cartes EN ATTENTE (18.11.2026, après le smoke rc.9.38) : une carte
tranchée avant l'envoi n'est jamais poussée ; tranchée ailleurs après l'envoi, elle est retirée (`withdraw`) ; une
décision de l'appareil arrivée après celle de la CLI reçoit `already_decided` et n'écrit rien. Une carte inconnue
reste `unknown_card`."""
from __future__ import annotations

from tests.unit.accord.test_hub import card, connect, device, make, pair, ready


def test_a_card_decided_before_sending_is_never_pushed():
    hub, b, _, _ = make()
    d = device()
    pair(hub, d)
    s = connect(hub, d)
    a = card(b)
    b.decide_local(a.id, "approve", "local-owner", via="cli")  # tranchée avant le premier tour de l'appareil
    assert s.poll() == []
    a2 = card(b, cid="c2")
    out = s.poll()
    assert [m["type"] for m in out] == ["card"] and out[0]["card_id"] == a2.id  # la suivante, seule en attente


def test_a_card_decided_elsewhere_after_sending_is_withdrawn():
    hub, b, _, _ = make()
    d, s, a = ready(hub, b)
    b.decide_local(a.id, "reject", "local-owner", via="web")
    assert s.poll() == [{"type": "withdraw", "card_id": a.id}]
    assert s.poll() == []  # retirée une fois, rien d'autre


def test_race_cli_first_then_device_gets_already_decided_and_writes_nothing():
    hub, b, _, executed = make()
    d, s, a = ready(hub, b)
    b.decide_local(a.id, "approve", "local-owner", via="cli")  # la CLI gagne
    out = s.handle(d.decision_message("refuse"))  # l'appareil arrive après, avant son prochain tour
    assert out == [{"type": "error", "code": "already_decided", "card_id": a.id}]
    got = b.get(a.id)
    assert got.status == "approved" and executed == []  # la décision de la CLI tient, l'appareil n'a rien écrit
    decided = [(t, d) for t, d in b._load_events(a.id) if t in ("approved", "rejected")]
    assert decided == [("approved", decided[0][1])] and decided[0][1].get("via") == "cli"  # une seule décision : la CLI


def test_race_device_first_then_cli_is_refused():
    import pytest

    from nokker.approvals.errors import ApprovalNotPending

    hub, b, _, executed = make()
    d, s, a = ready(hub, b)
    assert s.handle(d.decision_message("approve")) == []  # l'appareil gagne
    assert b.get(a.id).status == "approved" and [x.id for x in executed] == [a.id]
    with pytest.raises(ApprovalNotPending):
        b.decide_local(a.id, "reject", "local-owner", via="cli")


def test_unknown_card_stays_unknown_card():
    hub, b, _, _ = make()
    d, s, a = ready(hub, b)
    d.card = dict(d.card, card_id="pas-une-carte")
    out = s.handle(d.decision_message("approve"))
    assert out[0]["code"] == "unknown_card"
