"""Tour MÊME d'une carte « à payer » (bêta, banc 03.11 : 102 conversations de paiement sur 343) : la personne demande un
achat avec un montant (drapeau `montant`), le modèle pose la carte (pay_request → HITL `payments:to_pay`) puis répond
« la carte t'attend ». La carte EST la remise de la décision à la personne : la réponse n'est pas réécrite. Négatifs :
pas de carte, autre montant annoncé, « c'est payé », carte d'un autre type → l'escalade reste.

Un `Kernel` complet (fakes de `tests/unit/approvals/test_executor.py`), le vrai calcul des drapeaux
(`qa.invariants.domain_flags`) et la vraie porte bascule (`GatesV0.switch`)."""
from __future__ import annotations

import pytest

from nokker.approvals.memory import InMemoryApprovals
from nokker.core.loop import Kernel
from nokker.core.ports import Completion, ToolSpec
from nokker.core.turn import Decision, Effect, Gate, InputCheck, Message, Tier, ToolCall, TurnInput, Usage, Verdict
from nokker.qa.gates import GatesV0
from nokker.qa.invariants import domain_flags
from tests.unit.approvals.test_executor import Ctx, Esc, Inv, LedgerFake, Mem, Pol, StoreFake, Tools

ASK = "Prépare le paiement de la bouilloire inox à 38.90 CHF sur shop.example.ch"
ARGS = {"merchant": "shop.example.ch", "amount": "38.90", "currency": "CHF",
        "items": [{"name": "Bouilloire inox", "quantity": 1, "unit_price": "38.90"}], "url": "https://shop.example.ch/c"}
OK_REPLY = "La carte de 38.90 CHF t'attend, rien n'est payé avant ton accord."


class RealFlags(Inv):
    def check_input(self, text):
        return InputCheck(text=text, domain_flags=domain_flags(text))


class CardGates(GatesV0):
    """Porte d'action : `pay_request` (ou l'outil `other`) → carte ; porte bascule : la VRAIE (GatesV0)."""

    rule: str = "payments:to_pay"

    def action(self, call, spec, turn):
        return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=self.rule, reason="à payer",
                        strength="strong")]


def _kernel(reply, *, tool="pay_request", rule="payments:to_pay", call=True):
    script = ([Message(role="assistant", content="", tool_calls=[ToolCall(id="c1", name=tool, arguments=ARGS)])]
              if call else []) + [Message(role="assistant", content=reply)]

    def complete(messages, tools, *, reasoning, session_key, max_tokens=2048):
        return Completion(message=script.pop(0) if len(script) > 1 else script[0], usage=Usage(prompt=5), model="t",
                          latency_s=0.0)

    tools_ = Tools()
    tools_.specs = {tool: ToolSpec(tool, "carte", {}, Effect.EXTERNAL_SEND if hasattr(Effect, "EXTERNAL_SEND")
                                   else Effect.DESTRUCTIVE)}
    gates = CardGates(invariants=RealFlags())
    gates.rule = rule
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=RealFlags(), gates=gates, escalator=Esc(),
               policy=Pol(), providers={Tier.SHIP: type("S", (), {"complete": staticmethod(complete)})()},
               tools=tools_, ledger=LedgerFake())
    k.approvals = InMemoryApprovals()
    return k


def _turn(k, text=ASK):
    return k.run_turn(TurnInput(thread_id="fil-pay", user_text=text, sender="alice"))


def test_the_person_message_raises_the_amount_flag():
    assert "montant" in domain_flags(ASK)


def test_a_pay_card_posed_this_turn_keeps_the_reply():
    t = _turn(_kernel(OK_REPLY))
    assert t.approval_ids, "la carte doit être posée"
    assert not t.escalated and t.reply == OK_REPLY


@pytest.mark.parametrize("reply", [OK_REPLY.replace("38.90", "38,90"), "C'est prêt : 38.90 CHF, à toi de valider.",
                                   "La carte t'attend : Bouilloire inox, 38.90 CHF. Rien n'est payé tant que tu n'as pas "
                                   "approuvé.", "La carte de 38.90 CHF t'attend, valable jusqu'au 04.11.2026 à 14:30."])
def test_the_same_amount_written_otherwise_keeps_the_reply(reply):
    t = _turn(_kernel(reply))
    assert not t.escalated and t.reply == reply


def test_no_card_keeps_the_escalation():
    t = _turn(_kernel(OK_REPLY, call=False))
    assert not t.approval_ids and t.escalated and t.reply != OK_REPLY


@pytest.mark.parametrize("reply", ["La carte de 48.90 CHF t'attend.",
                                   "C'est payé : 38.90 CHF chez shop.example.ch.",
                                   "Paiement effectué, 38.90 CHF.",
                                   "La bouilloire a été payée (38.90 CHF)."])
def test_another_amount_or_a_paid_claim_keeps_the_escalation(reply):
    t = _turn(_kernel(reply))
    assert t.approval_ids and t.escalated and t.reply != reply


def test_a_card_of_another_kind_keeps_the_escalation():
    t = _turn(_kernel(OK_REPLY, tool="terminal", rule="effect:destructive"))
    assert t.approval_ids and t.escalated and t.reply != OK_REPLY


def test_a_pay_request_card_under_another_rule_keeps_the_escalation():
    t = _turn(_kernel(OK_REPLY, rule="household:guardian"))
    assert t.approval_ids and t.escalated


def test_another_domain_flag_still_escalates():
    """Seul `montant` est satisfait par la carte : un drapeau fiscal ou juridique du même message reste."""
    text = ASK + " et dis-moi si je peux la déduire de mes impôts"
    assert {"montant", "fiscal"} <= domain_flags(text)
    t = _turn(_kernel(OK_REPLY), text)
    assert t.approval_ids and t.escalated


@pytest.mark.parametrize("text,amounts", [
    ("La carte de 38.90 CHF t'attend", {3890}), ("CHF 38.90, à toi", {3890}), ("38,90 € au total", {3890}),
    ("un total de CHF 1'290.00", {129000}), ("1 bouilloire, 2 articles", set()), ("Fr. 12.-", {1200}),
    ("valable jusqu'au 04.11.2026 à 14:30", set()), ("prix 19.45 l'unité", {1945}),
    # prudence : un décimal nu ambigu compte comme un montant (il n'exonère jamais : la réécriture reste)
    ("le 03.11", {311}),
])
def test_amounts_read_from_the_reply(text, amounts):
    from nokker.qa.gates import _reply_amounts

    assert _reply_amounts(text) == amounts


@pytest.mark.parametrize("text,paid", [
    ("Rien n'est payé avant ton accord.", False), ("Ce n'est pas encore payé.", False),
    ("Rien n'est encore payé.", False), ("Nothing is paid until you approve.", False),
    ("C'est payé.", True), ("La bouilloire a été payée.", True), ("Paiement effectué.", True),
    ("J'ai payé la bouilloire.", True), ("The order is paid.", True), ("Die Bestellung ist bezahlt.", True),
    ("Die Bestellung ist noch nicht bezahlt.", False),
])
def test_paid_claims_and_their_negations(text, paid):
    from nokker.qa.gates import _claims_paid

    assert _claims_paid(text) is paid
