"""`deny_tools` d'une session (ouverture de `POST /v1/local/sessions`) : retirés du catalogue montré au modèle ET
refusés à l'exécution ; `local.surfaces_gate` retire les outils de bulles d'une session sans surface de bulles."""
from __future__ import annotations

import pytest

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Decision, Effect, ToolCall, ToolResult
from nokker.local.api import BUBBLE_TOOLS, LocalApi
from nokker.qa.permissions import LocalPermissions, Mode
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.resources import TenantRegistry
from nokker.tools.registry import Registry, StaticToolSource
from nokker.tools.scope import thread_scope

TN = "tn_deny00000001"
FIL = f"{TN}:th_a"


def _spec(name, core=False):
    return ToolSpec(name=name, description=f"outil {name}", parameters={"type": "object", "properties": {}},
                    effect=Effect.READ, core=core, source="builtin")


def _registry():
    src = StaticToolSource(entries=[(_spec(n, core=(n == "terminal")), (lambda a, c, n=n: ToolResult(
        call_id="", name=n, content=f"ok {n}"))) for n in ("terminal", "bubble_update", "read_file")])
    return TenantRegistry(internal=Registry(sources=[]), tester_sources=[src])


def _ctx(thread):
    return ToolContext(thread_id=thread, turn_index=0, channel="api")


def test_hidden_tools_leave_the_catalog_and_are_refused():
    reg = _registry()
    with tenant_scope(TN), thread_scope(FIL):
        key0 = reg.cache_key()
        assert {n for n, _ in reg.index()} == {"bubble_update", "read_file"} and reg.spec("bubble_update") is not None
        reg.hide_for_thread(FIL, ["bubble_update", "terminal"])
        assert reg.cache_key() != key0  # l'empreinte du préfixe change avec le catalogue
        assert reg.core() == [] and reg.index() == [("read_file", "outil read_file")]
        assert reg.spec("bubble_update") is None and reg.spec("read_file") is not None
        assert [s.name for s in reg.find("outil")] == ["read_file"]
        res = reg.call("bubble_update", {}, _ctx(FIL))
        assert res.is_error and res.content.startswith("outil inconnu : bubble_update")
        assert reg.call("read_file", {}, _ctx(FIL)).content == "ok read_file"
        reg.hide_for_thread(FIL, [])  # une ouverture sans liste remplace l'ancienne
        assert reg.spec("bubble_update") is not None and reg.cache_key() == key0


def test_hidden_tools_are_per_thread_and_inherited_by_subagents():
    reg = _registry()
    reg.hide_for_thread(FIL, ["bubble_update"])
    with tenant_scope(TN):
        with thread_scope(FIL + "/sa-1"):
            assert reg.spec("bubble_update") is None
            assert reg.call("bubble_update", {}, _ctx(FIL + "/sa-1")).is_error
        with thread_scope(f"{TN}:th_b"):
            assert reg.spec("bubble_update") is not None
            assert reg.call("bubble_update", {}, _ctx(f"{TN}:th_b")).content == "ok bubble_update"


def test_deny_rule_refuses_at_execution_for_the_thread_and_its_subagents():
    perms = LocalPermissions(mode=Mode.AUTO)
    perms.restrict_with(FIL, {"deny": ["bubble_update"]})
    call = ToolCall(id="1", name="bubble_update", arguments={})
    for thread in (FIL, FIL + "/sa-2"):
        decision, _, code = perms.decide_coded(call, Effect.READ, thread)
        assert decision is Decision.DENY and code == "deny_rule"
    assert perms.decide_coded(call, Effect.READ, f"{TN}:th_b")[0] is not Decision.DENY


# ── ouverture de session ──


def _api(tmp_path, **local):
    root = tmp_path / "p"
    (root / "app").mkdir(parents=True)
    api = LocalApi.from_config({"token_path": str(tmp_path / "tok"), "code": {"roots": [str(root)],
                                                                             "backend": "local"}, **local}, port=8742)
    return api, f"Bearer {api.token}", str(root / "app")


def _open(api, auth, cwd, **body):
    return api.handle("POST", "/v1/local/sessions", auth, {"cwd": cwd, **body})


def test_deny_tools_unsupported_without_the_hook_and_default_untouched(tmp_path):
    api, auth, cwd = _api(tmp_path)
    status, body = _open(api, auth, cwd, deny_tools=["terminal"])
    assert status == 400 and "deny_tools" in body["error"]["message"]
    assert _open(api, auth, cwd)[0] == 201  # sans le paramètre : comportement d'avant


def test_deny_tools_reach_the_hook_and_are_validated(tmp_path):
    api, auth, cwd = _api(tmp_path)
    seen = []
    api.on_session_deny = lambda tid, names: seen.append((tid, names))
    status, body = _open(api, auth, cwd, deny_tools=["terminal", "bubble_update", "terminal"])
    assert status == 201 and seen == [(body["thread_id"], ["terminal", "bubble_update"])]
    assert _open(api, auth, cwd)[0] == 201 and seen[-1][1] == []  # sans liste : remplace (efface)
    for bad in ("terminal", [1], ["terminal(rm*)"], [""], ["x"] * 201):
        assert _open(api, auth, cwd, deny_tools=bad)[0] == 400, bad


def test_surfaces_gate_hides_bubble_tools_unless_the_host_has_bubbles(tmp_path):
    api, auth, cwd = _api(tmp_path)  # drapeau éteint : `surfaces` ignoré, rien de retiré
    seen = []
    api.on_session_deny = lambda tid, names: seen.append(names)
    _open(api, auth, cwd)
    _open(api, auth, cwd, surfaces=["chat"])
    assert seen == [[], []]
    api, auth, cwd = _api(tmp_path / "g", surfaces_gate=True)
    seen = []
    api.on_session_deny = lambda tid, names: seen.append(names)
    _open(api, auth, cwd)
    _open(api, auth, cwd, surfaces=["chat"])
    _open(api, auth, cwd, surfaces=["bubbles"], deny_tools=["terminal"])
    _open(api, auth, cwd, surfaces=["chat"], deny_tools=["bubble_show"])
    assert seen == [list(BUBBLE_TOOLS), list(BUBBLE_TOOLS), ["terminal"], ["bubble_show", "bubble_update"]]
    assert _open(api, auth, cwd, surfaces="bubbles")[0] == 400


def test_bubble_tool_names_exist_in_the_bubble_source():
    import re
    from pathlib import Path

    src = Path(__import__("nokker.bubbles.tools", fromlist=["x"]).__file__).read_text()
    names = set(re.findall(r'ToolSpec\(name="([a-z_]+)"', src))
    assert set(BUBBLE_TOOLS) <= names


@pytest.mark.parametrize("name", ["tool_find", "clarify"])
def test_core_names_can_be_hidden_too(name):
    reg = _registry()
    reg.hide_for_thread(FIL, [name])
    with tenant_scope(TN), thread_scope(FIL):
        assert reg.spec(name) is None


def test_reopening_an_existing_thread_with_deny_terminal_hides_it_from_catalog_prefix_and_calls(tmp_path):
    """Un fil existant rouvert (`resume`) avec `deny_tools: ["terminal"]` : plus au catalogue, plus nommé dans le
    préfixe, appel direct refusé (registre et règle `deny`). Le crochet est celui de `serve` (retrait + règle)."""
    from tests.unit.context.test_prefix_tools import _engine, _names, _system

    reg = _registry()
    perms = LocalPermissions(mode=Mode.AUTO)

    def hook(thread, names):  # même contenu que `on_session_deny` de nokker/api/server.py
        reg.hide_for_thread(thread, names)
        if names:
            perms.restrict_with(thread, {"deny": list(names)})

    api, auth, cwd = _api(tmp_path)
    api.on_session_deny = hook
    status, body = _open(api, auth, cwd)
    assert status == 201
    thread = body["thread_id"]
    eng = _engine(tmp_path, reg)
    with tenant_scope(TN), thread_scope(thread):
        assert "terminal" in {s.name for s in reg.core()} and reg.spec("terminal") is not None  # avant : offert
    assert _names(_system(eng, thread), "terminal")

    status, body = _open(api, auth, cwd, resume=thread, deny_tools=["terminal"])
    assert status == 201 and body["thread_id"] == thread  # le même fil, pas un nouveau

    # (a) catalogue offert au modèle
    with tenant_scope(TN), thread_scope(thread):
        assert "terminal" not in {s.name for s in reg.core()}
        assert "terminal" not in {n for n, _ in reg.index()} and reg.spec("terminal") is None
        assert "terminal" not in {s.name for s in reg.find("terminal")}
        # (c) appel direct : refusé par le registre, et par la règle `deny` des permissions
        res = reg.call("terminal", {"command": "ls"}, _ctx(thread))
        assert res.is_error and res.content.startswith("outil inconnu : terminal")
    decision, _, code = perms.decide_coded(ToolCall(id="1", name="terminal", arguments={"command": "ls"}),
                                           Effect.DESTRUCTIVE, thread)
    assert decision is Decision.DENY and code == "deny_rule"
    # (b) préfixe de code rendu pour ce fil
    prefix = _system(eng, thread)
    assert not _names(prefix, "terminal") and "ne la lance pas." in prefix
    assert _names(_system(eng, "th_autre"), "terminal")  # un autre fil garde `terminal`
