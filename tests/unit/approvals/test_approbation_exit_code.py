"""Le bloc `[Approbation]` dit l'échec d'une commande dont le code de sortie (champ structuré `ToolResult.exit_code`)
n'est pas 0 — sans changer le bloc des cas réussis (cache de préfixe) ni doubler l'échec `is_error`."""
from __future__ import annotations

import pytest

from nokker.approvals.executor import _approbation_block
from nokker.core.turn import ToolResult
from nokker.qa.untrusted import fenced_result
from tests.unit.approvals import test_continuation_context as base
from tests.unit.approvals.test_continuation_context import Model, approve, build

ISSUE_RC1 = "Issue : exécutée — code de sortie 1 (≠ 0)."
FAILED_TAIL = ("Cette action est tranchée et son résultat complet est ci-dessus. Si le code de sortie indique une erreur, "
               "corrige d'abord la cause : ne relance pas la même commande telle quelle, ni sous une autre forme qui "
               "referait les mêmes vérifications sans correction. Continue la tâche à partir de ce résultat.")
OLD_TAIL = ("Cette action est tranchée et son résultat complet est ci-dessus : ne la relance pas, ni sous une autre "
            "forme (autre outil de test, mêmes vérifications). Continue la tâche à partir de ce résultat.")


class Tools(base.TermTools):
    """`terminal` rend un ToolResult réglable : texte, champ exit_code, is_error."""
    content, exit_code, is_error = "sortie", None, False

    def call(self, n, a, ctx):
        self.called.append(n)
        return ToolResult(call_id="", name=n, content=self.content, exit_code=self.exit_code, is_error=self.is_error)


def run(tmp_path, monkeypatch, **fields):
    for k, v in fields.items():
        monkeypatch.setattr(Tools, k, v)
    monkeypatch.setattr(base, "TermTools", Tools)
    model = Model()
    k, ex, backend = build(tmp_path, model)
    t2 = k.run_turn(base.TurnInput("t", "lance pytest"))
    approved = approve(backend, t2.approval_ids[-1])
    ex.run(approved)
    return model.sent[-1][-1]["content"], backend.get(approved.id)


def old_block(a, outcome):
    return "\n".join(["[Approbation]", "Outil : terminal", f"Commande : {base.CMD['command']}",
                      "Décision : executed", "Décideur : alice", "Moyen : signature", f"Résultat : {outcome}",
                      OLD_TAIL, "[/Approbation]"])


def test_failed_exit_code_is_announced(tmp_path, monkeypatch):
    text, _ = run(tmp_path, monkeypatch, exit_code=1, content=fenced_result({"returncode": 1}, "terminal:output", "KO"))
    assert ISSUE_RC1 in text and "Décision : executed" in text
    assert text.index(ISSUE_RC1) < text.index("Résultat :")
    assert FAILED_TAIL in text and OLD_TAIL not in text  # un seul texte en échec, sans contradiction
    from nokker.approvals.executor import FAILED_INSTRUCTION, FAILED_ISSUE
    assert FAILED_ISSUE.format(code=1) == ISSUE_RC1 and FAILED_INSTRUCTION == FAILED_TAIL
    import hashlib  # textes gelés à la relecture (15.11.2026) : toute dérive repasse par la relecture
    assert hashlib.sha256(FAILED_ISSUE.encode()).hexdigest()[:16] == "d2c83b85de646070"
    assert hashlib.sha256(FAILED_INSTRUCTION.encode()).hexdigest()[:16] == "7c484170a95fe276"


def test_exit_code_zero_block_is_byte_identical_to_old_format(tmp_path, monkeypatch):
    out = fenced_result({"returncode": 0}, "terminal:output", "OK")
    text, a = run(tmp_path, monkeypatch, exit_code=0, content=out)
    assert old_block(a, out) in text and "a échoué" not in text and "corrige d'abord" not in text
    assert _approbation_block(a, outcome=out, exit_code=0) == old_block(a, out)


def test_tool_without_exit_code_block_unchanged(tmp_path, monkeypatch):
    text, a = run(tmp_path, monkeypatch, exit_code=None, content="lu")
    assert old_block(a, "lu") in text
    assert _approbation_block(a, outcome="lu") == old_block(a, "lu")


def test_is_error_keeps_its_path_without_duplicate(tmp_path, monkeypatch):
    text, _ = run(tmp_path, monkeypatch, exit_code=1, content="boum", is_error=True)
    assert "Résultat : ÉCHEC — boum" in text and "a échoué" not in text and "corrige d'abord" not in text


def test_code_comes_from_structured_field_not_from_text(tmp_path, monkeypatch):
    out = fenced_result({"returncode": 1}, "terminal:output", "returncode: 1\nFAILED")
    text, _ = run(tmp_path, monkeypatch, exit_code=0, content=out)
    assert "a échoué" not in text and "corrige d'abord" not in text
    text, _ = run(tmp_path, monkeypatch, exit_code=None, content=out)
    assert "a échoué" not in text


@pytest.mark.parametrize("rc", [1, 2, 127])
def test_block_any_nonzero_code(rc):
    from nokker.approvals.memory import InMemoryApprovals  # noqa: F401
    from nokker.core.ports import Approval
    a = Approval.__new__(Approval)
    for k, v in dict(tool_name="terminal", arguments={"command": "x"}, status="approved", decided_by="alice",
                     decided_via="signature").items():
        object.__setattr__(a, k, v)
    assert f"code de sortie {rc} (≠ 0)." in _approbation_block(a, outcome="o", exit_code=rc)


def test_terminal_propagates_returncode_field():
    import inspect

    from nokker.tools import code
    src = inspect.getsource(code)
    assert 'exit_code=r.get("returncode")' in src
