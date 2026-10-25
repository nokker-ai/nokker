"""Outils en LECTURE déclarés pour un serveur ajouté par `mcp_server_add`, sans pack (nokker/tools/mcp_config.py,
docs/en/packs.md « Declaring read tools »).

Par défaut tout outil d'un serveur ajouté reste WRITE. Une liste explicite `read_tools` (à l'ajout, ou après par
`mcp_server_read_tools` ou la route locale) rend ces outils READ au démarrage — jamais un outil que le noyau, la config
ou un pack classe déjà, et jamais un outil dont le NOM ou la DESCRIPTION annonce une écriture évidente sans
confirmation explicite sous carte forte (plancher que ni le mode ni une règle ne lèvent)."""
from __future__ import annotations

import json

import pytest

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Decision, Effect, ToolCall
from nokker.tools.effects import EffectsTable
from nokker.tools.mcp_config import (
    McpConfigError,
    McpConfigToolSource,
    ReadDeclaredSource,
    check_entry,
    load_user_servers,
    set_read_tools,
    write_words,
)

CTX = ToolContext(thread_id="t-read", turn_index=0, channel="api", sender="alice")
URL = "https://mcp.example.com/mcp"


@pytest.fixture
def src(tmp_path):
    return McpConfigToolSource(path=tmp_path / "mcp-servers.yaml", configured=lambda: ["memory"])


def call(s, tool, **a):
    r = s.call(tool, a, CTX)
    return r, (json.loads(r.content) if not r.is_error else r.content)


class _Fake:
    """Source MCP minimale : des specs fixes (comme `McpToolSource` après `list_tools`), tout en WRITE."""

    def __init__(self, tools: dict[str, str]):
        self.tools = tools
        self.calls: list[str] = []

    def specs(self):
        return [ToolSpec(name=n, description=d, parameters={}, source="mcp:tracker") for n, d in self.tools.items()]

    def call(self, name, arguments, ctx):
        self.calls.append(name)
        return f"ok:{name}"

    def turn_done(self, thread_id):
        self.calls.append(f"done:{thread_id}")


# ─────────────────────────────── écriture évidente ───────────────────────────────


@pytest.mark.parametrize("text", ["send_message", "deleteIssue", "issues.create", "post-update", "git_push", "rm",
                                  "Creates a new ticket", "Sends the draft to the recipients", "write_file",
                                  "update_status", "ScheduleSend", "remove_label"])
def test_obvious_writes_are_detected(text):
    assert write_words(text)


@pytest.mark.parametrize("text", ["list_issues", "get_issue", "searchTickets", "read_page", "status",
                                  "Returns the open issues of a project", "describe_board", ""])
def test_reads_are_not_flagged(text):
    assert write_words(text) == []


# ─────────────────────────────── déclaration ───────────────────────────────


def test_default_everything_stays_write(src, tmp_path):
    r, out = call(src, "mcp_server_add", name="tracker", url=URL)
    assert not r.is_error and "read_tools" not in out
    [entry] = load_user_servers(tmp_path / "mcp-servers.yaml")
    assert "read_tools" not in entry
    wrapped = ReadDeclaredSource(inner=_Fake({"list_issues": "List"}), read_tools=[], effects=EffectsTable())
    assert {s.name: s.effect for s in wrapped.specs()} == {"list_issues": Effect.WRITE}


def test_read_tools_at_add_and_listed(src, tmp_path):
    r, out = call(src, "mcp_server_add", name="tracker", url=URL, read_tools=["list_issues", "get_issue"])
    assert not r.is_error and out["read_tools"] == ["list_issues", "get_issue"]
    [entry] = load_user_servers(tmp_path / "mcp-servers.yaml")
    assert entry["read_tools"] == ["list_issues", "get_issue"]
    _, listed = call(src, "mcp_server_list")
    assert listed["servers"][0]["read_tools"] == ["list_issues", "get_issue"]


def test_write_named_tool_refused_without_confirmation(src, tmp_path):
    r, out = call(src, "mcp_server_add", name="tracker", url=URL, read_tools=["list_issues", "delete_issue"])
    assert r.is_error and "delete_issue" in out and "confirm_writes" in out
    assert not (tmp_path / "mcp-servers.yaml").exists()  # rien d'écrit


def test_confirmation_must_name_exactly_the_write_named_tools(src):
    r, out = call(src, "mcp_server_add", name="tracker", url=URL, read_tools=["delete_issue"],
                  confirm_writes=["other"])
    assert r.is_error and "delete_issue" in out
    r, out = call(src, "mcp_server_add", name="tracker", url=URL, read_tools=["list_issues"],
                  confirm_writes=["list_issues"])
    assert r.is_error and "confirm_writes" in out  # rien à confirmer : la liste ne sert qu'aux noms d'écriture


def test_confirmed_write_name_is_kept(src, tmp_path):
    r, out = call(src, "mcp_server_add", name="tracker", url=URL, read_tools=["post_count"],
                  confirm_writes=["post_count"])
    assert not r.is_error and out["confirmed_writes"] == ["post_count"]
    [entry] = load_user_servers(tmp_path / "mcp-servers.yaml")
    assert entry["confirmed_writes"] == ["post_count"]


def test_bad_tool_names_refused():
    with pytest.raises(McpConfigError):
        check_entry({"name": "tracker", "url": URL, "read_tools": ["../x"]})
    with pytest.raises(McpConfigError):
        check_entry({"name": "tracker", "url": URL, "read_tools": "list_issues"})


def test_declare_after_add_replaces_the_list(src, tmp_path):
    call(src, "mcp_server_add", name="tracker", url=URL, read_tools=["list_issues"])
    r, out = call(src, "mcp_server_read_tools", name="tracker", read_tools=["get_issue"])
    assert not r.is_error and out == {"server": "tracker", "read_tools": ["get_issue"], "confirmed_writes": [],
                                      "active": "au prochain redémarrage du noyau"}
    assert load_user_servers(tmp_path / "mcp-servers.yaml")[0]["read_tools"] == ["get_issue"]
    r, out = call(src, "mcp_server_read_tools", name="tracker", read_tools=[])
    assert not r.is_error and "read_tools" not in load_user_servers(tmp_path / "mcp-servers.yaml")[0]
    r, out = call(src, "mcp_server_read_tools", name="memory", read_tools=["x"])
    assert r.is_error and "config.yaml" in out  # serveur de la config : pas d'ici


def test_set_read_tools_function_used_by_the_route(tmp_path):
    path = tmp_path / "mcp-servers.yaml"
    McpConfigToolSource(path=path).call("mcp_server_add", {"name": "tracker", "url": URL}, CTX)
    assert set_read_tools(path, "tracker", ["list_issues"])["read_tools"] == ["list_issues"]
    with pytest.raises(McpConfigError, match="inconnu"):
        set_read_tools(path, "nope", ["list_issues"])
    with pytest.raises(McpConfigError, match="confirm_writes"):
        set_read_tools(path, "tracker", ["send_report"])


def test_tool_effect_is_destructive(src):
    specs = {s.name: s.effect for s in src.specs()}
    assert specs["mcp_server_read_tools"] is Effect.DESTRUCTIVE


# ─────────────────────────────── plancher : carte forte ───────────────────────────────


def _floor(src, tool, **args):
    return src.floor(ToolCall(id="c1", name=tool, arguments=args), None, None)


def test_floor_strong_card_for_confirmed_write_names(src):
    v = _floor(src, "mcp_server_read_tools", name="tracker", read_tools=["post_count"], confirm_writes=["post_count"])
    assert v.decision is Decision.HITL and v.strength == "strong" and "post_count" in v.reason
    v = _floor(src, "mcp_server_add", name="tracker", url=URL, read_tools=["post_count"],
               confirm_writes=["post_count"])
    assert v.decision is Decision.HITL and v.strength == "strong"


def test_floor_denies_unconfirmed_and_ignores_plain_reads(src):
    v = _floor(src, "mcp_server_read_tools", name="tracker", read_tools=["delete_issue"])
    assert v.decision is Decision.DENY
    assert _floor(src, "mcp_server_read_tools", name="tracker", read_tools=["list_issues"]) is None
    assert _floor(src, "mcp_server_list") is None and _floor(src, "terminal", command="ls") is None


# ─────────────────────────────── démarrage : effets ───────────────────────────────


def test_declared_reads_become_read_at_startup():
    inner = _Fake({"list_issues": "List the open issues", "get_issue": "Read one issue",
                   "close_issue": "Close an issue"})
    s = ReadDeclaredSource(inner=inner, read_tools=["list_issues", "get_issue"], effects=EffectsTable())
    assert {x.name: x.effect for x in s.specs()} == {"list_issues": Effect.READ, "get_issue": Effect.READ,
                                                    "close_issue": Effect.WRITE}
    assert s.call("get_issue", {}, CTX) == "ok:get_issue"
    s.turn_done("t1")
    assert inner.calls == ["get_issue", "done:t1"]


def test_description_that_announces_a_write_keeps_write_unless_confirmed():
    inner = _Fake({"sync": "Creates the missing tickets and sends a summary"})
    plain = ReadDeclaredSource(inner=inner, read_tools=["sync"], effects=EffectsTable())
    assert plain.specs()[0].effect is Effect.WRITE
    confirmed = ReadDeclaredSource(inner=inner, read_tools=["sync"], confirmed_writes=["sync"],
                                   effects=EffectsTable())
    assert confirmed.specs()[0].effect is Effect.READ


def test_a_declaration_never_lowers_what_the_core_config_or_a_pack_says():
    inner = _Fake({"mail_send": "Send", "linkedin_reply_comment": "Reply", "mail_move": "Move", "fetch": "Fetch"})
    table = EffectsTable.load({"linkedin_reply_comment": "send_external", "fetch": "write"})
    s = ReadDeclaredSource(inner=inner, read_tools=list(inner.tools), confirmed_writes=list(inner.tools),
                           effects=table)
    got = {x.name: x.effect for x in s.specs()}
    assert got["mail_send"] is Effect.SEND_EXTERNAL  # table du noyau
    assert got["linkedin_reply_comment"] is Effect.SEND_EXTERNAL  # relevé par un pack / la config
    assert got["fetch"] is Effect.WRITE  # surcharge explicite de la config
    assert got["mail_move"] is Effect.WRITE and next(x for x in s.specs() if x.name == "mail_move").effect_of


def test_build_kernel_wraps_servers_that_declare_reads(monkeypatch):
    """`build_kernel` : un serveur avec `read_tools` est entouré de `ReadDeclaredSource` (les autres non)."""
    from nokker.api import server
    from nokker.config import NokkerConfig

    cfg = NokkerConfig(mcp_tool_servers=[{"name": "tracker", "url": URL, "read_tools": ["list_issues"]},
                                         {"name": "other", "url": URL}])
    sources = server.mcp_tool_sources(cfg, EffectsTable(), None)
    assert [type(s).__name__ for s in sources] == ["ReadDeclaredSource", "SecretScrubbingToolSource"]
    assert sources[0].read_tools == ["list_issues"]


def test_no_mode_nor_allow_rule_lifts_the_strong_card(src):
    """Devant les permissions locales (comme `serve` le branche) : mode auto ET règle `allow` sur l'outil, la carte
    forte reste ; une déclaration ordinaire suit les permissions."""
    from nokker.core.turn import Gate, Verdict
    from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates, Rule

    class Inner:
        class invariants:
            @staticmethod
            def check_action(call, spec, turn):
                return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="ok")

        def action(self, call, spec, turn):
            return [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="ok")]

    gates = PermissionGates(inner=Inner(), permissions=LocalPermissions(
        mode=Mode.AUTO, allow=[Rule.parse("mcp_server_read_tools")]), floors=[src.floor])
    spec = next(s for s in src.specs() if s.name == "mcp_server_read_tools")
    risky = ToolCall(id="c1", name="mcp_server_read_tools", arguments={
        "name": "tracker", "read_tools": ["post_count"], "confirm_writes": ["post_count"]})
    [v] = gates.action(risky, spec, None)
    assert v.decision is Decision.HITL and v.strength == "strong"
    plain = ToolCall(id="c2", name="mcp_server_read_tools", arguments={"name": "tracker", "read_tools": ["list_x"]})
    [v] = gates.action(plain, spec, None)
    assert v.decision is Decision.ALLOW


# ─────────────────────────────── route locale ───────────────────────────────


def test_local_route_declares_reads_and_refuses_write_names(tmp_path):
    from nokker.local.api import LocalApi

    root = tmp_path / "projets"
    root.mkdir()
    api = LocalApi.from_config({"token_path": str(tmp_path / "tok"), "code": {"roots": [str(root)],
                                                                             "backend": "local"}}, port=8742)
    api.state_dir = tmp_path / "state"
    auth = f"Bearer {api.token}"
    McpConfigToolSource(path=api.state_dir / "mcp-servers.yaml").call(
        "mcp_server_add", {"name": "tracker", "url": URL}, CTX)
    status, body = api.handle("GET", "/v1/local/mcp-servers", auth, {})
    assert status == 200 and body["servers"][0]["name"] == "tracker" and "read_tools" not in body["servers"][0]
    status, body = api.handle("PUT", "/v1/local/mcp-servers/tracker/read-tools", auth,
                              {"read_tools": ["list_issues"]})
    assert status == 200 and body["read_tools"] == ["list_issues"]
    assert load_user_servers(api.state_dir / "mcp-servers.yaml")[0]["read_tools"] == ["list_issues"]
    # nom d'écriture évidente : jamais par la route, même « confirmé » (la carte forte passe par l'outil)
    status, body = api.handle("PUT", "/v1/local/mcp-servers/tracker/read-tools", auth,
                              {"read_tools": ["send_report"], "confirm_writes": ["send_report"]})
    assert status == 409 and body["error"]["code"] == "needs_strong_card" and "send_report" in body["error"]["message"]
    assert api.handle("PUT", "/v1/local/mcp-servers/nope/read-tools", auth, {"read_tools": []})[0] == 404
    assert api.handle("PUT", "/v1/local/mcp-servers/tracker/read-tools", auth, {"read_tools": "x"})[0] == 400
    assert api.handle("PUT", "/v1/local/mcp-servers/tracker/read-tools", "Bearer faux", {"read_tools": []})[0] == 401
