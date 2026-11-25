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


# ── serveur MCP injoignable : ses outils seuls sont retirés pour le tour, le suivant retente ──
class _DeadIo:
    """`_SyncMcp` qui ne joint personne (découverte) ; `ok` le rétablit."""

    def __init__(self):
        self.ok = False
        self.tools = []
        self.calls = 0

    def run(self, coro):
        import asyncio

        return asyncio.run(coro)

    async def session_async(self):
        self.calls += 1
        if not self.ok:
            raise ConnectionError("refus sur https://secret-host.invalid/mcp?token=abc123")
        io = self

        class S:
            async def list_tools(self):
                from mcp import types

                return types.ListToolsResult(tools=io.tools)

            async def call_tool(self, name, arguments):
                raise ConnectionError("coupé en cours d'appel")

        return S()

    def close(self):
        pass


def _mcp(name, tools=("t",), ok=True):
    from mcp import types

    from nokker.tools.mcp import McpToolSource

    src = McpToolSource(name=name, url="https://example.invalid/mcp")
    io = _DeadIo()
    io.ok, io.tools = ok, [types.Tool(name=t, description="d", inputSchema={}) for t in tools]
    src._io = io
    return src, io


def test_unreachable_mcp_server_drops_only_its_tools_and_is_retried_next_turn():
    good, _ = _mcp("bon", ("lire",))
    dead, io = _mcp("panne", ("ecrire",), ok=False)
    reg = Registry(sources=[builtin_core_source(), dead, good])
    names = [n for n, _d in reg.index()]  # ne lève pas
    assert names == ["lire"] and reg.spec("ecrire") is None
    [(server, why)] = reg.unavailable()
    assert server == "panne" and why.startswith("ConnectionError")
    assert "abc123" not in why and "secret-host" not in why  # ni adresse ni jeton
    asked = io.calls
    reg.index(), reg.spec("lire")  # même tour : pas de nouvelle tentative
    assert io.calls == asked
    reg.turn_done("th")  # fin de tour : retenté, et rétabli ici
    io.ok = True
    assert sorted(n for n, _d in reg.index()) == ["ecrire", "lire"] and reg.unavailable() == []


def test_two_servers_one_down_the_other_still_answers():
    good, _ = _mcp("bon", ("lire",))
    dead, _ = _mcp("panne", ("ecrire",), ok=False)
    reg = Registry(sources=[dead, good])
    r = reg.call("lire", {}, ToolContext(thread_id="th", turn_index=0, channel="api", sender="a"))
    assert "indisponible" not in r.content  # l'appel a atteint le bon serveur
    assert reg.call("ecrire", {}, ToolContext(thread_id="th", turn_index=0, channel="api", sender="a")).is_error
    assert [s for s, _w in reg.unavailable()] == ["panne"]


def test_a_server_that_raises_at_call_is_dropped_for_the_turn():
    from nokker.tools.select import unavailable_line

    class Raising(FakeSource):
        def call(self, name, arguments, ctx):
            raise ConnectionError("coupé")

    good, _ = _mcp("bon", ("lire",))
    src, _ = _mcp("flaky", ("f",))
    src.call = lambda name, arguments, ctx: (_ for _ in ()).throw(ConnectionError("coupé"))  # type: ignore[method-assign]
    reg = Registry(sources=[src, good])
    r = reg.call("f", {}, ToolContext(thread_id="th", turn_index=0, channel="api", sender="a"))
    assert r.is_error and r.content == unavailable_line(["flaky"])
    assert reg.spec("f") is None and reg.spec("lire") is not None


def test_a_failing_non_mcp_source_still_raises():
    import pytest

    class Broken:
        def specs(self):
            raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        Registry(sources=[Broken()]).index()


def test_the_model_sees_one_line_naming_the_unavailable_server(tmp_path):
    from nokker.context.engine import ContextEngineV0

    good, _ = _mcp("bon", ("lire",))
    dead, _ = _mcp("panne", ("ecrire",), ok=False)
    dead2, _ = _mcp("panne2", ("x",), ok=False)
    prefix = tmp_path / "p.md"
    prefix.write_text("Tu es Atlas.")
    block = ContextEngineV0(tools=Registry(sources=[good, dead]), prefix_path=str(prefix)).tools_index_block()
    assert block.splitlines()[-1] == ("Le serveur « panne » n'est pas joignable pour ce tour : ses outils ne sont pas disponibles. "
                                      "Dis-le simplement à la personne (sans le nom du serveur) et continue sans eux.")
    assert "ecrire" not in block and "lire" in block
    block2 = ContextEngineV0(tools=Registry(sources=[good, dead, dead2]), prefix_path=str(prefix)).tools_index_block()
    assert block2.splitlines()[-1] == ("Les serveurs « panne », « panne2 » ne sont pas joignables pour ce tour : leurs outils ne sont pas "
                                       "disponibles. Dis-le simplement à la personne (sans le nom des serveurs) et continue sans eux.")
    ok = ContextEngineV0(tools=Registry(sources=[good]), prefix_path=str(prefix)).tools_index_block()
    assert "indisponible" not in ok


def test_the_kernel_emits_one_host_event_per_server_and_turn():
    from types import SimpleNamespace

    from nokker.core.loop import Kernel
    from nokker.core.turn import InputCheck, Turn

    dead, _ = _mcp("panne", ("ecrire",), ok=False)
    reg = Registry(sources=[dead])
    reg.index()
    events = []
    fake = SimpleNamespace(tools=reg, _unavailable_seen={}, _observe_opt=lambda m, *a: events.append((m, a[1])))
    turn = Turn(thread_id="th", index=4, input=InputCheck(text="x"))
    Kernel._report_unavailable(fake, turn)
    Kernel._report_unavailable(fake, turn)  # même tour : une fois
    assert [(m, d["server"], d["reason"].split(":")[0]) for m, d in events] == [
        ("mcp_server_unavailable", "panne", "ConnectionError")]
    Kernel._report_unavailable(fake, Turn(thread_id="th", index=5, input=InputCheck(text="x")))
    assert len(events) == 2  # au tour suivant, de nouveau
