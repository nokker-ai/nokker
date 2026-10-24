"""`Registry` — agrégation de `ToolSource`, aucun réseau."""
from __future__ import annotations

from dataclasses import dataclass, field

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.tools.registry import Registry, StaticToolSource, builtin_core_source


@dataclass
class FakeSource:
    items: list[ToolSpec] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def specs(self):
        return self.items

    def call(self, name, arguments, ctx):
        self.calls.append(name)
        return ToolResult(call_id="", name=name, content="ok")


def spec(name, desc="", core=False, effect=Effect.READ):
    return ToolSpec(name=name, description=desc, parameters={"type": "object", "properties": {}},
                     effect=effect, core=core, source="test")


def test_core_returns_only_core_tools():
    reg = Registry(sources=[FakeSource([spec("a", core=True), spec("b")])])
    assert [s.name for s in reg.core()] == ["a"]


def test_index_excludes_core_tools():
    reg = Registry(sources=[FakeSource([spec("a", "desc a", core=True), spec("b", "desc b")])])
    assert reg.index() == [("b", "desc b")]


def test_find_scores_by_keyword_overlap():
    reg = Registry(sources=[FakeSource([spec("send_mail", "envoie un mail"), spec("calendar_list", "agenda")])])
    found = reg.find("mail")
    assert found and found[0].name == "send_mail"


def test_find_excludes_core_tools():
    reg = Registry(sources=[FakeSource([spec("mail_tool", "mail", core=True)])])
    assert reg.find("mail") == []


def test_call_dispatches_to_owning_source():
    src = FakeSource([spec("x")])
    reg = Registry(sources=[src])
    result = reg.call("x", {}, ToolContext(thread_id="t", turn_index=0, channel="api"))
    assert result.content == "ok" and src.calls == ["x"]


def test_call_unknown_tool_is_error():
    reg = Registry(sources=[FakeSource([])])
    result = reg.call("nope", {}, ToolContext(thread_id="t", turn_index=0, channel="api"))
    assert result.is_error


def test_first_source_wins_on_name_collision():
    reg = Registry(sources=[FakeSource([spec("dup", "premier")]), FakeSource([spec("dup", "second")])])
    assert reg.spec("dup").description == "premier"


def test_refresh_invalidates_cache_and_propagates():
    calls = {"n": 0}

    class Refreshable(FakeSource):
        def refresh(self):
            calls["n"] += 1

    reg = Registry(sources=[Refreshable([spec("a")])])
    reg._all()
    reg.refresh()
    assert calls["n"] == 1 and reg._cache is None


def test_builtin_core_source_has_exactly_tool_find_escalate_clarify():
    # `recall_context` RETIRÉ (décision Alice, P1-S1, 17.10.2026) : tant que le réservoir réel (A-1)
    # n'existe pas, ce n'était qu'un stub qui répond toujours « indisponible » — invitait à réessayer.
    names = {s.name for s in builtin_core_source().specs()}
    assert names == {"tool_find", "escalate_to_human", "clarify"}
    assert all(s.core for s in builtin_core_source().specs())


def test_builtin_clarify_echoes_question():
    src = builtin_core_source()
    ctx = ToolContext(thread_id="t", turn_index=0, channel="api")
    result = src.call("clarify", {"question": "quelle taille ?"}, ctx)
    assert result.content == "quelle taille ?" and not result.is_error


def test_recall_context_is_no_longer_a_known_tool():
    src = builtin_core_source()
    ctx = ToolContext(thread_id="t", turn_index=0, channel="api")
    result = src.call("recall_context", {"query": "x"}, ctx)
    assert result.is_error and "inconnu" in result.content


def test_builtin_find_and_escalate_fail_if_reached_directly():
    src = builtin_core_source()
    ctx = ToolContext(thread_id="t", turn_index=0, channel="api")
    assert src.call("tool_find", {}, ctx).is_error
    assert src.call("escalate_to_human", {}, ctx).is_error


def test_static_tool_source_unknown_name():
    src = StaticToolSource(entries=[])
    r = src.call("nope", {}, ToolContext(thread_id="t", turn_index=0, channel="api"))
    assert r.is_error


def test_native_tools_declare_the_argument_the_loop_reads():
    from nokker.tools.registry import builtin_core_source

    specs = {s.name: s for s in builtin_core_source().specs()}
    assert specs["tool_find"].parameters["required"] == ["query"]
    assert specs["escalate_to_human"].parameters["required"] == ["reason"]
