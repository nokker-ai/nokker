"""`EscalatorV0` — texte renvoyé + journalisation (PLAN-P1 §2.3)."""
from __future__ import annotations

import logging

from nokker.core.turn import InputCheck, Turn
from nokker.qa.escalate import EscalatorV0


def test_escalate_with_reason():
    turn = Turn(thread_id="t1", index=0, input=InputCheck(text="x"))
    text = EscalatorV0(addressee="Alice").escalate(turn, "TVA sur facture à l'étranger")
    assert "Alice" in text and "TVA sur facture à l'étranger" in text


def test_escalate_without_reason():
    turn = Turn(thread_id="t1", index=0, input=InputCheck(text="x"))
    text = EscalatorV0().escalate(turn, "")
    assert text == "Je transmets à l'équipe."  # défaut neutre : l'instance interne nomme son opérateur (config)


def test_escalate_custom_addressee():
    turn = Turn(thread_id="t1", index=0, input=InputCheck(text="x"))
    text = EscalatorV0(addressee="Bruno").escalate(turn, "motif")
    assert "Bruno" in text


def test_escalate_logs_thread_and_reason(caplog):
    turn = Turn(thread_id="fil-42", index=3, input=InputCheck(text="x"))
    with caplog.at_level(logging.INFO, logger="nokker.qa.escalate"):
        EscalatorV0().escalate(turn, "motif précis")
    assert any("fil-42" in r.message and "motif précis" in r.message for r in caplog.records)
