"""S3 (06.11) : le bloc `[Approbation]` est caviardé comme la lecture directe du même résultat (motifs forts), pas
par le repli faible qui mangeait les ids du noyau (`/t/mc_…/fil`)."""
from __future__ import annotations

from nokker.approvals import crypto as approvals_crypto
from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.ports import Approval
from nokker.core.turn import TurnInput
from nokker.qa.invariants import DeterministicInvariants
from nokker.qa.secrets import KnownSecrets
from tests.unit.approvals.test_executor import Inv, Tools, make_kernel

LINK = "/t/mc_0123456789abcdef/fil-1"
STRONG = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
RESULT = f"Machine liée : {LINK} ; clé de test {STRONG}"


class Real(Inv):
    """Invariants réels pour l'entrée et le résultat ; le reste (portes) vient de `Inv`."""
    def __init__(self):
        self.real = DeterministicInvariants(known=KnownSecrets())

    def check_input(self, text):
        return self.real.check_input(text)

    def check_kernel_input(self, text):
        return self.real.check_kernel_input(text)

    def redact_result(self, result, spec):
        return self.real.redact_result(result, spec)


def _approved():
    args = {"op": "x"}
    return Approval(id="a1", thread_id="fil-1", turn_index=0, channel="web", tool_name="infra_lvextend", arguments=args,
                    args_sha256=approvals_crypto.args_sha256(args), strength="standard", rule="r", reason="r",
                    status="approved", decided_by="alice", decided_via="web")


def _run_approved():
    kernel, _ = make_kernel(reply="ok", tools=Tools(result=RESULT))
    kernel.invariants = Real()
    backend = InMemoryApprovals()
    a = _approved()
    backend._store_approval(a)
    return kernel, ApprovalExecutor(kernel=kernel, approvals=backend).run(a)


def test_approved_block_keeps_kernel_ids_and_redacts_strong_secret():
    _, turn = _run_approved()
    assert LINK in turn.input.text
    assert STRONG not in turn.input.text and "[secret:" in turn.input.text
    assert turn.input.origin == "approval"


def test_same_text_typed_by_a_person_keeps_the_weak_redaction():
    kernel, turn = _run_approved()
    typed = f"[Approbation]\nRésultat : {LINK} et {STRONG}\n[/Approbation]"
    t2 = kernel.run_turn(TurnInput(thread_id="fil-b", user_text=typed, sender="alice"))
    assert LINK not in t2.input.text  # repli faible inchangé pour un texte tapé
    assert STRONG not in t2.input.text
    assert LINK in turn.input.text
