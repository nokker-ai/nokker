"""`McpToolSource` — pas de socket réel : `_SyncMcp` est remplacé par un faux qui exécute les coroutines
directement (asyncio.run) et renvoie une fausse session MCP. Vérifie la traduction
Tool/CallToolResult (SDK `mcp`) -> `ToolSpec`/`ToolResult` (noyau), pas le SDK lui-même."""
from __future__ import annotations

import asyncio

from mcp import types

from nokker.core.ports import ToolContext
from nokker.core.turn import Effect
from nokker.tools.mcp import McpToolSource, text_of


class FakeSession:
    def __init__(self, tools, call_results):
        self._tools = tools
        self._call_results = call_results

    async def list_tools(self):
        return types.ListToolsResult(tools=self._tools)

    async def call_tool(self, name, arguments):
        return self._call_results[name]


class FakeSyncMcp:
    """Remplace `_SyncMcp` : exécute la coroutine directement, pas de thread/boucle dédiée."""

    def __init__(self, session: FakeSession):
        self._session = session

    def run(self, coro):
        return asyncio.run(coro)

    async def session_async(self):
        return self._session

    def close(self):
        pass


def make_source(tools, call_results, core_tools=frozenset(), effects=None):
    src = McpToolSource(name="sokkan-memory", url="https://example.invalid/mcp",
                        core_tools=core_tools, effects=effects or {})
    src._io = FakeSyncMcp(FakeSession(tools, call_results))
    return src


def _described(text, **kw):
    tool = types.Tool(name="big", description=text, inputSchema={})
    src = make_source([tool], {})
    for k, v in kw.items():
        setattr(src, k, v)
    return src.specs()[0].description


def test_a_1000_character_description_stays_whole_with_its_usage_rules(caplog):
    text = "Premier paragraphe.\n\nRègles d'usage : " + "x" * 1000
    with caplog.at_level("WARNING", logger="nokker.tools.schema"):
        assert _described(text) == text
    assert not caplog.records


def test_a_5000_character_description_is_cut_at_4000_and_logged(caplog):
    with caplog.at_level("WARNING", logger="nokker.tools.schema"):
        out = _described("y" * 5000)
    assert out == "y" * 4000
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "sokkan-memory" in msg and "big" in msg and "4000" in msg and "5000" in msg


def test_description_cap_is_per_server(caplog):
    with caplog.at_level("WARNING", logger="nokker.tools.schema"):
        assert _described("z" * 500, description_max_chars=100) == "z" * 100
        assert _described("z" * 500, description_max_chars=800) == "z" * 500
    assert len(caplog.records) == 1


def test_cap_comes_from_the_server_entry_of_the_config():
    from nokker.tools.schema import DESCRIPTION_MAX_CHARS, description_cap

    assert (DESCRIPTION_MAX_CHARS, description_cap(None), description_cap("abc"), description_cap(0)) == (4000,) * 4
    assert description_cap(1500) == 1500 and description_cap("900") == 900


def test_specs_translates_tool_to_toolspec():
    tool = types.Tool(name="memory_search", description="Cherche dans la mémoire.\nRègle d'usage : relis avant d'écrire.",
                       inputSchema={"type": "object", "properties": {"query": {"type": "string"}}})
    src = make_source([tool], {}, core_tools=frozenset({"memory_search"}))
    specs = src.specs()
    assert len(specs) == 1
    s = specs[0]
    assert s.name == "memory_search" and s.description == "Cherche dans la mémoire.\nRègle d'usage : relis avant d'écrire."  # entière, pas la 1re ligne
    assert s.core is True and s.source == "mcp:sokkan-memory"
    assert s.parameters["properties"]["query"]["type"] == "string"


def test_specs_default_effect_is_write():
    tool = types.Tool(name="mail_draft", description="", inputSchema={})
    src = make_source([tool], {})
    assert src.specs()[0].effect == Effect.WRITE


def test_specs_effect_override():
    tool = types.Tool(name="mail_read", description="", inputSchema={})
    src = make_source([tool], {}, effects={"mail_read": Effect.READ})
    assert src.specs()[0].effect == Effect.READ


def test_specs_cached_not_refetched():
    calls = {"n": 0}
    tool = types.Tool(name="t", description="", inputSchema={})

    class CountingSession(FakeSession):
        async def list_tools(self):
            calls["n"] += 1
            return await super().list_tools()

    src = McpToolSource(name="s", url="https://example.invalid/mcp")
    src._io = FakeSyncMcp(CountingSession([tool], {}))
    src.specs()
    src.specs()
    assert calls["n"] == 1


def test_refresh_forces_relist():
    tool = types.Tool(name="t", description="", inputSchema={})
    src = make_source([tool], {})
    src.specs()
    src.refresh()
    assert src._cache is None


def test_call_returns_concatenated_text():
    result = types.CallToolResult(content=[types.TextContent(type="text", text="2 rendez-vous")])
    src = make_source([], {"calendar_list": result})
    ctx = ToolContext(thread_id="t1", turn_index=0, channel="api")
    r = src.call("calendar_list", {}, ctx)
    from nokker.tools.result_fence import fence_mcp_result

    assert r.content == fence_mcp_result(src.name, "calendar_list", "2 rendez-vous") and not r.is_error


def test_call_propagates_is_error():
    result = types.CallToolResult(content=[types.TextContent(type="text", text="plus de place")],
                                  isError=True)
    src = make_source([], {"disk_extend": result})
    ctx = ToolContext(thread_id="t1", turn_index=0, channel="api")
    r = src.call("disk_extend", {}, ctx)
    assert r.is_error


def test_call_exception_becomes_error_result_not_raised():
    class Boom(FakeSyncMcp):
        def run(self, coro):
            coro.close()
            raise RuntimeError("serveur MCP down")

    src = McpToolSource(name="s", url="https://example.invalid/mcp")
    src._io = Boom(FakeSession([], {}))
    r = src.call("x", {}, ToolContext(thread_id="t", turn_index=0, channel="api"))
    assert r.is_error and "serveur MCP down" in r.content


def test_text_of_empty_content():
    result = types.CallToolResult(content=[])
    assert text_of(result) == ""


def test_close_on_turn_end_closes_only_after_the_last_thread_using_the_server():
    """Alice 20.10 : les onglets du navigateur partagé doivent être rendus après usage (2 max par site hostile)."""
    from nokker.core.ports import ToolContext
    from nokker.tools.mcp import McpToolSource

    src = McpToolSource(name="browser", url="http://127.0.0.1:1/mcp", close_on_turn_end="browser_close")
    closed = []

    class FakeIo:
        def run(self, coro):
            closed.append(coro.cr_code.co_name)
            coro.close()

    src._io = FakeIo()
    src.call("browser_navigate", {"url": "https://x"}, ToolContext("t1", 0, "api", "alice"))
    src.call("browser_navigate", {"url": "https://y"}, ToolContext("t2", 0, "api", "alice"))
    closed.clear()
    src.turn_done("t1")
    assert closed == []           # t2 tient encore la session
    src.turn_done("t2")
    assert closed == ["_close"]   # dernier fil : fermeture
    src.turn_done("t2")
    assert closed == ["_close"]   # rien de plus sans nouvel usage


def test_registry_turn_done_reaches_wrapped_sources_and_survives_errors():
    from nokker.tools.effects import SecretScrubbingToolSource
    from nokker.tools.registry import Registry

    seen = []

    class Inner:
        def specs(self):
            return []

        def turn_done(self, tid):
            seen.append(tid)

    class Broken:
        def specs(self):
            return []

        def turn_done(self, tid):
            raise RuntimeError("panne")

    Registry(sources=[Broken(), SecretScrubbingToolSource(inner=Inner())]).turn_done("t9")
    assert seen == ["t9"]


def test_sync_mcp_two_cold_callers_share_one_session_without_deadlock(monkeypatch):
    """Revue 20.10 : un `threading.Lock` tenu à travers les `await` de la poignée de main bloquait le thread de la
    boucle dès qu'une 2e coroutine arrivait sur une session froide (rappel du tour dans le pool + memory_search du
    modèle) : interblocage définitif jusqu'au redémarrage. Deux appelants simultanés doivent partager UNE session."""
    import threading
    import time
    from contextlib import asynccontextmanager

    import nokker.tools.mcp as mcp_mod
    from nokker.tools.mcp import _SyncMcp

    @asynccontextmanager
    async def fake_transport(url, http_client=None):
        await asyncio.sleep(0.3)  # connexion lente : la coroutine cède la boucle
        yield ("read", "write")

    class FakeClientSession:
        created = 0

        def __init__(self, read, write):
            type(self).created += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def initialize(self):
            await asyncio.sleep(0.3)

    monkeypatch.setattr(mcp_mod, "streamable_http_client", fake_transport)
    monkeypatch.setattr(mcp_mod, "ClientSession", FakeClientSession)
    io = _SyncMcp("https://example.invalid/mcp", timeout_s=3.0)
    out = {}

    def go(name, delay):
        time.sleep(delay)
        t0 = time.monotonic()
        out[name] = (io.run(io.session_async()), time.monotonic() - t0)

    threads = [threading.Thread(target=go, args=("a", 0.0)), threading.Thread(target=go, args=("b", 0.1))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not any(t.is_alive() for t in threads)
    assert out["a"][0] is out["b"][0] and FakeClientSession.created == 1
    assert out["a"][1] < 2 and out["b"][1] < 2
    io.close()
