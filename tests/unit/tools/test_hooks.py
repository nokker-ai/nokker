"""Hooks des sessions de code (nokker/tools/hooks.py, lot X3 de Nokker Lokkal).

Partie 1, toujours jouée : filtre `outil(motif)`, blocage par `pre_tool`, note de `post_tool`, délai, charge utile
caviardée, hooks du dépôt refusés puis acceptés après confiance (et de nouveau refusés si le bloc change), mode plan,
sous-agents, drapeau éteint, route de confiance, boucle du noyau et `nokker hooks`.
Partie 2 : une vraie commande (amont, backend local), et le bac à sable docker avec NOKKER_TEST_DOCKER_ROOT."""
from __future__ import annotations

import importlib.util
import json
import os
import time

import pytest

from nokker.core.ports import ToolContext
from nokker.core.turn import Message, ToolResult, TurnInput
from nokker.qa.permissions import Mode
from nokker.tools.hooks import HookRunner, hooks_sha256, parse_hooks

UPSTREAM = importlib.util.find_spec("tools") is not None and importlib.util.find_spec("tools.file_operations") is not None
needs_upstream = pytest.mark.skipif(not UPSTREAM, reason="amont (tools.file_operations) non importable")
TH = "th_hooks"
CTX = ToolContext(thread_id=TH, turn_index=0, channel="api")


class FakeSandbox:
    """Bac à sable scripté : (code, sortie) par commande ; garde ce qui a été lancé et la charge utile reçue."""

    def __init__(self, results=None):
        self.results, self.ran = dict(results or {}), []

    def __call__(self, thread_id, command, stdin, timeout):
        self.ran.append({"thread_id": thread_id, "command": command, "payload": json.loads(stdin), "timeout": timeout})
        return self.results.get(command, (0, ""))


class FakeTools:
    def __init__(self, content="ok"):
        self.content, self.called = content, []

    def spec(self, name):
        return None

    def call(self, name, arguments, ctx):
        self.called.append(name)
        return ToolResult(call_id="", name=name, content=self.content)


def _settings(path, hooks=None, **extra):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**({"hooks": hooks} if hooks is not None else {}), **extra}))
    return path


def _runner(tmp_path, sandbox, hooks=None, mode=Mode.ACCEPT_EDITS, **kw):
    events = []
    r = HookRunner(sandbox=sandbox, user_settings=_settings(tmp_path / "user" / "settings.json", hooks),
                   scrub=lambda s: s.replace("hunter2", "[secret:pw]"), mode_of=lambda t: mode,
                   emit=lambda n, d: events.append((n, d)), background=False, **kw)
    return r, events


def _repo(tmp_path, hooks):
    repo = tmp_path / "repo"
    _settings(repo / ".nokker" / "settings.json", hooks, permissions={"deny": []})
    return repo


# ─────────────────────────────── réglages ───────────────────────────────


def test_parse_and_filter_use_the_permission_rule_syntax():
    hooks = parse_hooks({"pre_tool": [{"match": "terminal(npm test*)", "command": "echo a", "timeout_s": 9999}],
                         "post_tool": [{"command": "echo b"}]}, "user")
    pre, post = hooks
    assert pre.timeout_s == 300 and post.match == "*" and post.timeout_s == 30
    assert pre.matches("terminal", {"command": "npm test -- -x"})
    assert not pre.matches("terminal", {"command": "npm run build"}) and not pre.matches("write_file", {"path": "a"})
    assert post.matches("write_file", {"path": "x"}) and post.matches("read_file", {})
    path_hook = parse_hooks({"post_tool": [{"match": "write_file(src/**)", "command": "x"}]}, "user")[0]
    assert path_hook.matches("write_file", {"path": "./src/a/b.py"}) and not path_hook.matches("write_file",
                                                                                              {"path": "../src/a"})
    for bad in ({"pre_tool": [{"match": "terminal(x"}]}, {"pre_tool": [{"command": ""}]}, {"avant": []},
                {"pre_tool": {"command": "x"}}, {"subagents": "oui"}, ["x"]):
        with pytest.raises(ValueError):
            parse_hooks(bad, "user")
    assert hooks_sha256({"b": 1, "a": [2]}) == hooks_sha256({"a": [2], "b": 1})  # JSON canonique


# ─────────────────────────────── pre_tool, post_tool, délai ───────────────────────────────


def test_pre_tool_nonzero_blocks_the_tool_with_the_hook_reason(tmp_path):
    sb = FakeSandbox({"./check": (3, "branche main protégée\n")})
    runner, events = _runner(tmp_path, sb, {"pre_tool": [{"match": "terminal(git push*)", "command": "./check"}]})
    runner.open_session(TH, str(tmp_path))
    tools = FakeTools()
    wrapped = runner.wrap(tools)
    r = wrapped.call("terminal", {"command": "git push origin main"}, CTX)
    assert r.is_error and "BLOQUÉ par un hook pre_tool" in r.content and "branche main protégée" in r.content
    assert "code 3" in r.content and tools.called == []
    assert wrapped.call("terminal", {"command": "git status"}, CTX).content == "ok"  # pas couvert : aucun hook
    assert tools.called == ["terminal"] and len(sb.ran) == 1
    assert sb.ran[0]["payload"] == {"event": "pre_tool", "thread_id": TH, "tool": "terminal",
                                    "arguments": {"command": "git push origin main"}}
    ran = [d for n, d in events if n == "hook.ran"]
    assert ran == [{"thread_id": TH, "event": "pre_tool", "match": "terminal(git push*)", "source": "user",
                    "exit_code": 3, "duration_ms": ran[0]["duration_ms"], "timed_out": False}]
    sb.results["./check"] = (0, "")
    assert wrapped.call("terminal", {"command": "git push origin feat"}, CTX).content == "ok"  # code 0 : l'appel part


def test_post_tool_output_is_a_note_and_the_payload_is_scrubbed(tmp_path):
    sb = FakeSandbox({"ruff": (1, "src/a.py:1: F401 inutilisé")})
    runner, _ = _runner(tmp_path, sb, {"post_tool": [{"match": "write_file(src/**)", "command": "ruff"}]})
    runner.open_session(TH, str(tmp_path))
    tools = FakeTools(content='{"bytes": 12, "note": "hunter2"}')
    r = runner.wrap(tools).call("write_file", {"path": "src/a.py", "content": "x = 'hunter2'"}, CTX)
    assert not r.is_error and r.content.startswith('{"bytes": 12')
    assert "[hook post_tool write_file(src/**), code 1]\nsrc/a.py:1: F401 inutilisé" in r.content
    payload = sb.ran[0]["payload"]
    assert payload["arguments"]["content"] == "x = '[secret:pw]'"  # jamais une valeur du coffre vers le hook
    assert payload["result"] == {"content": '{"bytes": 12, "note": "[secret:pw]"}', "is_error": False}
    silent = FakeSandbox()
    runner2, _ = _runner(tmp_path, silent, {"post_tool": [{"command": "true"}]})
    runner2.open_session(TH, str(tmp_path))
    assert runner2.wrap(FakeTools()).call("read_file", {"path": "a"}, CTX).content == "ok"  # code 0, rien dit : rien


def test_timeout_blocks_and_is_reported(tmp_path):
    sb = FakeSandbox({"sleep 60": (124, "")})
    runner, events = _runner(tmp_path, sb, {"pre_tool": [{"command": "sleep 60", "timeout_s": 2}]})
    runner.open_session(TH, str(tmp_path))
    r = runner.wrap(FakeTools()).call("write_file", {"path": "a"}, CTX)
    assert r.is_error and "délai dépassé" in r.content and sb.ran[0]["timeout"] == 2
    assert [d["timed_out"] for n, d in events if n == "hook.ran"] == [True]


def test_a_sandbox_failure_blocks_pre_tool_fail_closed(tmp_path):
    def broken(*a):
        raise RuntimeError("docker absent")

    runner, _ = _runner(tmp_path, broken, {"pre_tool": [{"command": "x"}], "post_tool": [{"command": "y"}]})
    runner.open_session(TH, str(tmp_path))
    tools = FakeTools()
    r = runner.wrap(tools).call("write_file", {"path": "a"}, CTX)
    assert r.is_error and "docker absent" in r.content and tools.called == []


def test_session_start_and_turn_end_are_reported_as_events(tmp_path):
    sb = FakeSandbox({"git status --short": (0, " M a.py\n"), "notify": (0, "envoyé")})
    runner, events = _runner(tmp_path, sb, {"session_start": [{"command": "git status --short"}],
                                            "turn_end": [{"command": "notify"}]})
    runner.open_session(TH, str(tmp_path))

    class T:
        thread_id, index, error, reply = TH, 4, None, "Fait : hunter2 changé."

    runner.turn_end(T())
    ran = [d for n, d in events if n == "hook.ran"]
    assert [(d["event"], d["exit_code"], d["output"]) for d in ran] == [("session_start", 0, " M a.py\n"),
                                                                       ("turn_end", 0, "envoyé")]
    assert sb.ran[1]["payload"] == {"event": "turn_end", "thread_id": TH, "turn_index": 4, "error": None,
                                    "reply": "Fait : [secret:pw] changé."}


# ─────────────────────────────── confiance du dépôt ───────────────────────────────


def test_repo_hooks_refused_then_accepted_after_trust_then_refused_when_changed(tmp_path):
    sb = FakeSandbox({"./lint": (1, "lint KO")})
    block = {"post_tool": [{"match": "write_file", "command": "./lint"}]}
    repo = _repo(tmp_path, block)
    runner, events = _runner(tmp_path, sb)
    state = runner.open_session(TH, str(repo))
    sha = hooks_sha256(block)
    assert state["project"] == "untrusted" and state["sha256"] == sha and sha[:12] in state["notice"]
    assert state["project_hooks"] == [{"event": "post_tool", "match": "write_file", "command": "./lint"}]
    assert [n for n, _ in events] == ["hook.untrusted"]
    wrapped = runner.wrap(FakeTools())
    assert wrapped.call("write_file", {"path": "a"}, CTX).content == "ok" and sb.ran == []  # ignorés

    assert runner.trust(TH, "0" * 64)[0] == 409  # pas l'empreinte relue
    assert runner.trust("th_inconnu", sha)[0] == 404
    status, body = runner.trust(TH, sha)
    assert status == 200 and body["trusted"] and body["root"] == os.path.realpath(repo)
    user = json.loads((tmp_path / "user" / "settings.json").read_text())
    assert user["trusted_project_hooks"] == [{"root": os.path.realpath(repo), "sha256": sha}]
    assert "lint KO" in wrapped.call("write_file", {"path": "a"}, CTX).content  # actifs pour ce fil, tout de suite
    assert runner.open_session("th_2", str(repo))["project"] == "trusted"  # même dossier, même empreinte

    block2 = {"post_tool": [{"match": "write_file", "command": "./lint && curl evil"}]}
    _settings(repo / ".nokker" / "settings.json", block2)
    again = runner.open_session("th_3", str(repo))
    assert again["project"] == "untrusted" and again["sha256"] == hooks_sha256(block2)
    other = tmp_path / "copie"
    _settings(other / ".nokker" / "settings.json", block)
    assert runner.open_session("th_4", str(other))["project"] == "untrusted"  # même bloc, autre dossier


def test_repo_cannot_trust_itself_and_mid_session_edits_do_not_count(tmp_path):
    block = {"pre_tool": [{"command": "./x"}]}
    repo = tmp_path / "repo"
    # Le dépôt écrit sa propre confiance : seule celle des réglages de l'UTILISATEUR compte.
    _settings(repo / ".nokker" / "settings.json", block,
              trusted_project_hooks=[{"root": os.path.realpath(repo), "sha256": hooks_sha256(block)}])
    sb = FakeSandbox()
    runner, _ = _runner(tmp_path, sb)
    assert runner.open_session(TH, str(repo))["project"] == "untrusted"
    # Un bloc ajouté pendant la session (par le modèle, en accept_edits) : lu à l'ouverture seulement.
    runner2, _ = _runner(tmp_path, sb)
    runner2.open_session("th_b", str(tmp_path / "vide"))
    _settings(tmp_path / "vide" / ".nokker" / "settings.json", {"pre_tool": [{"command": "./y"}]})
    runner2.wrap(FakeTools()).call("write_file", {"path": "a"}, ToolContext(thread_id="th_b", turn_index=0,
                                                                            channel="api"))
    assert sb.ran == []


def test_invalid_blocks_are_ignored_whole_with_a_notice(tmp_path):
    repo = _repo(tmp_path, {"pre_tool": [{"match": "terminal(", "command": "x"}]})
    sb = FakeSandbox()
    runner, _ = _runner(tmp_path, sb, {"pre_tool": "pas une liste"})
    state = runner.open_session(TH, str(repo))
    assert state["project"] == "invalid" and state["user"] == 0
    assert "utilisateur ignorés" in state["notice"] and "dépôt ignorés" in state["notice"]


# ─────────────────────────────── plan, sous-agents ───────────────────────────────


def test_no_hook_in_plan_mode(tmp_path):
    sb = FakeSandbox({"x": (1, "non")})
    runner, _ = _runner(tmp_path, sb, {"session_start": [{"command": "x"}], "pre_tool": [{"command": "x"}]},
                        mode=Mode.PLAN)
    runner.open_session(TH, str(tmp_path))
    assert runner.wrap(FakeTools()).call("read_file", {"path": "a"}, CTX).content == "ok" and sb.ran == []


def test_subagents_only_with_explicit_inheritance(tmp_path):
    child = ToolContext(thread_id=f"{TH}/sa-ab12", turn_index=0, channel="subagent")
    sb = FakeSandbox({"x": (1, "non")})
    runner, _ = _runner(tmp_path, sb, {"pre_tool": [{"command": "x"}]})
    runner.open_session(TH, str(tmp_path))
    assert runner.wrap(FakeTools()).call("write_file", {"path": "a"}, child).content == "ok" and sb.ran == []
    runner2, _ = _runner(tmp_path, sb, {"pre_tool": [{"command": "x"}], "subagents": True})
    runner2.open_session(TH, str(tmp_path))
    assert runner2.wrap(FakeTools()).call("write_file", {"path": "a"}, child).is_error
    assert sb.ran[0]["thread_id"] == f"{TH}/sa-ab12"


def test_threads_without_a_session_get_no_hooks(tmp_path):
    sb = FakeSandbox({"x": (1, "non")})
    runner, _ = _runner(tmp_path, sb, {"pre_tool": [{"command": "x"}]})
    other = ToolContext(thread_id="telegram:42", turn_index=0, channel="telegram")
    assert runner.wrap(FakeTools()).call("write_file", {"path": "a"}, other).content == "ok" and sb.ran == []


# ─────────────────────────────── noyau ───────────────────────────────


def test_kernel_loop_blocks_with_pre_tool_and_runs_turn_end(tmp_path):
    from tests.unit.test_loop_contract import kernel, tc

    sb = FakeSandbox({"guard": (2, "pas d'agenda le dimanche"), "fin": (0, "")})
    runner, events = _runner(tmp_path, sb, {"pre_tool": [{"match": "calendar_list", "command": "guard"}],
                                            "turn_end": [{"command": "fin"}]})
    k, tools, *_ = kernel([Message("assistant", tool_calls=[tc("calendar_list")]), Message("assistant", "bloqué.")])
    k.tools = runner.wrap(k.tools)
    runner.open_session("t1", str(tmp_path))
    turn = k.run_turn(TurnInput("t1", "mon agenda ?"))
    assert tools.called == []  # le hook a bloqué, l'outil n'a pas tourné
    tool_msg = next(m for m in turn.messages if m.role == "tool")
    assert "pas d'agenda le dimanche" in tool_msg.content
    assert [d["event"] for n, d in events if n == "hook.ran"] == ["pre_tool", "turn_end"]
    assert sb.ran[-1]["payload"]["reply"] == "bloqué."


def test_a_call_waiting_for_a_card_does_not_run_pre_tool(tmp_path):
    from tests.unit.test_loop_contract import kernel, tc

    sb = FakeSandbox()
    runner, _ = _runner(tmp_path, sb, {"pre_tool": [{"command": "x"}]})
    k, tools, *_ = kernel([Message("assistant", tool_calls=[tc("infra_lvextend")]), Message("assistant", "attente")])
    k.tools = runner.wrap(k.tools)
    runner.open_session("t1", str(tmp_path))
    turn = k.run_turn(TurnInput("t1", "agrandis le volume"))
    assert [c.name for c in turn.pending_approvals] == ["infra_lvextend"] and sb.ran == [] and tools.called == []


# ─────────────────────────────── API locale et drapeau ───────────────────────────────


def _api(tmp_path, **local):
    from nokker.local.api import LocalApi

    root = tmp_path / "projets"
    (root / "app").mkdir(parents=True, exist_ok=True)
    return LocalApi.from_config({"token_path": str(tmp_path / "tok"), "settings_path": str(tmp_path / "s.json"),
                                 "code": {"roots": [str(root)], "backend": "local"}, **local}, port=8742)


def test_flag_off_by_default_no_hooks_key_and_trust_route_404(tmp_path):
    api = _api(tmp_path)
    assert api.hooks_enabled is False and api.hooks is None
    auth = f"Bearer {api.token}"
    status, body = api.handle("POST", "/v1/local/sessions", auth, {"cwd": str(tmp_path / "projets" / "app")})
    assert status == 201 and "hooks" not in body
    assert api.handle("POST", "/v1/local/hooks/trust", auth, {"thread_id": body["thread_id"]})[0] == 404
    assert _api(tmp_path, hooks=True).hooks_enabled is True


def test_session_response_carries_the_notice_and_trust_is_local_only(tmp_path):
    api = _api(tmp_path, hooks=True)
    repo = tmp_path / "projets" / "app"
    block = {"pre_tool": [{"command": "./guard"}]}
    _settings(repo / ".nokker" / "settings.json", block)
    sb = FakeSandbox()
    api.hooks = HookRunner(sandbox=sb, user_settings=api.settings_path, background=False)
    api.on_session_open = lambda t, cwd: {"hooks": api.hooks.open_session(t, cwd)}
    auth = f"Bearer {api.token}"
    status, body = api.handle("POST", "/v1/local/sessions", auth, {"cwd": str(repo)})
    assert status == 201 and body["hooks"]["project"] == "untrusted" and body["hooks"]["notice"]
    th, sha = body["thread_id"], body["hooks"]["sha256"]
    req = {"thread_id": th, "sha256": sha}
    assert api.handle("POST", "/v1/local/hooks/trust", auth, req, relayed=True)[0] == 401  # jeton local refusé relayé
    api.relayed_owner = lambda a: {"sub": "us_x"}  # même un propriétaire relayé ne fait pas confiance à distance
    assert api.handle("POST", "/v1/local/hooks/trust", "Bearer jwt", req, relayed=True)[0] == 403
    assert api.handle("POST", "/v1/local/hooks/trust", auth, req) == (200, {
        "thread_id": th, "root": os.path.realpath(repo), "sha256": sha, "trusted": True})
    assert json.loads(api.settings_path.read_text())["trusted_project_hooks"][0]["sha256"] == sha


def test_session_code_tools_run_hooks_in_the_session_of_the_parent_thread(tmp_path):
    from nokker.local.api import SessionCodeTools

    seen = []

    class Src:
        def run_hook(self, thread_id, command, stdin, timeout):
            seen.append(thread_id)
            return 0, ""

        def close(self):
            pass

    tools = SessionCodeTools(backend="local")
    tools._by_thread["th_p"] = Src()
    assert tools.run_hook("th_p/sa-1", "x", "{}", 5) == (0, "") and seen == ["th_p"]
    with pytest.raises(LookupError):
        tools.run_hook("th_inconnu", "x", "{}", 5)


# ─────────────────────────────── nokker hooks ───────────────────────────────


def test_cli_show_and_trust_require_the_reread_fingerprint(tmp_path, capsys):
    from nokker.cli import main

    block = {"post_tool": [{"command": "make lint"}]}
    repo = _repo(tmp_path, block)
    settings = tmp_path / "u.json"
    assert main(["hooks", "show", "--workspace", str(repo), "--settings", str(settings)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["trusted"] is False and shown["sha256"] == hooks_sha256(block)
    assert main(["hooks", "trust", "--workspace", str(repo), "--settings", str(settings), "--sha256", "abc"]) == 1
    assert not settings.exists()
    assert main(["hooks", "trust", "--workspace", str(repo), "--settings", str(settings),
                 "--sha256", shown["sha256"]]) == 0
    capsys.readouterr()
    assert main(["hooks", "show", "--workspace", str(repo), "--settings", str(settings)]) == 0
    assert json.loads(capsys.readouterr().out)["trusted"] is True


# ─────────────────────────────── vraies commandes ───────────────────────────────


@needs_upstream
def test_real_command_gets_the_payload_on_stdin_and_is_bounded(tmp_path, monkeypatch):
    from nokker.tools.code import CodeToolSource

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    work = tmp_path / "w"
    work.mkdir()
    src = CodeToolSource(root=str(work), backend="local")
    try:
        code, out = src.run_hook(TH, "python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[\"tool\"]); "
                                     "sys.exit(7)'", json.dumps({"tool": "terminal"}), 20)
        assert code == 7 and out.strip().endswith("terminal")
        t0 = time.monotonic()
        code, _ = src.run_hook(TH, "sleep 30", "{}", 2)
        assert code == 124 and time.monotonic() - t0 < 15
    finally:
        src.close()


@needs_upstream
def test_hardline_hook_never_runs(tmp_path):
    sb = FakeSandbox()
    runner, _ = _runner(tmp_path, sb, {"pre_tool": [{"command": "rm -rf /"}]})
    runner.open_session(TH, str(tmp_path))
    r = runner.wrap(FakeTools()).call("write_file", {"path": "a"}, CTX)
    assert r.is_error and "jamais exécutée" in r.content and sb.ran == []


@pytest.mark.skipif(not (UPSTREAM and os.environ.get("NOKKER_TEST_DOCKER_ROOT")),
                    reason="NOKKER_TEST_DOCKER_ROOT absent ou amont non importable")
def test_hooks_run_inside_the_docker_sandbox(tmp_path, monkeypatch):
    import shutil
    import uuid

    from nokker.tools import code

    base = os.environ["NOKKER_TEST_DOCKER_ROOT"]
    work = os.path.join(base, f"hk-{uuid.uuid4().hex[:8]}")
    os.makedirs(work)
    monkeypatch.setenv("HERMES_HOME", os.path.join(base, ".hermes"))
    src = code.CodeToolSource(root=work, backend="docker",
                              image=os.environ.get("NOKKER_TEST_SANDBOX_IMAGE", code.DEFAULT_SANDBOX_IMAGE))
    runner, _ = _runner(tmp_path, src.run_hook, {"post_tool": [
        {"match": "write_file", "command": "pwd; test -S /var/run/docker.sock && echo SOCK; cat"}]})
    ctx = ToolContext(thread_id="t-hk-docker", turn_index=0, channel="api")
    runner.open_session("t-hk-docker", work)
    try:
        r = runner.wrap(src).call("write_file", {"path": "a.txt", "content": "bonjour"}, ctx)
        note = r.content.split("[hook post_tool write_file, code 0]", 1)[1]
        assert note.strip().startswith("/workspace") and "SOCK" not in note  # dans le bac, rien de l'hôte
        assert '"tool": "write_file"' in note and work not in note
    finally:
        src.close()
        shutil.rmtree(work, ignore_errors=True)
