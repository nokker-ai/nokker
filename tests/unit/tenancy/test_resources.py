"""Ressources par locataire (docs/TENANCY.md §5) : outils, mémoire longue, préfixe, escalade et plafonds, câblés par
`build_kernel` comme en prod (MCP partagés et sokkan-memory remplacés par des faux qui comptent leurs appels)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from nokker.api import server
from nokker.api.server import build_kernel
from nokker.config import PRODUCT_PREFIX_PATH, NokkerConfig
from nokker.core.ports import Completion, Recall, RouteDecision, RouteState, ToolContext, ToolSpec
from nokker.core.turn import Effect, InputCheck, Message, Tier, ToolCall, ToolResult, Turn, TurnInput, Usage
from nokker.tenancy.context import INTERNAL_TENANT, NoTenant, scoped_thread_id, tenant_scope
from nokker.tenancy.limits import ZURICH, LimitedPolicy, MemoryUsage, TenantLimits
from nokker.tenancy.resources import NullMemory
from nokker.tools.registry import StaticToolSource

TESTER = "tn_aaaaaaaaaaaa"
OTHER = "tn_bbbbbbbbbbbb"
FORBIDDEN = {"mail_search", "calendar_list", "browser_navigate", "memory_search", "memory_get"}


class FakeMcp:
    """Remplace `McpToolSource` : `workspace` (mail, agenda de Alice) et `browser` (navigateur partagé de Alice)."""

    calls: list[str] = []

    def __init__(self, name, url, headers=None, close_on_turn_end=None):
        self.name = name
        tools = {"workspace": ["mail_search", "calendar_list"], "browser": ["browser_navigate"]}[name]
        self._specs = [ToolSpec(name=t, description=f"{t} ({name})", parameters={"type": "object"},
                                effect=Effect.READ, source=name) for t in tools]

    def specs(self):
        return self._specs

    def call(self, name, arguments, ctx):
        FakeMcp.calls.append(name)
        return ToolResult(call_id="", name=name, content=f"DONNÉE DE ALICE via {name}")


class FakeCorthexis:
    """Même double rôle que `CorthexisMemory` : MemoryPort (rappel) + ToolSource (memory_search/get, cœurs)."""

    def __init__(self):
        self.recalls, self.calls, self.proposed = 0, [], []

    def recall(self, query, k=5):
        self.recalls += 1
        return [Recall(note="note-de-alice", text="NOTE PRIVÉE DE ALICE", score=0.9)]

    def propose(self, thread_id, facts):
        self.proposed.append(facts)

    def specs(self):
        return [ToolSpec(name=n, description=n, parameters={"type": "object"}, effect=Effect.READ, core=True,
                         source="corthexis") for n in ("memory_search", "memory_get")]

    def call(self, name, arguments, ctx):
        self.calls.append(name)
        return ToolResult(call_id="", name=name, content="NOTE PRIVÉE DE ALICE")


class Scripted:
    """Provider scripté : rejoue `script` (messages assistant), garde ce qu'il a vu."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.seen: list[tuple[list[dict], list[dict]]] = []

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.seen.append((messages, tools))
        msg = self.script.pop(0) if self.script else Message(role="assistant", content="D'accord.")
        return Completion(message=msg, usage=Usage(prompt=10), model="scripted", latency_s=0.0)


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.delenv("NOKKER_PG_DSN", raising=False)
    monkeypatch.setattr(server, "McpToolSource", FakeMcp)
    FakeMcp.calls = []
    prefix = tmp_path / "prefix-interne.md"
    prefix.write_text("# Atlas\n\nTu es Atlas, l'agente de Alice et de Bruno.\n", encoding="utf-8")

    def make(**over):
        cfg = NokkerConfig(pg_dsn_env="TEST_NOKKER_ABSENT_DSN_VAR", web_search_enabled=False, approvals_enabled=False,
                           context_enabled=True, prefix_path=str(prefix), escalation_addressee="Alice",
                           mcp_tool_servers=[{"name": "workspace", "url": "http://ws.invalid/mcp"},
                                             {"name": "browser", "url": "http://br.invalid/mcp"}])
        for k, v in over.items():
            setattr(cfg, k, v)
        memory = FakeCorthexis()
        kernel = build_kernel(cfg, memory=memory)
        provider = Scripted()
        kernel.providers = {Tier.SHIP: provider, Tier.DEEP: provider}
        return kernel, memory, provider

    return make


def _names(kernel) -> set[str]:
    return {s.name for s in kernel.tools.core()} | {n for n, _ in kernel.tools.index()} | \
        {s.name for s in kernel.tools.find("", limit=10_000)}


def _turn(kernel, tenant, text="bonjour", sender="us_aaaaaaaaaaaa", thread="t1"):
    with tenant_scope(tenant):
        return kernel.run_turn(TurnInput(thread_id=scoped_thread_id(tenant, thread), user_text=text, channel="api",
                                         sender=sender))


# ─────────────────────────────── outils ───────────────────────────────

def test_tester_sees_only_core_session_memory_and_no_alice_tools(rig):
    kernel, _, _ = rig()
    with tenant_scope(TESTER):
        names = _names(kernel)
        core = {s.name for s in kernel.tools.core()}
    assert not names & FORBIDDEN
    assert {"clarify", "tool_find", "escalate_to_human", "recall_context"} <= core


def test_tester_direct_call_by_name_is_unknown_and_never_executed(rig):
    kernel, memory, _ = rig()
    ctx = ToolContext(thread_id=f"{TESTER}:t1", turn_index=0, channel="api", sender="us_aaaaaaaaaaaa")
    with tenant_scope(TESTER):
        for name in sorted(FORBIDDEN):
            assert kernel.tools.spec(name) is None
            res = kernel.tools.call(name, {"query": "x"}, ctx)
            assert res.is_error and "outil inconnu" in res.content
    assert FakeMcp.calls == [] and memory.calls == []


def test_tester_model_calling_alice_tools_executes_nothing(rig):
    kernel, memory, provider = rig()
    provider.script = [
        Message(role="assistant", content="", tool_calls=[
            ToolCall(id="c1", name="memory_search", arguments={"query": "alice"}),
            ToolCall(id="c2", name="browser_navigate", arguments={"url": "https://mail.google.com"}),
            ToolCall(id="c3", name="mail_search", arguments={"query": "facture"})]),
        Message(role="assistant", content="Je n'ai pas ces outils."),
    ]
    turn = _turn(kernel, TESTER)
    assert FakeMcp.calls == [] and memory.calls == []
    results = [m.content for m in turn.messages if m.role == "tool"]
    assert results == ["outil inconnu : memory_search", "outil inconnu : browser_navigate",
                       "outil inconnu : mail_search"]
    for _, tools in provider.seen:
        assert not {t["function"]["name"] for t in tools} & FORBIDDEN


def test_internal_registry_is_the_previous_one(rig):
    kernel, memory, _ = rig()
    with tenant_scope(INTERNAL_TENANT):
        assert kernel.tools.current() is kernel.tools.internal
        names = _names(kernel)
        ctx = ToolContext(thread_id="t1", turn_index=0, channel="api", sender="alice")
        assert "DONNÉE DE ALICE" in kernel.tools.call("browser_navigate", {}, ctx).content
    assert FORBIDDEN <= names
    order = [type(s).__name__ for s in kernel.tools.internal.sources]
    assert order == ["StaticToolSource", "RecallContextSource", "FakeCorthexis", "SecretScrubbingToolSource",
                     "SecretScrubbingToolSource"]


def test_connectors_belong_to_their_tenant_only(rig, monkeypatch):
    mail = ToolSpec(name="mail_search", description="courrier du testeur", parameters={"type": "object"},
                    effect=Effect.READ)
    source = StaticToolSource(entries=[(mail, lambda a, c: ToolResult(call_id="", name="mail_search", content="A"))])

    class Provider:
        asked: list[str] = []

        def sources_for(self, tenant_id):
            Provider.asked.append(tenant_id)
            return [source] if tenant_id == TESTER else []

    kernel, _, _ = rig()
    kernel.tools.connectors = Provider()
    with tenant_scope(TESTER):
        assert kernel.tools.spec("mail_search") is mail
        kernel.tools.core()
    with tenant_scope(OTHER):
        assert kernel.tools.spec("mail_search") is None
    assert Provider.asked == [TESTER, OTHER]  # un Registry en cache par locataire
    kernel.tools.forget(TESTER)
    with tenant_scope(TESTER):
        kernel.tools.turn_done(f"{TESTER}:t1")
    assert Provider.asked == [TESTER, OTHER, TESTER]
    kernel.tools.refresh()
    with tenant_scope(TESTER):
        kernel.tools.index()
    assert Provider.asked[-1] == TESTER and len(Provider.asked) == 4


# ─────────────────────────────── mémoire, préfixe, escalade ───────────────────────────────

def test_tester_gets_product_prefix_and_no_recall(rig):
    kernel, memory, provider = rig()
    turn = _turn(kernel, TESTER, "qu'est-ce que tu sais de moi ?")
    system = provider.seen[0][0][0]["content"]
    assert system.startswith("# Nokker")
    for word in ("Atlas", "Alice", "Bruno", "ninabot"):
        assert word not in system
    assert memory.recalls == 0
    assert "[Rappel mémoire]" not in provider.seen[0][0][-1]["content"]
    assert "memory:recall_used" not in [v.rule for v in turn.verdicts]
    with tenant_scope(TESTER):
        kernel.memory.propose(f"{TESTER}:t1", ["fait"])
        assert kernel.memory.recall("x") == []
    assert memory.proposed == []


def test_internal_keeps_internal_prefix_and_recall(rig):
    kernel, memory, provider = rig()
    turn = _turn(kernel, INTERNAL_TENANT, "où en est le disque ?", sender="alice")
    assert provider.seen[0][0][0]["content"].startswith("# Atlas")
    assert memory.recalls == 1 and "[Rappel mémoire]" in provider.seen[0][0][-1]["content"]
    assert "memory:recall_used" in [v.rule for v in turn.verdicts]


def test_prefix_hash_is_per_tenant_and_stable_across_turns(rig):
    kernel, _, _ = rig()
    a1 = _turn(kernel, TESTER, "un").calls[0].prefix_sha256
    a2 = _turn(kernel, TESTER, "deux").calls[0].prefix_sha256
    b = _turn(kernel, OTHER, "un", sender="us_bbbbbbbbbbbb").calls[0].prefix_sha256
    i = _turn(kernel, INTERNAL_TENANT, "un", sender="alice").calls[0].prefix_sha256
    assert a1 == a2 == b and a1 != i


def test_tester_escalation_names_nobody_internal(rig):
    kernel, _, _ = rig()
    t = Turn(thread_id=f"{TESTER}:t1", index=0, input=InputCheck(text="x"))
    with tenant_scope(TESTER):
        text = kernel.escalator.escalate(t, "question fiscale")
    assert "Alice" not in text and "question fiscale" in text
    with tenant_scope(INTERNAL_TENANT):
        assert "Alice" in kernel.escalator.escalate(t, "question fiscale")


def test_packaged_product_prefix():
    text = PRODUCT_PREFIX_PATH.read_text(encoding="utf-8")
    assert 4000 <= len(text) <= 6500
    for word in ("Atlas", "Alice", "Bruno", "ninabot", "hote21", "hote", "Vaultwarden", "memory_search"):
        assert word.lower() not in text.lower()
    for tool in ("tool_find", "recall_context", "clarify", "escalate_to_human"):
        assert f"`{tool}(" in text
    assert text.startswith("# Nokker\n") and "## Invariants" in text and "## Outils cœur" in text


def test_empty_memory_url_means_no_long_memory_for_anyone(monkeypatch):
    monkeypatch.delenv("NOKKER_PG_DSN", raising=False)

    def boom(*a, **k):
        raise AssertionError("aucune connexion à sokkan-memory avec memory_mcp_url vide")

    monkeypatch.setattr(server, "CorthexisMemory", boom)
    cfg = NokkerConfig(pg_dsn_env="TEST_NOKKER_ABSENT_DSN_VAR", memory_mcp_url="", mcp_tool_servers=[],
                       web_search_enabled=False, approvals_enabled=False)
    kernel = build_kernel(cfg)
    assert isinstance(kernel.memory.internal, NullMemory)
    for tenant in (INTERNAL_TENANT, TESTER):
        with tenant_scope(tenant):
            assert not _names(kernel) & {"memory_search", "memory_get"}
            assert kernel.memory.recall("x") == []


# ─────────────────────────────── hors scope ───────────────────────────────

def test_out_of_scope_everything_raises_no_tenant(rig):
    kernel, memory, provider = rig()
    with pytest.raises(NoTenant):
        kernel.tools.core()
    with pytest.raises(NoTenant):
        kernel.tools.call("clarify", {"question": "?"}, ToolContext(thread_id="t", turn_index=0, channel="api"))
    with pytest.raises(NoTenant):
        kernel.memory.recall("x")
    with pytest.raises(NoTenant):
        kernel.memory.propose("t", ["f"])
    with pytest.raises(NoTenant):
        kernel.escalator.escalate(Turn(thread_id="t", index=0, input=InputCheck(text="x")), "r")
    with pytest.raises(NoTenant):
        kernel.run_turn(TurnInput(thread_id="t", user_text="bonjour", channel="api", sender="alice"))
    assert provider.seen == [] and memory.recalls == 0


# ─────────────────────────────── plafonds ───────────────────────────────

def test_turn_limit_answers_without_model_and_keeps_history(rig):
    kernel, memory, provider = rig(tenant_limits={"mode": "hard", "turns_per_day": 2, "deep_per_day": 1})
    for text in ("un", "deux"):
        assert _turn(kernel, TESTER, text).reply == "D'accord."
    calls = len(provider.seen)
    over = _turn(kernel, TESTER, "trois")
    assert len(provider.seen) == calls and over.calls == []
    assert over.reply.startswith("Limite du jour atteinte") and "Daily limit reached" in over.reply
    assert [v.rule for v in over.verdicts] == ["limits:turns_per_day"]
    hist = kernel.store.history(f"{TESTER}:t1")
    assert [t.reply for t in hist][-1] == over.reply and len(hist) == 3
    # Par compte : un autre compte du même locataire a son propre compteur ; l'interne n'a aucun plafond.
    assert _turn(kernel, TESTER, "un", sender="us_cccccccccccc").reply == "D'accord."
    kernel.limits.turns_per_day = 0
    assert _turn(kernel, INTERNAL_TENANT, "un", sender="alice").reply == "D'accord."


def test_turn_limit_resets_at_noon_utc():
    now = [datetime(2026, 10, 28, 12, 50, tzinfo=ZURICH)]  # 11:50 UTC (heure d'hiver)
    limits = TenantLimits(counter=MemoryUsage(), mode="hard", turns_per_day=1, clock=lambda: now[0])
    with tenant_scope(TESTER):
        assert limits.check_turn("us_a") is None
        assert limits.check_turn("us_a").rule == "limits:turns_per_day"
        now[0] += timedelta(minutes=9)  # 11:59 UTC : toujours le même jour de quota
        assert limits.check_turn("us_a") is not None
        now[0] += timedelta(minutes=1)  # 12:00 UTC = 13:00 à Zurich : nouveau jour
        assert limits.check_turn("us_a") is None


def test_deep_over_limit_stays_on_ship():
    class AlwaysDeep:
        def choose(self, state):
            return RouteDecision(tier=Tier.DEEP, reasoning=True, reason="doute")

    policy = LimitedPolicy(inner=AlwaysDeep(), limits=TenantLimits(counter=MemoryUsage(), deep_per_day=1))
    state = RouteState(turn=Turn(thread_id="t", index=0, input=InputCheck(text="x"), sender="us_a"), call_index=0)
    with tenant_scope(TESTER):
        assert policy.choose(state).tier is Tier.DEEP
        second = policy.choose(state)
        assert second.tier is Tier.SHIP and second.reason.startswith("limits:deep_per_day")
    with tenant_scope(INTERNAL_TENANT):
        assert all(policy.choose(state).tier is Tier.DEEP for _ in range(5))
