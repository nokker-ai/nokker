"""Outils offerts selon le FIL (`TenantRegistry.scope_key`, `tools/scope.py`).

Commit 1 (sans drapeau, régression de cf497c9) : dans un fil à session de code VIVANTE, `delegate` est celui des
sessions (worktree, fond, status/cancel), pas celui de conversation ; ailleurs, le delegate de conversation, comme
avant. Les vraies sources `DelegateToolSource` (conversation avec paliers ; sessions en fond, sur une source de code)."""
from __future__ import annotations

from nokker.core.ports import ToolContext
from nokker.core.turn import ToolResult
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.resources import TenantRegistry
from nokker.tools.delegate import CONV_DESCRIPTION, DelegateToolSource
from nokker.tools.registry import Registry
from nokker.tools.scope import thread_scope

TN = "tn_scope000001"
SESSION = f"{TN}:s-0123456789abcdef"
PLAIN = f"{TN}:th_plain"


class _Calls(DelegateToolSource):
    """Note les appels au lieu de lancer des sous-agents."""

    def __init__(self, *a, label: str, **kw):
        super().__init__(*a, **kw)
        self.label, self.calls = label, []

    def call(self, name, arguments, ctx):
        self.calls.append((name, ctx.thread_id))
        return ToolResult(call_id="", name=name, content=self.label)


def _registry():
    conv = _Calls(lambda *a: None, None, tier=True, label="conversation")
    conv.conversation_only = True  # comme serve (delegate_tenants)
    sessions = _Calls(lambda *a: None, object(), background=True, label="sessions")  # source de code présente
    reg = TenantRegistry(internal=Registry(sources=[]), tester_sources=[conv, sessions])
    reg.has_session = lambda thread: thread.split("/sa-", 1)[0] == SESSION
    return reg, conv, sessions


def _ctx(thread):
    return ToolContext(thread_id=thread, turn_index=0, channel="web", sender="us_x")


def test_in_a_session_thread_delegate_is_the_sessions_one_with_status_and_cancel():
    reg, conv, sessions = _registry()
    with tenant_scope(TN):
        for thread in (SESSION, SESSION + "/sa-abc123"):  # le fil d'un sous-agent est celui de sa session
            with thread_scope(thread):
                spec = reg.spec("delegate")
                assert "worktree" in spec.description and spec.description != CONV_DESCRIPTION
                assert {"delegate", "delegate_status", "delegate_cancel"} <= {n for n, _ in reg.index()}
                assert reg.call("delegate", {"tasks": []}, _ctx(thread)).content == "sessions"
    assert conv.calls == [] and [c[0] for c in sessions.calls] == ["delegate", "delegate"]


def test_in_an_ordinary_thread_delegate_is_the_conversation_one():
    reg, conv, sessions = _registry()
    with tenant_scope(TN):
        for thread in (PLAIN, None):  # fil ordinaire, ou hors de tout tour : comme avant cf497c9+
            with thread_scope(thread):
                assert reg.spec("delegate").description == CONV_DESCRIPTION
                assert reg.call("delegate", {"tasks": []}, _ctx(PLAIN)).content == "conversation"
    assert sessions.calls == [] and len(conv.calls) == 2


def test_a_closed_or_unreadable_session_keeps_the_tenant_set():
    reg, *_ = _registry()
    reg.has_session = lambda thread: (_ for _ in ()).throw(RuntimeError("base"))
    with tenant_scope(TN), thread_scope(SESSION):
        assert reg.scope_key() == "all" and reg.spec("delegate").description == CONV_DESCRIPTION


def test_each_scope_has_its_own_cached_registry_and_forget_drops_them_all():
    reg, *_ = _registry()
    with tenant_scope(TN):
        with thread_scope(SESSION):
            a = reg.current()
        with thread_scope(PLAIN):
            b = reg.current()
        with thread_scope(SESSION):
            assert reg.current() is a
        assert a is not b
        reg.forget(TN)
        with thread_scope(SESSION):
            assert reg.current() is not a


# ───── commit 3 : drapeau `code_tools_session_only` — outils des sessions absents hors de leurs fils ─────

class _Code:
    """Source des outils de session (cœur), marquée comme serve la marque."""

    code_session_only = True

    def specs(self):
        from nokker.core.ports import ToolSpec

        return [ToolSpec(name=n, description=n, parameters={}, core=True) for n in ("read_file", "terminal", "todo")]

    def call(self, name, arguments, ctx):
        return ToolResult(call_id="", name=name, content="code")


def _flagged(on):
    reg, conv, sessions = _registry()
    sessions.code_session_only = True  # delegate en fond, status, cancel : outils des sessions
    reg.tester_sources.append(_Code())
    reg.session_only = on
    return reg


def _all_names(reg):
    return {s.name for s in reg.core()} | {n for n, _ in reg.index()}


def test_flag_on_ordinary_thread_has_no_code_session_tool_schema():
    reg = _flagged(True)
    with tenant_scope(TN):
        for thread in (PLAIN, PLAIN + "/sa-abc123", None):  # fil ordinaire, son sous-agent, hors tour
            with thread_scope(thread):
                names = _all_names(reg)
                assert not names & {"read_file", "terminal", "todo", "delegate_status", "delegate_cancel"}, names
                assert reg.spec("delegate").description == CONV_DESCRIPTION  # le delegate de conversation reste
                assert reg.call("terminal", {}, _ctx(PLAIN)).is_error  # schéma absent : outil inconnu


def test_flag_on_session_thread_has_all_its_tools():
    reg = _flagged(True)
    with tenant_scope(TN):
        for thread in (SESSION, SESSION + "/sa-abc123"):
            with thread_scope(thread):
                names = _all_names(reg)
                assert {"read_file", "terminal", "todo", "delegate", "delegate_status", "delegate_cancel"} <= names
                assert "worktree" in reg.spec("delegate").description


def test_flag_off_keeps_the_tenant_set_in_ordinary_threads():
    reg = _flagged(False)
    with tenant_scope(TN), thread_scope(PLAIN):
        assert {"read_file", "terminal", "todo", "delegate_status", "delegate_cancel"} <= _all_names(reg)
