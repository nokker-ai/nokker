"""Fin sûre d'une chaîne de tours (`Kernel.chain_max_calls`, `Kernel.conclude_on_green`) : le plafond d'appels se remet
à zéro à chaque tour de suite (défaut établi), et la tâche se clôt quand les tests passent. Éteints : comportement d'avant."""
from __future__ import annotations

import pytest

from nokker.core.ports import ToolSpec
from nokker.core.turn import APPROVAL_CONTINUE, Effect, Message, Tier, ToolResult, TurnInput
from tests.unit.test_loop_approvals import make_kernel, tc


class CodeTools:
    """Outils de code factices : `patch` écrit, `terminal` rend le code de retour programmé selon la commande."""

    def __init__(self, base, codes):
        self.base, self.codes, self.commands = base, list(codes), []
        self.base.specs.update({"patch": ToolSpec("patch", "p", {}, Effect.WRITE),
                                "terminal": ToolSpec("terminal", "t", {}, Effect.WRITE)})

    def __getattr__(self, name):
        return getattr(self.base, name)

    def call(self, n, a, ctx):
        self.base.called.append(n)
        if n == "terminal":
            self.commands.append(a.get("command"))
            code = self.codes.pop(0) if self.codes else 0
            return ToolResult(call_id="", name=n, content=f"rc={code}", exit_code=code)
        return ToolResult(call_id="", name=n, content="ok")


def patch(i=1):
    return Message("assistant", tool_calls=[tc("patch", path="s.py", n=i)])


def pytest_run(cmd="pytest -q"):
    return Message("assistant", tool_calls=[tc("terminal", command=cmd)])


def wrap(text="Terminé : voilà le résumé."):
    return Message("assistant", text)


@pytest.fixture(autouse=True)
def _classify(monkeypatch):
    # sans l'amont, `classify_command` rend DESTRUCTIVE et la commande n'est jamais « run » : on le fixe à WRITE
    monkeypatch.setattr("nokker.tools.code.classify_command", lambda command: Effect.WRITE)


def _kernel(script, codes=(), **flags):
    k, base = make_kernel(script)
    tools = CodeTools(base, codes)
    k.tools = tools
    for name, value in flags.items():
        setattr(k, name, value)
    return k, tools


def _chain(k, n_follow):
    """Un message de la personne, puis `n_follow` tours de suite (décisions de cartes) : les tours de la chaîne."""
    turns = [k.run_turn(TurnInput("t1", "corrige slugify"))]
    for _ in range(n_follow):
        turns.append(k.run_turn(TurnInput("t1", "[Approbation] ok", control=APPROVAL_CONTINUE)))
    return turns


# ── plafond cumulé ──


def test_default_cap_restarts_at_every_follow_turn():
    """Défaut établi : 3 tours de la chaîne font 3 × `max_calls` appels (ici 3 × 2)."""
    script = [patch(), patch(), wrap()] * 3
    k, tools = _kernel(script, max_calls=2)
    _chain(k, 2)
    assert tools.called == ["patch"] * 6


def test_chain_max_calls_caps_the_whole_chain_and_concludes():
    script = [patch(), patch(), patch(), wrap("a"), patch(), patch(), wrap("b"), wrap("c")]
    k, tools = _kernel(script, max_calls=3, chain_max_calls=5)
    turns = _chain(k, 2)
    assert tools.called == ["patch"] * 5  # 3 + 2 + 0 : la chaîne s'arrête au plafond cumulé
    assert [t.reply for t in turns] == ["a", "b", "c"]
    last = [v for v in turns[2].verdicts if v.rule == "loop:max_calls"]
    assert last and "cumulés" in last[0].reason


def test_a_message_from_the_person_resets_the_chain():
    script = [patch(), patch(), wrap("a"), patch(), patch(), wrap("b")]
    k, tools = _kernel(script, max_calls=2, chain_max_calls=2)
    k.run_turn(TurnInput("t1", "première demande"))
    k.run_turn(TurnInput("t1", "deuxième demande"))
    assert tools.called == ["patch"] * 4


# ── tests verts ──


def test_green_tests_after_a_write_conclude_instead_of_looping():
    # après le test vert, le modèle « bouclerait » (patch, patch…) : le noyau prend la main avant son appel suivant
    script = [patch(), pytest_run(), wrap("tout est vert"), patch(2), patch(3)]
    k, tools = _kernel(script, max_calls=60, conclude_on_green=True)
    turn = k.run_turn(TurnInput("t1", "corrige slugify"))
    assert tools.called == ["patch", "terminal"] and turn.reply == "tout est vert"
    assert len(k.providers[Tier.SHIP].script) == 2  # le boucleur n'a pas été rappelé
    from nokker.core.loop import GREEN_WRAP_UP_NOTE

    last = k.providers[Tier.SHIP].seen[-1][-1]  # la note du dernier appel : celle des tests verts, pas « limite atteinte »
    assert last["content"] == GREEN_WRAP_UP_NOTE
    assert any(v.rule == "loop:tests_green" for v in turn.verdicts) and not turn.error


def test_flag_off_the_model_keeps_looping_after_green_tests():
    script = [patch(), pytest_run(), patch(2), patch(3), wrap("fini")]
    k, tools = _kernel(script, max_calls=60)
    turn = k.run_turn(TurnInput("t1", "corrige slugify"))
    assert tools.called == ["patch", "terminal", "patch", "patch"] and turn.reply == "fini"


@pytest.mark.parametrize("script,codes", [
    ([patch(), pytest_run(), wrap()], [1]),                          # tests rouges
    ([pytest_run(), patch(), wrap()], [0]),                          # vert AVANT toute écriture
    ([patch(), pytest_run("ruff check ."), patch(2), wrap()], [0]),  # linter : pas des tests
    ([patch(), pytest_run("pytest -q | tail"), patch(2), wrap()], [0]),  # commande composée : non reconnue
])
def test_no_conclusion_without_green_tests_after_a_write(script, codes):
    k, tools = _kernel(script, codes, max_calls=60, conclude_on_green=True)
    turn = k.run_turn(TurnInput("t1", "corrige slugify"))
    assert not any(v.rule == "loop:tests_green" for v in turn.verdicts) and len(tools.called) == len(script) - 1


def test_a_write_after_green_reopens_the_task():
    script = [patch(), pytest_run(), wrap("fini")]
    k, _tools = _kernel(script, max_calls=60, conclude_on_green=True)
    k.note_outcome("t1", "patch", {}, ToolResult(call_id="", name="patch", content="ok"))
    k.note_outcome("t1", "terminal", {"command": "pytest"}, ToolResult(call_id="", name="terminal", content="", exit_code=0))
    assert k._chain["t1"]["green"] is True
    k.note_outcome("t1", "patch", {}, ToolResult(call_id="", name="patch", content="ok"))
    assert k._chain["t1"]["green"] is False


def test_green_tests_run_on_a_card_conclude_the_follow_turn_without_a_tool_call():
    """Commande de tests exécutée par l'exécuteur après une carte : la suite conclut tout de suite."""
    k, tools = _kernel([wrap("c'est fait")], max_calls=60, conclude_on_green=True)
    k.note_outcome("t1", "patch", {"path": "s.py"}, ToolResult(call_id="", name="patch", content="ok"))
    k.note_outcome("t1", "terminal", {"command": "python -m pytest tests -q"},
                   ToolResult(call_id="", name="terminal", content="1 passed", exit_code=0))
    turn = k.run_turn(TurnInput("t1", "[Approbation] ok", control=APPROVAL_CONTINUE))
    assert tools.called == [] and turn.reply == "c'est fait"
    assert any(v.rule == "loop:tests_green" for v in turn.verdicts)


def test_executor_reports_card_results_to_the_kernel():
    from nokker.approvals import crypto as approvals_crypto
    from nokker.approvals.executor import ApprovalExecutor
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.core.ports import Approval

    backend = InMemoryApprovals()
    k, tools = _kernel([wrap("ok")], max_calls=60, conclude_on_green=True)
    args = {"command": "pytest -q"}
    k.note_outcome("t1", "patch", {}, ToolResult(call_id="", name="patch", content="ok"))
    approval = Approval(id="a9", thread_id="t1", turn_index=0, channel="api", tool_name="terminal", arguments=args,
                        args_sha256=approvals_crypto.args_sha256(args), strength="standard", rule="permissions:default",
                        status="approved", decided_by="local-owner", decided_via="local-token")
    backend._store_approval(approval)
    ApprovalExecutor(kernel=k, approvals=backend).run(approval)
    assert tools.commands == ["pytest -q"] and k._chain["t1"]["green"] is True


@pytest.mark.parametrize("command,expected", [
    ("pytest", True), ("pytest -q tests/unit", True), ("python -m pytest -x", True), ("python3 -m unittest", True),
    ("cd app && pytest -q", True), ("npm test", True), ("npm run test", True), ("pnpm test", True), ("go test ./...", True),
    ("cargo test", True), ("make test", True), ("mvn test", True), ("tox", True), ("npx vitest", False),
    ("ruff check .", False), ("make build", False), ("npm run lint", False), ("tsc --noEmit", False),
    ("pytest -q | tail", False), ("pytest && rm -rf x", False), ("pytest --fix", False), ("", False),
])
def test_is_test_command_is_closed(command, expected):
    from nokker.tools.code import is_test_command

    assert is_test_command(command) is expected
