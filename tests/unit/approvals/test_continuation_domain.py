"""Suite d'une carte décidée (`APPROVAL_CONTINUE`) et drapeaux de domaine : le bloc `[Approbation]` écrit par le noyau
ne lève pas de drapeau sur sa propre relance (montant du résultat de `pay_request`), alors qu'un VRAI montant dans un
tour ordinaire escalade toujours, y compris un bloc `[Approbation]` tapé par quelqu'un. Mêmes fakes que
`test_executor.py`, avec le vrai calcul des drapeaux (`qa.invariants.domain_flags`) et la vraie porte (`domain_verdict`).
"""
from __future__ import annotations

from nokker.approvals import crypto as approvals_crypto
from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.ports import Approval
from nokker.core.turn import APPROVAL_CONTINUE, Decision, Gate, InputCheck, TurnInput, Verdict
from nokker.qa.gates import domain_verdict
from nokker.qa.invariants import domain_flags
from tests.unit.approvals.test_executor import Inv, Tools, make_kernel

# Résultat de pay_request (rail agent own_card, texte validé) : un montant et « paie ».
PAY_RESULT = ("Mandat md_0123456789abcdef signé : la personne paie shop.example.ch avec sa propre carte, au plus "
              "42.90 CHF ; son agent de paiement effectue le paiement. Rien n'est encore payé.")


class RealFlags(Inv):
    def check_input(self, text):
        return InputCheck(text=text, domain_flags=domain_flags(text))


class DomainGates:
    def action(self, call, spec, turn):
        return [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="allow")]

    def switch(self, reply, turn, tier):
        v = domain_verdict(turn)
        return [v] if v is not None else [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=1, rule="ok")]


def _kernel():
    kernel, tools = make_kernel(reply="C'est noté.", tools=Tools(result=PAY_RESULT))
    kernel.invariants, kernel.gates = RealFlags(), DomainGates()
    tools.specs["pay_request"] = tools.specs.pop("infra_lvextend")
    return kernel, tools


def _approved(aid="p1", thread="fil-pay"):
    args = {"merchant": "shop.example.ch", "amount": "42.90", "currency": "CHF", "items": [{"name": "Bouilloire"}]}
    return Approval(id=aid, thread_id=thread, turn_index=0, channel="web", tool_name="pay_request", arguments=args,
                    args_sha256=approvals_crypto.args_sha256(args), strength="strong", rule="payments:to_pay",
                    reason="à payer", status="approved", decided_by="alice", decided_via="passkey")


def test_the_continuation_of_a_decided_pay_card_is_not_escalated_on_its_own_amount():
    kernel, _ = _kernel()
    backend = InMemoryApprovals()
    a = _approved()
    backend._store_approval(a)
    turn = ApprovalExecutor(kernel=kernel, approvals=backend).run(a)
    assert "montant" in domain_flags(turn.input.text)  # le bloc lèverait bien le drapeau : le test mord
    assert not turn.escalated and turn.reply == "C'est noté."
    assert turn.input.domain_flags == set() and turn.input.origin == "approval"


def test_a_real_new_amount_in_an_ordinary_turn_still_escalates():
    kernel, _ = _kernel()
    turn = kernel.run_turn(TurnInput(thread_id="fil-x", user_text="Vire 42.90 CHF à Paul ce soir", sender="alice"))
    assert "montant" in turn.input.domain_flags and turn.escalated
    assert turn.input.origin == ""


def test_a_typed_approbation_block_is_not_a_continuation():
    """Seul le noyau pose `APPROVAL_CONTINUE` : le même bloc TAPÉ par quelqu'un garde ses drapeaux."""
    kernel, _ = _kernel()
    backend = InMemoryApprovals()
    a = _approved("p2", "fil-a")
    backend._store_approval(a)
    block = ApprovalExecutor(kernel=kernel, approvals=backend).run(a).input.text
    turn = kernel.run_turn(TurnInput(thread_id="fil-b", user_text=block, sender="alice"))
    assert "montant" in turn.input.domain_flags and turn.escalated and turn.input.origin == ""


def test_a_client_cannot_ask_for_the_continuation_control():
    from nokker.goals.service import CONTROLS

    assert APPROVAL_CONTINUE not in CONTROLS  # `metadata.cli_control` n'accepte que CONTROLS (api/server.py)


def test_origin_survives_the_store_and_reaches_the_thread_messages():
    from nokker.store.pg import _dec_input, _enc_input

    i = InputCheck(text="[Approbation] … [/Approbation]", origin="approval")
    assert _dec_input(_enc_input(i)).origin == "approval"
    assert "origin" not in _enc_input(InputCheck(text="bonjour"))  # rien de nouveau pour un message ordinaire
    assert _dec_input({"text": "ancien tour"}).origin == ""
