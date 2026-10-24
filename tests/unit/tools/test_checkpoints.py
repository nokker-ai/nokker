"""Points de retour (nokker/tools/checkpoints.py, Nokker Lokkal L3).

Partie 1, toujours jouée (sans Hermes) : schémas, effets, enveloppe transparente (faux inner, faux gestionnaire) —
ordre new_turn → ensure_checkpoint → appel → record_agent_write, rien pour un READ, traduction /workspace, un
gestionnaire qui lève n'empêche pas l'appel.
Partie 2, si Hermes est importable (image nokker-lokkal-dev) : vrai CheckpointManager sous un HOME temporaire."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess

import pytest

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.tools import checkpoints as cp
from nokker.tools.checkpoints import (CHECKPOINT_TOOLS, CheckpointToolSource, CheckpointingToolSource, host_path)
from nokker.tools.select import FAMILIES, compact_index, relevant_tools

HERMES = importlib.util.find_spec("tools") is not None and \
    importlib.util.find_spec("tools.checkpoint_manager") is not None
needs_hermes = pytest.mark.skipif(not HERMES, reason="Hermes (tools.checkpoint_manager) non importable")


def ctx(turn=0, tid="t1"):
    return ToolContext(thread_id=tid, turn_index=turn, channel="api")


class FakeInner:
    def __init__(self, log, fail=False):
        self.log, self.fail, self.closed, self.done = log, fail, False, []
        self._specs = [ToolSpec("read_file", "r", {}, Effect.READ), ToolSpec("write_file", "w", {}, Effect.WRITE),
                       ToolSpec("patch", "p", {}, Effect.WRITE),
                       ToolSpec("terminal", "t", {}, Effect.DESTRUCTIVE,
                                effect_of=lambda a: Effect.READ if a.get("command") == "ls" else Effect.WRITE)]

    def specs(self):
        return self._specs

    def call(self, name, arguments, c):
        self.log.append(("call", name))
        return ToolResult(call_id="", name=name, content="boom" if self.fail else "ok", is_error=self.fail)

    def turn_done(self, tid):
        self.done.append(tid)

    def close(self):
        self.closed = True


class FakeManager:
    def __init__(self, log, boom=False):
        self.log, self.boom = log, boom

    def new_turn(self):
        self.log.append(("new_turn",))

    def ensure_checkpoint(self, root, reason):
        self.log.append(("ensure", root, reason))
        if self.boom:
            raise RuntimeError("git cassé")

    def record_agent_write(self, path):
        self.log.append(("record", path))
        if self.boom:
            raise RuntimeError("ledger cassé")


def wrap(tmp_path, log, boom=False, inner=None):
    return CheckpointingToolSource(inner or FakeInner(log), lambda tid: tmp_path, manager=FakeManager(log, boom))


def test_specs_and_effects():
    specs = {s.name: s for s in CheckpointToolSource(lambda t: None, manager=object()).specs()}
    assert tuple(specs) == CHECKPOINT_TOOLS
    assert {n: s.effect for n, s in specs.items()} == {
        "checkpoint_create": Effect.WRITE, "checkpoint_list": Effect.READ, "checkpoint_diff": Effect.READ,
        "checkpoint_restore": Effect.DESTRUCTIVE}
    assert all(not s.core and s.source == "builtin:checkpoints" for s in specs.values())
    assert specs["checkpoint_restore"].parameters["required"] == ["hash"]
    blob = json.dumps([s.description for s in specs.values()] + [s.parameters for s in specs.values()])
    assert "hermes" not in blob.lower()


def test_family_in_select():
    assert any(f.label == "Points de retour" for f in FAMILIES)
    names = list(CHECKPOINT_TOOLS)
    assert "Points de retour" in compact_index(names)
    specs = CheckpointToolSource(lambda t: None, manager=object()).specs()
    for text in ("fais un rollback", "reviens en arrière", "annule les modifications", "restore the checkpoint"):
        assert {s.name for s in relevant_tools(text, set(), specs)} == set(CHECKPOINT_TOOLS), text
    assert relevant_tools("bonjour", set(), specs) == []


def test_without_hermes_tools_answer_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "make_manager", lambda home=None, **k: None)
    src = CheckpointToolSource(lambda t: tmp_path)
    for name in CHECKPOINT_TOOLS:
        r = src.call(name, {"hash": "abcdef0"}, ctx())
        assert r.is_error and r.content == "points de retour indisponibles"
    assert src.call("inconnu", {}, ctx()).is_error
    log = []
    w = CheckpointingToolSource(FakeInner(log), lambda t: tmp_path, manager=None)
    w.manager = None
    assert not w.call("write_file", {"path": "a"}, ctx()).is_error
    assert log == [("call", "write_file")]  # transparente


def test_auto_prune_without_hermes(monkeypatch):
    monkeypatch.setattr(cp, "_hermes_checkpoints", lambda home=None: None)
    r = cp.auto_prune()
    assert r["skipped"] and "indisponibles" in r["error"]


def test_order_new_turn_ensure_call_record(tmp_path):
    log = []
    w = wrap(tmp_path, log)
    w.call("write_file", {"path": "/workspace/src/a.py", "content": "x"}, ctx(0))
    assert log == [("new_turn",), ("ensure", str(tmp_path), "avant write_file"), ("call", "write_file"),
                   ("record", str(tmp_path / "src" / "a.py"))]


def test_new_turn_only_when_turn_index_changes(tmp_path):
    log = []
    w = wrap(tmp_path, log)
    w.call("write_file", {"path": "a"}, ctx(0))
    w.call("write_file", {"path": "b"}, ctx(0))
    w.call("write_file", {"path": "c"}, ctx(1))
    w.call("write_file", {"path": "d"}, ctx(0, tid="autre"))
    assert [e[0] for e in log].count("new_turn") == 3
    assert log.index(("new_turn",)) == 0


def test_read_takes_no_checkpoint_and_terminal_is_classified_per_call(tmp_path):
    log = []
    w = wrap(tmp_path, log)
    w.call("read_file", {"path": "a"}, ctx())
    w.call("terminal", {"command": "ls"}, ctx())
    assert not any(e[0] == "ensure" for e in log)
    w.call("terminal", {"command": "make"}, ctx())
    assert ("ensure", str(tmp_path), "avant terminal") in log
    assert not any(e[0] == "record" for e in log)  # terminal : pas de ledger


def test_patch_modes_record_written_paths(tmp_path):
    log = []
    w = wrap(tmp_path, log)
    w.call("patch", {"mode": "replace", "path": "x.py", "old_string": "a", "new_string": "b"}, ctx())
    w.call("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Update File: /workspace/m.py\n@@\n-a\n+b\n"
                     "*** Add File: n.py\n+z\n*** End Patch"}, ctx())
    assert [e[1] for e in log if e[0] == "record"] == [str(tmp_path / "x.py"), str(tmp_path / "m.py"),
                                                       str(tmp_path / "n.py")]


def test_failed_write_is_not_recorded(tmp_path):
    log = []
    w = wrap(tmp_path, log, inner=FakeInner(log, fail=True))
    assert w.call("write_file", {"path": "a"}, ctx()).is_error
    assert ("ensure", str(tmp_path), "avant write_file") in log
    assert not any(e[0] == "record" for e in log)


def test_manager_that_raises_never_blocks_the_tool(tmp_path):
    log = []
    w = wrap(tmp_path, log, boom=True)
    r = w.call("write_file", {"path": "a"}, ctx())
    assert not r.is_error and r.content == "ok" and ("call", "write_file") in log


def test_unknown_root_means_no_capture(tmp_path):
    log = []
    w = CheckpointingToolSource(FakeInner(log), lambda t: None, manager=FakeManager(log))
    assert not w.call("write_file", {"path": "a"}, ctx()).is_error
    assert log == [("call", "write_file")]


def test_delegation_of_specs_turn_done_close(tmp_path):
    log = []
    inner = FakeInner(log)
    w = wrap(tmp_path, log, inner=inner)
    assert w.specs() is inner.specs()
    w.turn_done("t1")
    w.close()
    assert inner.done == ["t1"] and inner.closed


def test_host_path(tmp_path):
    assert host_path(tmp_path, "/workspace/a/b") == str(tmp_path / "a" / "b")
    assert host_path(tmp_path, "/workspace") == str(tmp_path)
    assert host_path(tmp_path, "rel/x") == str(tmp_path / "rel" / "x")
    assert host_path(tmp_path, "/etc/x") == "/etc/x"


def test_hooks_env_wrapper_appends_config():
    wrapped = cp._no_user_hooks(lambda *a, **k: {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b",
                                                  "GIT_CONFIG_VALUE_0": "c"})
    env = wrapped()
    assert env["GIT_CONFIG_COUNT"] == "2" and env["GIT_CONFIG_KEY_1"] == "core.hooksPath"
    assert env["GIT_CONFIG_VALUE_1"] == os.devnull and env["GIT_CONFIG_KEY_0"] == "a.b"
    assert cp._no_user_hooks(lambda: {})()["GIT_CONFIG_COUNT"] == "1"


# ─────────────────────────────── avec Hermes ───────────────────────────────


@pytest.fixture
def real(tmp_path, monkeypatch):
    home = tmp_path / "nokker-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))  # ensure_checkpoint refuse le HOME lui-même
    work = tmp_path / "proj"
    work.mkdir()
    (work / "a.txt").write_text("un\n")
    (work / "b.txt").write_text("deux\n")
    wrapper, tools = cp.checkpoint_sources(FakeInner([]), lambda tid: work, home=home)
    assert wrapper.manager is not None
    return work, wrapper, tools


def _j(r):
    assert not r.is_error, r.content
    return json.loads(r.content)


@needs_hermes
def test_real_create_modify_list_diff_restore(real):
    work, _, tools = real
    c = ctx()
    created = _j(tools.call("checkpoint_create", {"reason": "départ"}, c))
    assert created["created"] and created["reason"] == "départ"
    (work / "a.txt").write_text("modifié\n")
    (work / "c.txt").write_text("nouveau\n")
    listing = _j(tools.call("checkpoint_list", {}, c))
    assert [e["hash"] for e in listing] == [created["hash"]]
    assert set(listing[0]) >= {"hash", "short_hash", "timestamp", "reason", "files_changed", "insertions", "deletions"}
    d = _j(tools.call("checkpoint_diff", {"hash": created["hash"]}, c))
    assert "modifié" in d["diff"] and "c.txt" in d["diff"]
    assert "modifié" in _j(tools.call("checkpoint_diff", {}, c))["diff"]  # diff de session
    r = _j(tools.call("checkpoint_restore", {"hash": created["hash"], "safe": False}, c))
    assert r["success"]
    assert (work / "a.txt").read_text() == "un\n"
    assert (work / "c.txt").exists()  # restore non sûr : ne supprime pas ce qui n'était pas dans le point
    # restore sûr : un fichier créé par l'agent (ledger) disparaît, un fichier de l'utilisateur reste
    _.manager.record_agent_write(str(work / "c.txt"))
    (work / "a.txt").write_text("encore\n")
    _.manager.record_agent_write(str(work / "a.txt"))
    r = _j(tools.call("checkpoint_restore", {"hash": created["hash"]}, c))
    assert (work / "a.txt").read_text() == "un\n" and not (work / "c.txt").exists(), r
    assert tools.call("checkpoint_restore", {"hash": "0" * 40}, c).is_error
    assert tools.call("checkpoint_restore", {}, c).is_error


@needs_hermes
def test_real_safe_restore_keeps_user_edits(real):
    work, wrapper, tools = real
    c = ctx()
    wrapper.call("write_file", {"path": "/workspace/a.txt", "content": "agent\n"}, c)  # FakeInner n'écrit pas
    (work / "a.txt").write_text("agent\n")  # ce que l'outil aurait écrit
    wrapper.manager.record_agent_write(str(work / "a.txt"))
    (work / "b.txt").write_text("humain\n")  # édition de l'utilisateur, jamais notée au ledger
    h = _j(tools.call("checkpoint_list", {}, c))[-1]["hash"]
    res = _j(tools.call("checkpoint_restore", {"hash": h}, c))
    assert (work / "a.txt").read_text() == "un\n"
    assert (work / "b.txt").read_text() == "humain\n" and "b.txt" in res["skipped_user_edits"]


@needs_hermes
def test_real_wrapper_captures_before_write_once_per_turn(real):
    work, wrapper, tools = real
    wrapper.call("write_file", {"path": "a.txt"}, ctx(0))
    (work / "a.txt").write_text("v2\n")
    wrapper.call("write_file", {"path": "a.txt"}, ctx(0))  # même tour : pas de second point
    assert len(_j(tools.call("checkpoint_list", {}, ctx()))) == 1
    wrapper.call("read_file", {"path": "a.txt"}, ctx(1))
    assert len(_j(tools.call("checkpoint_list", {}, ctx()))) == 1
    wrapper.call("write_file", {"path": "a.txt"}, ctx(2))
    assert len(_j(tools.call("checkpoint_list", {}, ctx()))) == 2


@needs_hermes
def test_real_no_git_dir_written_in_the_users_folder(real):
    work, _, tools = real
    _j(tools.call("checkpoint_create", {}, ctx()))
    (work / "a.txt").write_text("x\n")
    tools.call("checkpoint_diff", {}, ctx())
    assert not (work / ".git").exists()
    assert sorted(p.name for p in work.iterdir()) == ["a.txt", "b.txt"]


@needs_hermes
def test_real_user_repo_hooks_never_run(real, tmp_path):
    work, _, tools = real
    marker = tmp_path / "HOOK-RAN"
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(["git", "init", "-q", str(work)], check=True, env=env)
    for hook in ("pre-commit", "post-commit", "commit-msg", "pre-push", "post-checkout", "reference-transaction"):
        h = work / ".git" / "hooks" / hook
        h.write_text(f"#!/bin/sh\necho {hook} >> {marker}\n")
        h.chmod(0o755)
    (work / ".git" / "config").write_text("[core]\n\thooksPath = %s\n" % (work / ".git" / "hooks"))
    c = ctx()
    h = _j(tools.call("checkpoint_create", {"reason": "avec dépôt"}, c))["hash"]
    (work / "a.txt").write_text("autre\n")
    tools.call("checkpoint_diff", {"hash": h}, c)
    _j(tools.call("checkpoint_restore", {"hash": h, "safe": False}, c))
    assert (work / "a.txt").read_text() == "un\n"
    assert not marker.exists(), marker.read_text() if marker.exists() else ""


@needs_hermes
def test_real_hooks_path_env_is_effective_in_shadow_git(real, tmp_path):
    """Preuve directe : le git du gestionnaire voit core.hooksPath=/dev/null."""
    work, _, tools = real
    _j(tools.call("checkpoint_create", {}, ctx()))
    from tools import checkpoint_manager as cm

    ok, out, err = cm._run_git(["config", "--get", "core.hooksPath"], cm._store_path(), str(work))
    assert ok and out == os.devnull, (out, err)


@needs_hermes
def test_real_auto_prune_runs(real):
    r = cp.auto_prune(real[1].manager and None)
    assert "skipped" in r


def test_fr_reason():
    from nokker.tools.checkpoints import fr_reason
    assert fr_reason("pre-rollback snapshot (restoring to 55aa3d50e96104b5c81ab0cddd3d157dd915e766)") == "avant retour à 55aa3d5"
    assert fr_reason("auto") == "automatique"
    assert fr_reason("essai features") == "essai features"
