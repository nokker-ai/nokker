"""Branchement `Kernel.approvals` (core/loop.py, optionnel — docs/APPROVALS.md §2). Sans port, le
comportement est EXACTEMENT celui d'avant (`tests/unit/test_loop_contract.py` le prouve déjà en ne
passant jamais `approvals=`) ; avec, `Decision.HITL` pose une VRAIE carte via `Approvals.request()`."""
from __future__ import annotations

import hashlib

from nokker.approvals.memory import InMemoryApprovals
from nokker.core.loop import Kernel
from nokker.core.ports import Assembled, Completion, ToolSpec
from nokker.core.turn import Decision, Effect, Gate, Message, Tier, ToolCall, ToolResult, TurnInput, Usage, Verdict

PREFIX = "Tu es Atlas."


class ScriptedProvider:
    def __init__(self, script, model="scripted"):
        self.script, self.model, self.seen = list(script), model, []

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.seen.append(messages)
        return Completion(message=self.script.pop(0), usage=Usage(prompt=100), model=self.model, latency_s=0.01)


class Ctx:
    def assemble(self, turn, history, recall, loaded, tier):
        msgs = [{"role": "system", "content": PREFIX}, {"role": "user", "content": turn.input.text}]
        msgs += [m.to_openai() for m in turn.messages]
        return Assembled(messages=msgs, tools=[], prefix_sha256=hashlib.sha256(PREFIX.encode()).hexdigest())

    def admit_result(self, r):
        return r


class Mem:
    def recall(self, q, k=5):
        return []

    def propose(self, t, f):
        pass


class Inv:
    def check_input(self, text):
        from nokker.core.turn import InputCheck

        return InputCheck(text=text)

    def check_action(self, call, spec, turn):
        eff = spec.effect if spec else Effect.WRITE
        if eff is Effect.DESTRUCTIVE:
            return Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:destructive",
                          reason="sans GO", strength="strong")
        return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule=f"effect:{eff.value}")

    def redact_result(self, result, spec):
        return result

    def scrub_output(self, text):
        return text


class Gates:
    def __init__(self, inv):
        self.inv = inv

    def action(self, call, spec, turn):
        return [self.inv.check_action(call, spec, turn)]

    def switch(self, reply, turn, tier):
        return [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=1, rule="ok")]


class Esc:
    def escalate(self, turn, reason):
        return f"escaladé: {reason}"


class Pol:
    def choose(self, state):
        from nokker.core.ports import RouteDecision

        return RouteDecision(tier=Tier.SHIP, reasoning=False, reason="défaut")


class Tools:
    def __init__(self):
        self.called = []
        self.specs = {"infra_lvextend": ToolSpec("infra_lvextend", "lvm", {}, Effect.DESTRUCTIVE)}

    def core(self):
        return []

    def spec(self, n):
        return self.specs.get(n)

    def find(self, q, limit=5):
        return []

    def index(self):
        return []

    def call(self, n, a, ctx):
        self.called.append(n)
        return ToolResult(call_id="", name=n, content="fait")


class StoreFake:
    def __init__(self):
        self.turns = []

    def history(self, t, limit=None):
        return [x for x in self.turns if x.thread_id == t]

    def next_index(self, t):
        return len(self.history(t))

    def append(self, turn):
        self.turns.append(turn)


class LedgerFake:
    def record(self, t, i, c):
        pass

    def record_verdict(self, t, i, v):
        pass


def tc(name, **args):
    return ToolCall(id=f"c-{name}", name=name, arguments=args)


def make_kernel(script, approvals=None):
    inv, tools = Inv(), Tools()
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=Gates(inv), escalator=Esc(),
              policy=Pol(), providers={Tier.SHIP: ScriptedProvider(script)}, tools=tools, ledger=LedgerFake(),
              approvals=approvals)
    return k, tools


def test_without_approvals_port_behaviour_is_unchanged():
    k, tools = make_kernel([Message("assistant", tool_calls=[tc("infra_lvextend")]), Message("assistant", "fait")])
    turn = k.run_turn(TurnInput("t1", "le disque est plein"))
    assert tools.called == []
    assert [p.name for p in turn.pending_approvals] == ["infra_lvextend"]
    assert "approval:" not in turn.reply


def test_with_approvals_port_a_real_card_is_requested():
    backend = InMemoryApprovals()
    k, tools = make_kernel([Message("assistant", tool_calls=[tc("infra_lvextend", path="/srv/share", size="50G")]),
                           Message("assistant", "fait")], approvals=backend)
    turn = k.run_turn(TurnInput("t1", "le disque est plein sur hote21", sender="alice@example.ch"))
    assert tools.called == []  # jamais exécuté sans décision
    pending = backend.pending()
    assert len(pending) == 1
    assert pending[0].tool_name == "infra_lvextend"
    assert pending[0].strength == "strong"
    assert pending[0].thread_id == "t1"
    tool_messages = " ".join(m.content for m in turn.messages if m.role == "tool")
    assert f"approval:{pending[0].id}" in tool_messages


def test_approvals_request_receives_the_verdict_rule_and_reason():
    backend = InMemoryApprovals()
    k, tools = make_kernel([Message("assistant", tool_calls=[tc("infra_lvextend")]), Message("assistant", "fait")],
                           approvals=backend)
    k.run_turn(TurnInput("t1", "x"))
    approval = backend.pending()[0]
    assert approval.rule == "effect:destructive"
    assert approval.reason == "sans GO"
