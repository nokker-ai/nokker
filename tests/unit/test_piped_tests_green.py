"""`conclude_on_green` et les tests à sortie coupée (`<tests> 2>&1 | tail -N`, `| head -N`) : reconnaissance fermée, et
code de retour des TESTS (pas celui de `tail`, toujours 0 : un `| tail` masque l'échec). Réalisé par `pipefail_command`
(sous-shell bash), établi avec un vrai bash et de faux exécutables de tests."""
from __future__ import annotations

import stat
import subprocess

import pytest

from nokker.core.ports import ToolContext
from nokker.core.turn import Effect, ToolResult
from nokker.tools.code import CodeToolSource, Workspace, is_test_command, pipefail_command
from tests.unit.test_chain_end_rules import CodeTools, _kernel, patch, pytest_run, wrap  # noqa: F401 — fixture autouse ci-dessous

CTX = ToolContext(thread_id="th_1", turn_index=0, channel="api")


@pytest.fixture(autouse=True)
def _classify(monkeypatch):
    monkeypatch.setattr("nokker.tools.code.classify_command", lambda command: Effect.WRITE)


RECOGNISED = [
    "pytest -q 2>&1 | tail -5", "pytest -q 2>&1 | head -20", "pytest | tail -3", "pytest | head -1", "pytest -q | tail -n 5",
    "pytest -q 2>&1 | tail -n 1000", "pytest 2>&1|tail -3", "pytest 2>&1 | tail -1000", "python -m pytest tests -q 2>&1 | tail -15",
    "python3 -m unittest 2>&1 | tail -4", "cd app && pytest -q 2>&1 | tail -5", "npm test 2>&1 | tail -20",
    "go test ./... 2>&1 | head -30", "cargo test 2>&1 | tail -8", "make test 2>&1 | tail -3", "tox 2>&1 | tail -3",
    "  pytest -q 2>&1 | tail -5  ", "pytest -q 2>&1  |  tail   -5",
]
REFUSED = [
    "pytest 2>&1 | tail", "pytest 2>&1 | tail -f", "pytest 2>&1 | tail -0", "pytest 2>&1 | tail -1001", "pytest 2>&1 | tail -10000",
    "pytest 2>&1 | tail -05", "pytest 2>&1 | tail -n", "pytest 2>&1 | tail -n5", "pytest 2>&1 | tail -3 -q",
    "pytest 2>&1 | tail -3 | tee out", "pytest 2>&1 | tee out", "pytest 2>&1 | grep FAIL", "pytest 2>&1 | grep FAIL | tail -3",
    "pytest 2>&1 | cat", "pytest 2>&1 | tail -3; echo ok", "pytest 2>&1 | tail -3 && rm -rf x", "pytest 2>&1 | tail -3 || true",
    "pytest 2>&1 | tail -3 > out.txt", "pytest 2>&1 | tail -3 >> out.txt", "pytest 2>&1 | tail -3 &", "pytest 2>&1 | tail -3 2>&1",
    "pytest 2>&1 | tail -3\nrm -rf x", "pytest\n| tail -3", "pytest 2>&1 | tail -3\n", "pytest | tail -3 # fini",
    "pytest; false 2>&1 | tail -3", "pytest && true | tail -3", "pytest || true | tail -3", "true | pytest | tail -3",
    "pytest --fix 2>&1 | tail -3", "pytest -o x=y 2>&1 | tail -3", "pytest --junitxml=r.xml 2>&1 | tail -3",
    "ruff check . 2>&1 | tail -3", "make build 2>&1 | tail -3", "npm run lint 2>&1 | tail -3", "tsc --noEmit | tail -3",
    "false 2>&1 | tail -3", "true 2>&1 | tail -3", "echo pytest | tail -3", "ls | tail -3", "pytest 2>/dev/null | tail -3",
    "pytest 2>&1 >out | tail -3", "pytest $(x) 2>&1 | tail -3", "pytest `x` 2>&1 | tail -3", "pytest | tail -3 | head -2",
    "cd a && cd b && pytest 2>&1 | tail -3", "cd a; pytest 2>&1 | tail -3", "pytest 2>&1 | head -n 5 -c 3", "pytest 2>&1 | HEAD -5",
    "pytest 2>&1 | tail -3 ;", "pytest 2>&1 | /bin/tail -3", "pytest 2>&1 | sudo tail -3", "pytest 2>&1 | tail --lines=3",
    "pytest | | tail -3", "| tail -3", "2>&1 | tail -3", "pytest 2>&1 | tail -3.5", "pytest 2>&1 | tail -+3", "pytest 2>&1 | tail - 3",
]


@pytest.mark.parametrize("command", RECOGNISED)
def test_piped_forms_are_recognised(command):
    assert is_test_command(command) is True, command
    assert pipefail_command(command) is not None


@pytest.mark.parametrize("command", REFUSED)
def test_everything_else_stays_unrecognised(command):
    assert is_test_command(command) is False, command
    assert pipefail_command(command) is None  # exécutée telle quelle : rien n'est réécrit


@pytest.mark.parametrize("command", ["pytest", "pytest -q tests/unit", "cd app && pytest -q", "npm test", "ruff check ."])
def test_unpiped_commands_are_not_rewritten(command):
    assert pipefail_command(command) is None


def test_rewriting_wraps_in_a_subshell_and_keeps_the_command_as_written():
    assert pipefail_command("pytest -q 2>&1 | tail -5") == "(set -o pipefail; pytest -q 2>&1 | tail -5)"
    assert pipefail_command("  pytest | head -3 ") == "(set -o pipefail; pytest | head -3)"
    assert pipefail_command("cd app && pytest -q 2>&1 | tail -5") == "cd app && (set -o pipefail; pytest -q 2>&1 | tail -5)"


# ── vrai bash, faux exécutables ──

@pytest.fixture()
def fakebin(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()

    def make(name, rc):
        f = d / name
        f.write_text(f"#!/bin/bash\nfor i in 1 2 3 4 5 6; do echo \"ligne $i\"; done\nexit {rc}\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)

    make("pytest", 1)
    ok = tmp_path / "ok"
    ok.mkdir()
    f = ok / "pytest"
    f.write_text("#!/bin/bash\nfor i in 1 2 3 4 5 6; do echo \"ligne $i\"; done\nexit 0\n")
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return {"red": str(d), "green": str(ok)}


def _bash(command, path):
    r = subprocess.run(["bash", "-c", command], capture_output=True, text=True, env={"PATH": path + ":/usr/bin:/bin"},
                       cwd="/", check=False)
    return r.returncode, r.stdout


def test_real_bash_without_the_rewrite_the_tail_masks_the_code(fakebin):
    """La preuve du piège : sans réécriture, des tests rouges rendent 0."""
    rc, out = _bash("pytest -q 2>&1 | tail -3", fakebin["red"])
    assert rc == 0 and out.split() == ["ligne", "4", "ligne", "5", "ligne", "6"]


@pytest.mark.parametrize("form", ["pytest -q 2>&1 | tail -3", "pytest | tail -3", "pytest -q 2>&1 | head -20", "pytest | head -50",
                                  "pytest -q 2>&1 | tail -1000", "pytest | head -1000"])
def test_real_bash_rewrite_gives_the_exact_code_of_the_tests(fakebin, form):
    """La sortie (6 lignes) est plus courte que N, ou `tail` lit tout : aucun tube fermé tôt, donc le code exact."""
    rewritten = pipefail_command(form)
    assert _bash(rewritten, fakebin["red"]) == (1, _bash(form, fakebin["red"])[1])
    assert _bash(rewritten, fakebin["green"]) == (0, _bash(form, fakebin["green"])[1])  # même sortie qu'avant


@pytest.mark.parametrize("form", ["pytest -q 2>&1 | head -3", "pytest | head -2", "pytest | head -1"])
def test_real_bash_head_closing_the_pipe_early_is_never_a_false_green(fakebin, form):
    """`head` plus court que la sortie ferme le tube : le test peut mourir de SIGPIPE (141) selon la course. Attendu :
    jamais 0 sur des tests rouges ; sur des tests verts, 0 ou 141 (la sortie reste la même)."""
    rewritten = pipefail_command(form)
    for _ in range(15):  # la course varie d'un passage à l'autre
        rc_red, out_red = _bash(rewritten, fakebin["red"])
        assert rc_red != 0 and rc_red in (1, 141) and out_red == _bash(form, fakebin["red"])[1]
        rc_green, out_green = _bash(rewritten, fakebin["green"])
        assert rc_green in (0, 141) and out_green == _bash(form, fakebin["green"])[1]


def test_only_a_zero_exit_code_can_be_green(fakebin):
    """141 (SIGPIPE derrière `head`) ou 1 : jamais vert. Seul 0 clôt la chaîne."""
    k, _tools = _kernel([], conclude_on_green=True)
    k.note_outcome("t1", "patch", {}, ToolResult(call_id="", name="patch", content="ok"))
    for code in (141, 1, 2, 130, 137, None):
        k.note_outcome("t1", "terminal", {"command": "pytest -q | head -3"},
                       ToolResult(call_id="", name="terminal", content="", exit_code=code))
        assert k._chain["t1"]["green"] is False, code
    k.note_outcome("t1", "terminal", {"command": "pytest -q | head -3"}, ToolResult(call_id="", name="terminal", content="", exit_code=0))
    assert k._chain["t1"]["green"] is True


def test_real_bash_pipefail_does_not_leak_out_of_the_subshell(fakebin):
    _, out = _bash(pipefail_command("pytest | tail -1") + "; false | true; echo rc=$?", fakebin["green"])
    assert out.strip().endswith("rc=0")  # le shell appelant n'a pas pipefail


def test_real_bash_cd_prefix_is_kept_outside_the_subshell(fakebin, tmp_path):
    sub = tmp_path / "sous"
    sub.mkdir()
    _, out = _bash(pipefail_command(f"cd {sub} && pytest | tail -1") + "; pwd", fakebin["red"])
    assert out.strip().splitlines()[-1] == str(sub)


# ── de bout en bout : terminal de CodeToolSource -> note_outcome -> conclusion ──

class BashEnv:
    def __init__(self, path):
        self.path, self.commands = path, []

    def execute(self, command, **kw):
        self.commands.append(command)
        rc, out = _bash(command, self.path)
        return {"returncode": rc, "output": out}


def _terminal(path, command):
    src = CodeToolSource(root="/tmp", backend="local")
    env = BashEnv(path)
    src._spaces["th_1"] = Workspace(env=env, ops=None, todos=None, cwd="/tmp")
    return src.call("terminal", {"command": command}, CTX), env


def test_terminal_reports_the_code_of_the_tests_not_of_tail(fakebin):
    r, env = _terminal(fakebin["red"], "pytest -q 2>&1 | tail -3")
    assert r.exit_code == 1 and env.commands == ["(set -o pipefail; pytest -q 2>&1 | tail -3)"]
    r, env = _terminal(fakebin["green"], "pytest -q 2>&1 | tail -3")
    assert r.exit_code == 0
    r, env = _terminal(fakebin["red"], "pytest -q")  # sans tube : commande inchangée
    assert r.exit_code == 1 and env.commands == ["pytest -q"]
    r, env = _terminal(fakebin["red"], "false 2>&1 | tail -3")  # hors liste : inchangée, code de tail
    assert env.commands == ["false 2>&1 | tail -3"] and r.exit_code == 0


def _outcome(k, path, command):
    r, _ = _terminal(path, command)
    k.note_outcome("t1", "terminal", {"command": command}, ToolResult(call_id="", name="terminal", content="", exit_code=r.exit_code))
    return k._chain["t1"]["green"]


def test_red_tests_behind_a_tail_do_not_close_the_task_and_green_ones_do(fakebin):
    k, _tools = _kernel([], conclude_on_green=True)
    k.note_outcome("t1", "patch", {}, ToolResult(call_id="", name="patch", content="ok"))
    assert _outcome(k, fakebin["red"], "pytest -q 2>&1 | tail -3") is False  # le piège payé : tail rendait 0
    assert _outcome(k, fakebin["red"], "pytest -q | head -3") is False
    assert _outcome(k, fakebin["red"], "false 2>&1 | tail -3") is False  # hors liste : jamais verte
    assert _outcome(k, fakebin["green"], "true 2>&1 | tail -3") is False
    assert _outcome(k, fakebin["green"], "pytest -q 2>&1 | tee x | tail -3") is False
    assert _outcome(k, fakebin["green"], "pytest -q 2>&1 | tail -3") is True
    k.note_outcome("t1", "patch", {}, ToolResult(call_id="", name="patch", content="ok"))  # une écriture rouvre
    assert k._chain["t1"]["green"] is False
    assert _outcome(k, fakebin["green"], "pytest -q 2>&1 | head -50") is True


def test_a_piped_green_closes_the_chain_in_the_loop():
    k, tools = _kernel([patch(), pytest_run("pytest -q 2>&1 | tail -5"), wrap("fini")], max_calls=60, conclude_on_green=True)
    turn = k.run_turn(__import__("nokker.core.turn", fromlist=["TurnInput"]).TurnInput("t1", "corrige"))
    assert tools.commands == ["pytest -q 2>&1 | tail -5"]
    assert any(v.rule == "loop:tests_green" for v in turn.verdicts)
