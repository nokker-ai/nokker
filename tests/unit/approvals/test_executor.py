"""`ApprovalExecutor` — exécution après décision + tour de continuation (docs/APPROVALS.md §2, dernier
item). Un `Kernel` complet, mêmes fakes qu'ailleurs (`tests/unit/test_loop_contract.py`)."""
from __future__ import annotations

import hashlib

from nokker.approvals import crypto as approvals_crypto
from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.loop import Kernel
from nokker.core.ports import Approval, Assembled, Completion, ToolSpec
from nokker.core.turn import Decision, Effect, Gate, Message, Tier, ToolResult, Usage, Verdict

PREFIX = "Tu es Atlas."


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
        return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="allow")

    def redact_result(self, result, spec):
        return result

    def scrub_output(self, text):
        return text


class GatesAllow:
    def action(self, call, spec, turn):
        return [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="allow")]

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
    def __init__(self, result="Volume agrandi à 150G.", is_error=False):
        self.called = []
        self.result, self.is_error = result, is_error
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
        self.called.append((n, dict(a)))
        return ToolResult(call_id="", name=n, content=self.result, is_error=self.is_error)


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


def make_kernel(reply="Volume agrandi, confirmé.", tools=None):
    tools = tools or Tools()
    ship = type("Ship", (), {"complete": staticmethod(
        lambda messages, tools, *, reasoning, session_key, max_tokens=2048:
        Completion(message=Message(role="assistant", content=reply), usage=Usage(prompt=5), model="t",
                  latency_s=0.0))})()
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=Inv(), gates=GatesAllow(),
              escalator=Esc(), policy=Pol(), providers={Tier.SHIP: ship}, tools=tools, ledger=LedgerFake())
    return k, tools


def test_approved_execution_calls_the_tool_and_continues_the_turn():
    backend = InMemoryApprovals()
    kernel, tools = make_kernel()
    executor = ApprovalExecutor(kernel=kernel, approvals=backend)

    approval = Approval(id="a1", thread_id="fil-1", turn_index=0, channel="telegram", tool_name="infra_lvextend",
                        arguments={"path": "/srv/share", "size": "50G"},
                        args_sha256=approvals_crypto.args_sha256({"path": "/srv/share", "size": "50G"}),
                        strength="strong", rule="effect:destructive", reason="sans GO", status="approved",
                        decided_by="alice", decided_via="signature")
    backend._store_approval(approval)
    turn = executor.run(approval)

    assert tools.called == [("infra_lvextend", {"path": "/srv/share", "size": "50G"})]
    assert "[Approbation]" in turn.input.text and "[/Approbation]" in turn.input.text
    assert "infra_lvextend" in turn.input.text
    assert "alice" in turn.input.text
    assert backend.get("a1").status == "executed"


def test_rejected_approval_never_calls_the_tool():
    backend = InMemoryApprovals()
    kernel, tools = make_kernel()
    executor = ApprovalExecutor(kernel=kernel, approvals=backend)
    approval = Approval(id="a2", thread_id="fil-2", turn_index=0, channel="telegram", tool_name="infra_lvextend",
                        arguments={"x": 1}, args_sha256="deadbeef", strength="strong", rule="effect:destructive",
                        status="rejected", decided_by="alice", decided_via="signature")
    turn = executor.run(approval)
    assert tools.called == []
    assert "refusé par alice" in turn.input.text


def test_tampered_arguments_block_execution_even_if_approved():
    backend = InMemoryApprovals()
    kernel, tools = make_kernel()
    executor = ApprovalExecutor(kernel=kernel, approvals=backend)
    approval = Approval(id="a3", thread_id="fil-3", turn_index=0, channel="telegram", tool_name="infra_lvextend",
                        arguments={"size": "500G"}, args_sha256="pas-la-vraie-empreinte", strength="strong",
                        rule="effect:destructive", status="approved", decided_by="alice", decided_via="signature")
    backend._store_approval(approval)
    turn = executor.run(approval)
    assert tools.called == []
    assert "ÉCHEC" in turn.input.text
    assert backend.get("a3").status == "failed"


def test_tool_error_marks_approval_failed():
    backend = InMemoryApprovals()
    kernel, tools = make_kernel(tools=Tools(result="outil indisponible", is_error=True))
    executor = ApprovalExecutor(kernel=kernel, approvals=backend)

    approval = Approval(id="a4", thread_id="fil-4", turn_index=0, channel="telegram", tool_name="infra_lvextend",
                        arguments={"x": 1}, args_sha256=approvals_crypto.args_sha256({"x": 1}), strength="strong",
                        rule="effect:destructive", status="approved", decided_by="alice", decided_via="signature")
    backend._store_approval(approval)
    turn = executor.run(approval)
    assert backend.get("a4").status == "failed"
    assert "ÉCHEC" in turn.input.text
