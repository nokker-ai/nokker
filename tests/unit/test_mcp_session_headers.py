"""En-têtes MCP par session (`mcp_headers`, `local.mcp_session_headers`) : une connexion à part par fil qui en porte,
fixes puis ceux du fil ; les autres fils gardent la connexion du serveur ; refusé sans le drapeau."""
from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest
from mcp import types

import nokker.tools.mcp as mcp_mod
from nokker.core.ports import ToolContext
from nokker.local.api import LocalApi
from nokker.tools import mcp_session
from nokker.tools.mcp import McpToolSource


class _Session:
    def __init__(self, label):
        self.label = label

    async def call_tool(self, name, arguments):
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"{self.label}:{name}")])


class _Io:
    made: ClassVar[list] = []

    def __init__(self, url, headers=None, *a, **kw):
        self.headers, self.closed = headers, False
        self.active, self.last_used = 0, 0.0
        _Io.made.append(self)

    def run(self, coro):
        return asyncio.run(coro)

    async def session_async(self):
        return _Session(repr(sorted((self.headers or {}).items())))

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    _Io.made = []
    monkeypatch.setattr(mcp_mod, "_SyncMcp", _Io)
    monkeypatch.setattr(mcp_session, "_REGISTRY", mcp_session.SessionHeaders())
    yield


def _ctx(thread):
    return ToolContext(thread_id=thread, turn_index=0, channel="api")


def _src():
    return McpToolSource(name="sokkan", url="https://h.invalid/mcp", headers={"X-Static": "s"})


def test_session_headers_use_their_own_connection_others_keep_the_default():
    src = _src()
    default = src._io
    mcp_session.registry().set("th_a", {"sokkan": {"Authorization": "Bearer jeton-a"}})
    a = src.call("board_list", {}, _ctx("th_a")).content
    assert "jeton-a" in a and "X-Static" in a  # fixes PUIS ceux du fil
    other = src.call("board_list", {}, _ctx("th_b")).content
    assert "jeton-a" not in other and src._io is default
    src.call("board_list", {}, _ctx("th_a"))
    assert len([i for i in _Io.made if i is not default and i.headers.get("Authorization")]) == 1  # connexion réutilisée


def test_subagent_inherits_and_other_server_is_untouched():
    src = _src()
    mcp_session.registry().set("th_a", {"sokkan": {"Authorization": "Bearer jeton-a"}, "autre": {"X-A": "1"}})
    assert "jeton-a" in src.call("t", {}, _ctx("th_a/sa-1")).content
    src2 = McpToolSource(name="memoire", url="https://h.invalid/mcp")
    assert "jeton-a" not in src2.call("t", {}, _ctx("th_a")).content


def test_replaced_by_the_next_open_and_closed_with_the_source():
    src = _src()
    mcp_session.registry().set("th_a", {"sokkan": {"Authorization": "Bearer v1"}})
    assert "v1" in src.call("t", {}, _ctx("th_a")).content
    mcp_session.registry().set("th_a", {"sokkan": {"Authorization": "Bearer v2"}})
    assert "v2" in src.call("t", {}, _ctx("th_a")).content
    mcp_session.registry().set("th_a", {})
    assert "Authorization" not in src.call("t", {}, _ctx("th_a")).content
    src.close()
    assert all(i.closed for i in _Io.made if i.headers.get("Authorization"))


@pytest.mark.parametrize("bad", [
    "x", {"s": "x"}, {"s": {"Host": "evil"}}, {"s": {"a b": "v"}}, {"s": {"X": "a\r\nInjected: 1"}},
    {"s": {"X": 5}}, {"s": {"X": "v" * 5000}}, {f"s{i}": {} for i in range(17)}])
def test_validation_refuses_bad_headers(bad):
    assert mcp_session.validate(bad)[1] is not None


def _api(tmp_path, **local):
    root = tmp_path / "p"
    (root / "app").mkdir(parents=True)
    api = LocalApi.from_config({"token_path": str(tmp_path / "tok"), "code": {"roots": [str(root)],
                                                                             "backend": "local"}, **local}, port=8742)
    return api, f"Bearer {api.token}", str(root / "app")


def test_open_session_flag_off_refuses_on_validates_and_records(tmp_path):
    api, auth, cwd = _api(tmp_path)
    status, body = api.handle("POST", "/v1/local/sessions", auth, {"cwd": cwd, "mcp_headers": {"s": {"X": "v"}}})
    assert status == 400 and "mcp_session_headers" in body["error"]["message"]
    assert api.handle("POST", "/v1/local/sessions", auth, {"cwd": cwd})[0] == 201
    api, auth, cwd = _api(tmp_path / "on", mcp_session_headers=True)
    assert api.handle("POST", "/v1/local/sessions", auth, {"cwd": cwd, "mcp_headers": {"s": {"Host": "x"}}})[0] == 400
    status, body = api.handle("POST", "/v1/local/sessions", auth, {"cwd": cwd, "mcp_headers": {"s": {"X-T": "tok-zqwx-9"}}})
    assert status == 201 and "tok-zqwx-9" not in str(body)  # la valeur n'est jamais rendue
    assert mcp_session.registry().get(body["thread_id"], "s") == {"X-T": "tok-zqwx-9"}
