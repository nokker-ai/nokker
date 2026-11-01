"""`execute_code` (nokker/tools/execute_code.py, docs/LOKKAL.md §7.31) : un script qui appelle plusieurs outils en un
appel, ses appels repassant par les portes du noyau.

Pas de Docker ici : le « bac à sable » factice lance le VRAI bootstrap avec l'interpréteur des tests (même protocole,
mêmes trames, même jeton) ; seul `script_command` diffère (docker/kubectl exec en vrai, voir test_code_script_command).
Essai réel : docs/LOKKAL.md §7.31, « Essayer en vrai »."""
from __future__ import annotations

import json
import sys

import pytest

from nokker.approvals.memory import InMemoryApprovals
from nokker.core.loop import Kernel
from nokker.core.ports import ToolContext, ToolSpec, call_effect
from nokker.core.turn import (
    Decision,
    Effect,
    Gate,
    Message,
    Tier,
    ToolCall,
    ToolResult,
    TurnInput,
    Verdict,
)
from nokker.qa.gates import GatesV0
from nokker.qa.invariants import DeterministicInvariants
from nokker.qa.secrets import KnownSecrets
from nokker.tools.execute_code import (
    EMPTY,
    NEVER,
    TOOL,
    ExecuteCodeConfig,
    ExecuteCodeSource,
    ScriptSession,
    clip,
    format_block,
)
from nokker.tools.registry import Registry, StaticToolSource
from tests.unit.test_loop_approvals import (
    Ctx,
    Esc,
    LedgerFake,
    Mem,
    Pol,
    ScriptedProvider,
    StoreFake,
)

SECRET = "Zq8vR2mLx9" + "TwK4pB7nYc"  # valeur factice construite à l'exécution (gitleaks)


class FakeSandbox:
    """`script_command` : l'argv du noyau, avec l'interpréteur des tests à la place de `python3` du bac à sable."""

    def __init__(self) -> None:
        self.threads: list[str] = []

    def script_command(self, thread_id, argv):
        self.threads.append(thread_id)
        return [sys.executable if a == "python3" else a for a in argv]


class AskWrites:
    """Portes d'une session en mode « demander » (règles L1 de Lokkal) : toute écriture pose une carte."""

    def __init__(self, inner):
        self.inner = inner

    def action(self, call, spec, turn):
        if call_effect(spec, call.arguments) is Effect.WRITE:
            return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="ask:write",
                            reason="écriture : demander", strength="standard")]
        return self.inner.action(call, spec, turn)

    def switch(self, reply, turn, tier):
        return self.inner.switch(reply, turn, tier)


def _tools(calls):
    def tool(name, effect, fn):
        spec = ToolSpec(name=name, description=name, parameters={"type": "object"}, effect=effect)

        def run(args, ctx):
            calls.append((name, dict(args)))
            return fn(args, ctx)
        return spec, run

    return StaticToolSource(entries=[
        tool("read_file", Effect.READ, lambda a, c: ToolResult("", "read_file", json.dumps(
            {"content": f"contenu de {a['path']}", "path": a["path"]}))),
        tool("memory_search", Effect.READ, lambda a, c: ToolResult("", "memory_search", json.dumps(
            [{"note": "infra", "text": f"note sur {a['query']}"}]))),
        tool("leaky_read", Effect.READ, lambda a, c: ToolResult("", "leaky_read", f"jeton={SECRET}")),
        tool("write_file", Effect.WRITE, lambda a, c: ToolResult("", "write_file", "écrit")),
        tool("terminal", Effect.WRITE, lambda a, c: ToolResult("", "terminal", "lancé")),
        tool("broken", Effect.READ, lambda a, c: ToolResult("", "broken", "disque illisible", is_error=True)),
    ])


def make(script, *, tools_cfg=None, ask_writes=False, approvals=None, sandbox=None, **cfg):
    calls: list = []
    known = KnownSecrets()
    known.add("CLE_API", SECRET)
    inv = DeterministicInvariants(known=known)
    gates = GatesV0(invariants=inv)
    sandbox = sandbox or FakeSandbox()
    source = ExecuteCodeSource(resolve=lambda ctx: sandbox, cfg=ExecuteCodeConfig.from_dict(
        {"tools": tools_cfg or ["read_file", "memory_search", "leaky_read", "broken"], **cfg}))
    provider = ScriptedProvider(script)
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv,
               gates=AskWrites(gates) if ask_writes else gates, escalator=Esc(), policy=Pol(),
               providers={Tier.SHIP: provider}, tools=Registry(sources=[_tools(calls), source]),
               ledger=LedgerFake(), approvals=approvals)
    return k, calls, sandbox


def run_code(code, **kw):
    """Un tour : le modèle appelle execute_code avec `code`, puis conclut. Rend (bloc, appels d'outils, noyau)."""
    script = [Message("assistant", tool_calls=[ToolCall(id="x1", name=TOOL, arguments={"code": code})]),
              Message("assistant", "fini")]
    k, calls, _ = make(script, **kw)
    turn = k.run_turn(TurnInput("th_1", "fais le point", sender="alice"))
    block = next(m.content for m in turn.messages if m.role == "tool")
    return block, calls, k, turn


def test_three_reads_in_one_call_give_one_fixed_block():
    code = ("a = tools.read_file(path='a.py')\n"
            "b = call_tool('read_file', {'path': 'b.py'})\n"
            "m = tools.memory_search(query='disque')\n"
            "print(a['content'])\nprint(b['content'])\n"
            "{'notes': len(m), 'premiere': m[0]['text']}\n")
    block, calls, _, turn = run_code(code)
    assert [c[0] for c in calls] == ["read_file", "read_file", "memory_search"]
    assert block == ("[execute_code]\nstdout:\ncontenu de a.py\ncontenu de b.py\n"
                     'return:\n{"notes": 1, "premiere": "note sur disque"}\n'
                     f"error:\n{EMPTY}\n[/execute_code]")
    tool_msgs = [m for m in turn.messages if m.role == "tool"]
    assert len(tool_msgs) == 1  # un seul résultat pour le modèle
    assert {v.subject for v in turn.verdicts if v.subject} >= {"x1"}  # l'appel externe ET les internes passent les portes
    assert len([v for v in turn.verdicts if v.gate is Gate.ACTION]) == 4


def test_write_tool_from_script_poses_the_same_card_and_is_not_executed():
    backend = InMemoryApprovals()
    code = ("try:\n    tools.write_file(path='x.txt', content='y')\n"
            "except ApprovalPending as e:\n    print('attente', e.approval_id)\n"
            "tools.write_file(path='z.txt', content='w')\n")
    # l'appel externe (execute_code, WRITE) passerait aussi par « demander » : on l'autorise par règle, pas l'interne
    class AllowOuter(AskWrites):
        def action(self, call, spec, turn):
            return self.inner.action(call, spec, turn) if call.name == TOOL else super().action(call, spec, turn)

    script = [Message("assistant", tool_calls=[ToolCall(id="x1", name=TOOL, arguments={"code": code})]),
              Message("assistant", "fini")]
    k, calls, _ = make(script, tools_cfg=["write_file"], approvals=backend)
    k.gates = AllowOuter(k.gates)
    turn = k.run_turn(TurnInput("th_1", "écris", sender="alice"))
    block = next(m.content for m in turn.messages if m.role == "tool")
    assert calls == []  # jamais exécuté sans décision
    pending = backend.pending()
    assert [p.tool_name for p in pending] == ["write_file", "write_file"]
    assert pending[0].rule == "ask:write" and pending[0].thread_id == "th_1"
    assert pending[0].arguments == {"path": "x.txt", "content": "y"}
    assert f"attente {pending[0].id}" in block
    # non rattrapée : l'erreur du bloc porte la carte, comme le résultat « en attente » de la boucle
    assert "ApprovalPending: EN ATTENTE D'APPROBATION HUMAINE (ask:write: écriture : demander) " \
           f"[approval:{pending[1].id}]. L'action n'a pas été exécutée." in block
    assert "  ligne 5, dans <module>" in block


def test_destructive_tool_from_script_gets_the_strong_card_of_the_invariants():
    backend = InMemoryApprovals()

    def destructive():
        src = _tools([])
        spec = ToolSpec(name="infra_reboot", description="x", parameters={}, effect=Effect.DESTRUCTIVE)
        src.entries.append((spec, lambda a, c: ToolResult("", "infra_reboot", "redémarré")))
        return src

    script = [Message("assistant", tool_calls=[ToolCall(id="x1", name=TOOL, arguments={
        "code": "tools.infra_reboot(host='h1')"})]), Message("assistant", "fini")]
    k, _, _ = make(script, tools_cfg=["infra_reboot"], approvals=backend)
    k.tools = Registry(sources=[destructive(), *k.tools.sources[1:]])
    k.run_turn(TurnInput("th_1", "redémarre", sender="alice"))
    (card,) = backend.pending()
    assert card.tool_name == "infra_reboot" and card.strength == "strong" and card.rule == "effect:destructive"


def test_tool_outside_the_whitelist_is_refused_without_running():
    block, calls, _, _ = run_code("try:\n    tools.terminal(command='ls')\nexcept ToolRefused as e:\n    print(e)\n"
                                  "tools.execute_code(code='1')\n")
    assert calls == []
    assert "outil non appelable depuis execute_code : terminal (appelables : broken, leaky_read, memory_search, " \
           "read_file)" in block
    assert "ToolRefused: outil non appelable depuis execute_code : execute_code" in block


def test_never_list_wins_over_config():
    cfg = ExecuteCodeConfig.from_dict({"tools": ["read_file", "execute_code", "tool_find", "delegate"]})
    assert cfg.tools == ("read_file",)
    assert TOOL in NEVER


def test_call_cap_and_tool_error():
    block, calls, _, _ = run_code("for i in range(5):\n    tools.read_file(path=str(i))\n", max_calls=3)
    assert len(calls) == 3 and "ToolRefused: plafond d'appels d'outils atteint (3)" in block
    block, _, _, _ = run_code("tools.broken()")
    assert "ToolError: disque illisible" in block


def test_expired_or_wrong_token_is_refused_before_the_gates():
    now = [100.0]
    hits = []
    s = ScriptSession(allowed=frozenset({"read_file"}), max_calls=5, expires_at=110.0,
                      dispatch=lambda n, a: hits.append(n) or {"status": "ok", "content": "x"}, clock=lambda: now[0])
    frame = {"t": "call", "n": 1, "token": s.token, "name": "read_file", "args": {"path": "a"}}
    assert s.handle(frame)["status"] == "ok"
    assert s.handle({**frame, "token": "faux"}) == {"n": 1, "status": "denied", "content": "jeton invalide"}
    now[0] = 111.0
    assert s.handle(frame) == {"n": 1, "status": "denied", "content": "jeton expiré"}
    now[0] = 100.0
    s.closed = True
    assert s.handle(frame)["status"] == "denied"
    assert hits == ["read_file"]


def test_truncation_is_deterministic():
    text = "".join(f"ligne {i:05d} " + "x" * 40 + "\n" for i in range(2000))
    a, b = clip(text, 1000), clip(text, 1000)
    assert a == b and len(a) < 1100
    assert a.startswith("ligne 00000") and a.rstrip().endswith("x" * 40)
    head, rest = a.split("\n[… ", 1)
    n = int(rest.split(" caractères coupés …]\n", 1)[0])
    tail = rest.split(" caractères coupés …]\n", 1)[1]
    assert len(head) + n + len(tail) == len(text) - 1  # rien d'inventé : tête + coupé + queue = le texte
    code = "for i in range(3000):\n    print('ligne', i, 'x' * 30)\n"
    b1, _, _, _ = run_code(code, stdout_chars=2000)
    b2, _, _, _ = run_code(code, stdout_chars=2000)
    assert b1 == b2 and "caractères coupés" in b1 and "ligne 0 " in b1 and "ligne 2999 " in b1


def test_secret_never_in_output():
    code = ("r = call_tool('leaky_read')\nprint('outil :', r)\n"
            f"print('en dur : {SECRET}')\n"
            f"raise ValueError('valeur {SECRET}')\n")
    block, _, _, turn = run_code(code)
    assert SECRET not in block
    assert "[secret:CLE_API]" in block
    assert all(SECRET not in (m.content or "") for m in turn.messages)


def test_return_value_is_canonical_json_and_errors_point_at_the_line():
    block, _, _, _ = run_code("x = {'b': 2, 'a': {3, 1, 2}}\nx")
    assert 'return:\n{"a": [1, 2, 3], "b": 2}\n' in block
    block, _, _, _ = run_code("x = 1\ny = x / 0\n")
    assert block.endswith("error:\nTraceback (dernier appel en dernier) :\n  ligne 2, dans <module>\n"
                          "    y = x / 0\nZeroDivisionError: division by zero\n[/execute_code]")
    block, _, _, _ = run_code("def (:\n")
    assert "SyntaxError" in block and "stdout:\n(vide)" in block


def test_timeout_keeps_what_was_printed():
    block, _, _, _ = run_code("import time\nprint('avant')\ntime.sleep(30)\n", timeout_s=1)
    assert "stdout:\navant\n" in block and "TimeoutError: délai dépassé (1 s), script arrêté" in block


def test_child_processes_cannot_write_on_the_channel():
    code = "import os\nos.system('echo depuis-un-enfant')\nos.system('echo {\\\"t\\\": \\\"done\\\"} >&2')\n'ok'\n"
    block, _, _, _ = run_code(code)
    assert "depuis-un-enfant" in block and 'return:\n"ok"' in block


def test_no_sandbox_and_local_backend_are_refused(tmp_path):
    k, _, _ = make([])
    src = ExecuteCodeSource(resolve=lambda ctx: None)
    ctx = ToolContext(thread_id="t", turn_index=0, channel="api", kernel=k)
    r = src.call(TOOL, {"code": "1"}, ctx)
    assert r.is_error and "pas de session de code sur ce fil" in r.content and r.content.startswith("[execute_code]")
    from nokker.tools.code import CodeToolSource

    local = CodeToolSource(root=str(tmp_path), backend="local")
    r = ExecuteCodeSource(resolve=lambda ctx: local).call(TOOL, {"code": "1"}, ctx)
    assert "REFUSÉ : execute_code tourne dans un bac à sable" in r.content
    r = src.call(TOOL, {"code": "1"}, ToolContext(thread_id="t", turn_index=0, channel="api"))
    assert "n'est appelable que dans un tour du noyau" in r.content


def test_format_block_rule_all_sections_always_present():
    assert format_block("", "", "", (10, 10, 10)) == \
        "[execute_code]\nstdout:\n(vide)\nreturn:\n(vide)\nerror:\n(vide)\n[/execute_code]"


def test_spec_is_not_core_and_text_is_fixed():
    (spec,) = ExecuteCodeSource(resolve=lambda c: None).specs()
    assert spec.name == TOOL and not spec.core and spec.effect is Effect.WRITE
    assert spec.description == ("Exécute un script Python dans le bac à sable du fil ; le script peut appeler "
                                "plusieurs outils en un seul appel et rend sa sortie, sa dernière valeur et son erreur.")


@pytest.mark.parametrize("backend", ["docker", "kubernetes"])
def test_code_script_command(tmp_path, backend):
    """argv réel : docker exec -i -w <dossier> <container> … ; kubectl exec -i <pod> -- sh -c 'cd …' …"""
    from nokker.tools.code import CodeToolSource, Workspace

    class Env:
        cwd = "/workspace/sub"
        _container_id = "c0ffee"
        _docker_exe = "docker"
        pod = "nokker-sbx-1"
        started = 0

        class kubectl:
            @staticmethod
            def exec_argv(pod, argv, stdin):
                return ["kubectl", "exec", *(["-i"] if stdin else []), pod, "--", *argv]

        def _before_execute(self):
            Env.started += 1

    kw = {"env_factory": lambda **k: None} if backend == "kubernetes" else {}
    src = CodeToolSource(root=str(tmp_path), backend=backend, **kw)
    src._spaces["th_1"] = Workspace(env=Env(), ops=None, todos=None, cwd="/workspace")
    argv = src.script_command("th_1/sa-ab", ["python3", "-c", "x"])  # sous-agent : bac à sable du parent
    if backend == "docker":
        assert argv == ["docker", "exec", "-i", "-w", "/workspace/sub", "c0ffee", "python3", "-c", "x"]
    else:
        assert argv == ["kubectl", "exec", "-i", "nokker-sbx-1", "--", "sh", "-c", 'cd "$1" && shift && exec "$@"',
                        "sh", "/workspace/sub", "python3", "-c", "x"] and Env.started == 1
