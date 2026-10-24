"""Banc d'essai du « never clear » : un vrai `Kernel` (boucle, invariants, registre d'outils) sur un modèle scripté,
le `ContextEngineV1` et ses magasins en mémoire. Aucun réseau."""
from __future__ import annotations

from dataclasses import dataclass, field

from nokker.context.engine_v1 import ContextEngineV1
from nokker.context.manager import ContextManager
from nokker.context.tools import RecallContextSource
from nokker.context.types import ContextBudgets
from nokker.core.loop import Kernel
from nokker.core.ports import Completion, ToolContext, ToolSpec
from nokker.core.turn import Effect, Message, Tier, ToolCall, ToolResult, TurnInput, Usage
from nokker.ledger.costs import InMemoryLedger
from nokker.qa.escalate import EscalatorV0
from nokker.qa.gates import GatesV0
from nokker.qa.invariants import DeterministicInvariants
from nokker.route.policy import PolicyV0
from nokker.store.pg import MemoryStore
from nokker.tools.registry import Registry, StaticToolSource, builtin_core_source

BIG_MARK = "[[BIG]]"


class Clock:
    def __init__(self, t0: float = 1_790_000_000.0) -> None:
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class ScriptedProvider:
    """Répond « Réponse n°k » ; si le dernier message utilisateur porte `[[BIG]]` et qu'aucun résultat d'outil ne
    suit, appelle d'abord `big_tool`. Enregistre les messages de chaque appel."""

    def __init__(self) -> None:
        self.seen: list[list[dict]] = []
        self.calls = 0

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.seen.append([dict(m) for m in messages])
        self.calls += 1
        last_user = next(m for m in reversed(messages) if m["role"] == "user")
        if BIG_MARK in last_user["content"] and messages[-1]["role"] != "tool":
            msg = Message(role="assistant", content="", tool_calls=[
                ToolCall(id=f"call{self.calls}", name="big_tool", arguments={}, raw_arguments="{}")])
        else:
            msg = Message(role="assistant", content=f"Réponse n°{self.calls}.")
        return Completion(message=msg, usage=Usage(prompt=100, completion=10), model="scripted", latency_s=0.001)


def big_tool_source(size_chars: int = 8000) -> StaticToolSource:
    spec = ToolSpec(name="big_tool", description="renvoie un gros résultat", parameters={"type": "object",
                    "properties": {}}, effect=Effect.READ, core=False, source="test")

    def fn(_args, _ctx: ToolContext) -> ToolResult:
        line = "ligne de log numéro {:05d} — disque hote21 à 91 pourcent, rien d'anormal\n"
        return ToolResult(call_id="", name="big_tool",
                          content="".join(line.format(i) for i in range(size_chars // 70 + 1))[:size_chars])

    return StaticToolSource(entries=[(spec, fn)])


@dataclass
class Session:
    kernel: Kernel
    manager: ContextManager
    provider: ScriptedProvider
    store: MemoryStore
    clock: Clock
    closed: list = field(default_factory=list)
    thread: str = "th_test"
    registry: Registry | None = None
    prefix_path: str = ""

    def say(self, text: str, thread: str | None = None):
        return self.kernel.run_turn(TurnInput(thread_id=thread or self.thread, user_text=text, sender="alice"))


def make_session(budgets: ContextBudgets | None = None, tmp_path=None, *, tool_size: int = 8000,
                 store: MemoryStore | None = None, thread: str = "th_test", with_recall: bool = True,
                 provider=None, episodes=None, reservoir=None) -> Session:
    budgets = budgets or ContextBudgets(episodes_budget=500, current_budget=1500, compaction_step=500,
                                        shed_min_tokens=200, reservoir_min_tokens=600, reservoir_excerpt_chars=600)
    clock = Clock()
    store = store if store is not None else MemoryStore()
    inv = DeterministicInvariants()
    closed: list = []

    def on_closed(ep):
        closed.append(ep)
        return f"bu_{ep.id}"

    extra = {k: v for k, v in (("episodes", episodes), ("reservoir", reservoir)) if v is not None}
    manager = ContextManager(store=store, budgets=budgets, scrub=inv.scrub_output, clock=clock,
                             on_episode_closed=on_closed, **extra)
    prefix = (tmp_path / "prefix.md") if tmp_path is not None else None
    if prefix is not None:
        prefix.write_text("Tu es Atlas.")
    recall_sources = [RecallContextSource(manager)] if with_recall else []
    registry = Registry(sources=[builtin_core_source(), *recall_sources, big_tool_source(tool_size)])
    engine = ContextEngineV1(tools=registry, prefix_path=str(prefix) if prefix else "/nonexistent",
                             manager=manager)
    provider = provider if provider is not None else ScriptedProvider()
    kernel = Kernel(store=store, context=engine, memory=_NoMemory(), invariants=inv,
                    gates=GatesV0(invariants=inv), escalator=EscalatorV0(), policy=PolicyV0(),
                    providers={Tier.SHIP: provider, Tier.DEEP: provider}, tools=registry, ledger=InMemoryLedger())
    return Session(kernel=kernel, manager=manager, provider=provider, store=store, clock=clock, closed=closed,
                   thread=thread, registry=registry, prefix_path=str(prefix) if prefix else "")


class _NoMemory:
    def recall(self, query, k=5):
        return []

    def propose(self, thread_id, facts):
        pass


def chars_of(messages: list[dict]) -> int:
    return sum(len(m.get("content") or "") for m in messages)
