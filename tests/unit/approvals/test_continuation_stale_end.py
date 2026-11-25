"""Fin de tour périmée (recette du 15.11) : le texte de fin rédigé AVANT la décision d'une carte (« une carte est
apparue, accepte-la ») ne reste pas comme dernier message de l'assistant que le modèle voit au tour de continuation."""
from __future__ import annotations

import dataclasses
from datetime import datetime

from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.context.engine import ZURICH, ContextEngineV0
from nokker.core.loop import Kernel
from nokker.core.turn import Message, Tier, TurnInput
from tests.unit.test_loop_approvals import Esc, Gates, Inv, LedgerFake, Mem, Pol, ScriptedProvider, StoreFake, Tools, tc

STALE = "STALE-FIN-DE-TOUR"


def _run(tmp_path, decision, drop=True):
    prefix = tmp_path / "prefix.md"
    prefix.write_text("PRÉFIXE", encoding="utf-8")
    tools = Tools()
    ctx = ContextEngineV0(tools=tools, prefix_path=str(prefix), now_fn=lambda: datetime(2026, 11, 15, tzinfo=ZURICH),
                          drop_card_end_text=drop)
    ship = ScriptedProvider([Message("assistant", tool_calls=[tc("infra_lvextend", size="50G")]),
                             Message("assistant", STALE), Message("assistant", "suite")])
    inv = Inv()
    backend = InMemoryApprovals()
    k = Kernel(store=StoreFake(), context=ctx, memory=Mem(), invariants=inv, gates=Gates(inv), escalator=Esc(),
               policy=Pol(), providers={Tier.SHIP: ship}, tools=tools, ledger=LedgerFake(), approvals=backend)
    first = k.run_turn(TurnInput("t1", "agrandis le disque"))
    card = backend.pending()[0]
    assert first.reply == STALE and first.approval_ids == [card.id]
    backend._store_approval(dataclasses.replace(card, status=decision, decided_by="alice", decided_via="passkey"))
    cont = ApprovalExecutor(kernel=k, approvals=backend).run(backend.get(card.id))
    return ship, cont


def _assistant_texts(messages):
    return [m.get("content") for m in messages if m.get("role") == "assistant" and m.get("content")]


def test_continuation_does_not_replay_the_end_text_written_before_the_decision(tmp_path):
    for decision in ("approved", "rejected"):
        ship, cont = _run(tmp_path, decision)
        assert cont is not None and cont.input.origin == "approval"
        seen = ship.seen[-1]  # l'appel du tour de continuation
        assert STALE not in _assistant_texts(seen), decision
        assert "[Approbation]" in seen[-1]["content"]


def test_an_ordinary_turn_keeps_its_replayed_end_text(tmp_path):
    prefix = tmp_path / "prefix.md"
    prefix.write_text("PRÉFIXE", encoding="utf-8")
    tools = Tools()
    ctx = ContextEngineV0(tools=tools, prefix_path=str(prefix), now_fn=lambda: datetime(2026, 11, 15, tzinfo=ZURICH))
    ship = ScriptedProvider([Message("assistant", "premier"), Message("assistant", "second")])
    inv = Inv()
    k = Kernel(store=StoreFake(), context=ctx, memory=Mem(), invariants=inv, gates=Gates(inv), escalator=Esc(),
               policy=Pol(), providers={Tier.SHIP: ship}, tools=tools, ledger=LedgerFake())
    k.run_turn(TurnInput("t1", "a"))
    k.run_turn(TurnInput("t1", "b"))
    assert "premier" in _assistant_texts(ship.seen[-1])


def test_flag_off_keeps_the_complete_history_byte_for_byte(tmp_path):
    ship, _ = _run(tmp_path, "approved", drop=False)
    assert STALE in _assistant_texts(ship.seen[-1])  # drapeau éteint : état d'avant, le texte périmé est rejoué
