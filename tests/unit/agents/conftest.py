"""Un monde Crew complet pour les tests du moteur (lot X14) : le VRAI noyau (`Kernel`) avec un faux modèle scripté,
des portes de test (lecture permise, écriture → carte, destructif → carte forte, GO écrit simulé), les vraies
approbations en mémoire, le vrai hub des fils, un coffre en mémoire, et l'exécuteur d'approbation câblé comme
`make_handler` le fait. Aucun appel réseau."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from nokker.agents.engine import ARM_TOOL, AgentEngine
from nokker.agents.hub import AgentsHub
from nokker.agents.memory import InMemoryAgents
from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.context.engine import ZURICH, ContextEngineV0
from nokker.core.loop import Kernel
from nokker.core.ports import Completion, ToolSpec, call_effect
from nokker.core.turn import Decision, Effect, Gate, Message, Tier, ToolCall, ToolResult, Usage, Verdict
from nokker.ledger.costs import InMemoryLedger
from nokker.secrets.base import SecretsError
from nokker.secrets.refs import SecretResolver
from nokker.store.pg import MemoryStore
from nokker.threads.memory import InMemoryThreads
from tests.unit.api.test_server import NoEscalator, NullMem, ShipOnlyPolicy

MODEL = "Qwen3.5-397B-A17B"  # au catalogue, en EUR (0,60 € / 3,60 € par million)
SECRET_VALUE = "valeur-tres-secrete-0042"


class Clock:
    def __init__(self, t: float = 1_793_000_000.0):  # 2026-10-26
        self.t = t

    def __call__(self) -> float:
        return self.t


class Recorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event_type, data):
        self.events.append((event_type, data))
        return None

    def of(self, event_type):
        return [d for t, d in self.events if t == event_type]


class FakeProvider:
    """Coffre en mémoire (portée `user`)."""

    def __init__(self, values: dict[str, str] | None = None):
        self.values = dict(values or {})

    def get(self, name):
        return self.values.get(name)


class Script:
    """Faux modèle : rejoue `steps` dans l'ordre, un pas par appel modèle — ("tool", nom, arguments) ou ("text", t).
    Au-delà du script : « fini »."""

    def __init__(self, steps=()):
        self.steps = list(steps)
        self.seen: list[list[dict]] = []
        self.calls = 0

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.seen.append(messages)
        self.calls += 1
        step = self.steps.pop(0) if self.steps else ("text", "fini")
        usage = Usage(prompt=1000, completion=100)
        if step[0] == "tool":
            tc = ToolCall(f"c{self.calls}", step[1], dict(step[2]))
            return Completion(message=Message("assistant", tool_calls=[tc]), usage=usage, model=MODEL, latency_s=0)
        return Completion(message=Message("assistant", content=step[1]), usage=usage, model=MODEL, latency_s=0)


@dataclass
class Tools:
    resolver: Any = None
    calls: list[tuple[str, dict]] = field(default_factory=list)
    specs: dict[str, ToolSpec] = field(default_factory=lambda: {
        "web_search": ToolSpec("web_search", "web", {}, Effect.READ, core=True),
        "note_write": ToolSpec("note_write", "écrit une note", {}, Effect.WRITE, core=True),
        "wipe": ToolSpec("wipe", "efface tout", {}, Effect.DESTRUCTIVE, core=True),
        "use_secret": ToolSpec("use_secret", "appelle un service avec un secret du coffre", {}, Effect.READ,
                               core=True),
    })

    def core(self):
        return list(self.specs.values())

    def spec(self, n):
        return self.specs.get(n)

    def find(self, q, limit=5):
        return []

    def index(self):
        return []

    def call(self, n, a, ctx):
        self.calls.append((n, dict(a)))
        if n == "use_secret":
            try:
                self.resolver.value(str(a.get("name")))
            except SecretsError as e:
                return ToolResult("", n, f"ÉCHEC : {e}", is_error=True)
            return ToolResult("", n, "service appelé")
        return ToolResult("", n, f"{n} fait")


class TestGates:
    """Lecture permise ; écriture → carte ; destructif → carte forte, sauf « GO » écrit (simule l'invariant 4)."""

    __test__ = False

    def action(self, call, spec, turn):
        eff = call_effect(spec, call.arguments)
        if eff is Effect.READ:
            return [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="effect:read")]
        if eff is Effect.DESTRUCTIVE and "GO" in turn.input.text:
            return [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="effect:destructive:go")]
        return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=f"effect:{eff.value}",
                        strength="strong" if eff is Effect.DESTRUCTIVE else "standard")]

    def switch(self, reply, turn, tier):
        return []


class ScrubInvariants:
    def check_input(self, text):
        from nokker.core.turn import InputCheck

        return InputCheck(text=text)

    def check_action(self, call, spec, turn):
        return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="t")

    def redact_result(self, result, spec):
        result.content = self.scrub_output(result.content)
        return result

    def scrub_output(self, text):
        return (text or "").replace(SECRET_VALUE, "[secret:API_TOKEN]")


@dataclass
class World:
    engine: AgentEngine
    agents: AgentsHub
    kernel: Kernel
    bubbles: BubbleHub
    approvals: InMemoryApprovals
    executor: ApprovalExecutor
    model: Script
    tools: Tools
    rec: Recorder
    clock: Clock
    vault: FakeProvider
    submitted: list = field(default_factory=list)

    def runs(self, agent_id):
        return self.agents.store.runs(agent_id, limit=100)[0]

    def run(self, run_id):
        return self.agents.store.get_run(run_id)

    def decide(self, approval_id, decision="approve", by="alice"):
        decided = self.approvals.decide_local(approval_id, decision, by)
        self.executor.run(decided)
        return self.approvals.get(approval_id)


def make_world(tmp_path, steps=(), *, secrets=None, sync=True) -> World:
    clock = Clock()
    rec = Recorder()
    vault = FakeProvider(secrets)
    resolver = SecretResolver(vault)
    tools = Tools(resolver=resolver)
    bubbles = BubbleHub(threads=InMemoryThreads(clock=clock), bubbles=InMemoryBubbles(clock=clock), publish=rec,
                        known_users=frozenset({"alice", "bruno"}), spawn=lambda fn: fn(), clock=clock)
    prefix = tmp_path / "prefix.md"
    prefix.write_text("PRÉFIXE", encoding="utf-8")
    ctx = ContextEngineV0(tools=tools, prefix_path=str(prefix), now_fn=lambda: datetime(2026, 10, 26, tzinfo=ZURICH))
    approvals = InMemoryApprovals(is_authorized=lambda u: u in ("alice", "bruno"), clock=clock)
    model = Script(steps)
    kernel = Kernel(store=MemoryStore(), context=ctx, memory=NullMem(), invariants=ScrubInvariants(),
                    gates=TestGates(), escalator=NoEscalator(), policy=ShipOnlyPolicy(), providers={Tier.SHIP: model},
                    tools=tools, ledger=InMemoryLedger(), approvals=approvals, observer=bubbles, max_calls=12)
    bubbles.kernel = kernel
    agents = AgentsHub(store=InMemoryAgents(clock=clock), publish=rec, clock=clock)
    submitted: list = []

    def submit(fn, *args):
        submitted.append((fn, args))
        if sync:
            fn(*args)

    engine = AgentEngine(hub=agents, kernel=kernel, bubbles=bubbles, approvals=approvals,
                         resolver=lambda: resolver, clock=clock, submit=submit)
    executor = ApprovalExecutor(kernel=kernel, approvals=approvals, handlers={ARM_TOOL: engine.on_arm_decision},
                                kernel_for=engine.kernel_for)
    return World(engine=engine, agents=agents, kernel=kernel, bubbles=bubbles, approvals=approvals,
                 executor=executor, model=model, tools=tools, rec=rec, clock=clock, vault=vault, submitted=submitted)


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)

