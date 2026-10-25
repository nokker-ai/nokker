"""Sous-agents (nokker/tools/delegate.py, Nokker Lokkal L3) : sans l'amont ni docker.

Partie 1 : faux `kernel_factory`, faux `run` git (cas de finalisation), repli relative-paths, concurrence, délai
d'inactivité, schéma de sortie, profondeur, exclude. Partie 2 : vrai git local (dépôt temporaire)."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

import pytest

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, InputCheck, LLMCall, Tier, ToolCall, ToolResult, Turn, Usage
from nokker.tools import delegate as dg
from nokker.tools.delegate import DelegateToolSource
from nokker.tools.registry import Registry
from nokker.tools.select import FAMILIES, relevant_tools

SPEC = lambda n, e=Effect.READ: ToolSpec(n, n, {}, e)  # noqa: E731


def ctx(tid="t1", turn=3):
    return ToolContext(thread_id=tid, turn_index=turn, channel="api", sender="alice")


class Src:
    def __init__(self, *names):
        self.names, self.calls, self.closed = names, [], False

    def specs(self):
        return [SPEC(n) for n in self.names]

    def call(self, name, args, c):
        self.calls.append((name, c.thread_id))
        return ToolResult(call_id="", name=name, content="ok")

    def close(self):
        self.closed = True


class FakeScoped(Src):
    def __init__(self, subdir):
        super().__init__("read_file", "write_file", "patch", "terminal")
        self.subdir = subdir


class FakeCode(Src):
    def __init__(self, root, run_impl=None, cwd="/workspace"):
        super().__init__("read_file", "write_file", "patch", "terminal")
        self.root, self.cwd, self.scopes = root, cwd, []
        self.cmds: list[str] = []
        self.run_impl = run_impl or (lambda cmd: (0, ""))
        outer = self

        class Env:
            def execute(self, cmd, timeout=60, **kw):
                outer.cmds.append(cmd)
                rc, out = outer.run_impl(cmd)
                return {"returncode": rc, "output": out}

        self.ws = type("WS", (), {"env": Env(), "cwd": cwd})()

    def workspace(self, tid):
        return self.ws

    def scoped(self, subdir):
        s = FakeScoped(subdir)
        self.scopes.append(s)
        return s


def turn(reply="fait", **kw):
    t = Turn(thread_id="x", index=0, input=InputCheck(text=""), reply=reply, **kw)
    t.calls = [LLMCall(tier=Tier.SHIP, model="m", reasoning=False, prefix_sha256="", layer_tokens={},
                       usage=Usage(prompt=10, completion=5), latency_s=0.1, route_reason="t", chf=0.01)]
    return t


class FakeKernel:
    def __init__(self, replies, factory):
        self.replies, self.factory, self.inputs, self.cancelled = replies, factory, [], []

    def cancel(self, thread_id):  # Kernel.cancel : arrêt du tour en cours du fil
        self.cancelled.append(thread_id)

    def run_turn(self, inp, stream=None):
        self.inputs.append(inp)
        return self.factory.behave(self, inp)


class Factory:
    """Faux kernel_factory à 3 arguments (registry, thread_id, observer)."""

    def __init__(self, replies=("fait",), behave=None):
        self.registries, self.threads, self.observers, self.kernels = [], [], [], []
        self.replies, self._behave = list(replies), behave
        self.lock = threading.Lock()

    def __call__(self, registry, thread_id, observer):
        with self.lock:
            self.registries.append(registry)
            self.threads.append(thread_id)
            self.observers.append(observer)
            k = FakeKernel(self.replies, self)
            self.kernels.append(k)
        return k

    def behave(self, kernel, inp):
        if self._behave:
            return self._behave(kernel, inp)
        with self.lock:
            reply = self.replies[min(len(kernel.inputs) - 1, len(self.replies) - 1)]
        return turn(reply)


def git_fake(state):
    """Faux `run` git : répond aux commandes reconnues selon `state`."""
    def run(cmd):
        state.setdefault("log", []).append(cmd)
        if "--version" in cmd:
            return 0, f"git version {state.get('version', '2.47.3')}\n"
        if "--show-toplevel" in cmd:
            return (0, "/workspace\n") if state.get("repo", True) else (128, "fatal: not a git repository")
        if "rev-parse HEAD" in cmd:
            return 0, "abc123\n"
        if "--git-common-dir" in cmd:
            return 0, "/workspace/.git\n"
        if "--absolute-git-dir" in cmd:
            return 0, "/workspace/.git/worktrees/x\n"
        if "worktree add" in cmd:
            return (0, "") if state.get("add_ok", True) else (128, "fatal")
        if "rev-list" in cmd:
            return (0, f"{state.get('commits', 0)}\n") if state.get("probe_ok", True) else (1, "")
        if "status" in cmd:
            return 0, state.get("status", "")
        if "worktree remove" in cmd:
            state["removed"] = True
            return 0, ""
        if cmd.startswith("test -e"):
            return (1, "") if state.get("removed") else (0, "")
        return 0, ""
    return run


def source(factory, code=None, **kw):
    code = code if code is not None else FakeCode("/nonexistent")
    src = DelegateToolSource(factory, code, **kw)
    base = Src("web_search", "send_mail", "mail_send", "clarify", "escalate_to_human", "memory_search",
               "memory_save", "session_send", "agent_draft", "checkpoint_create", "skill_view")
    reg = Registry(sources=[base, code, src])
    src.bind(reg)
    return src, base, code


def run(src, tasks, isolate=True, c=None):
    r = src.call("delegate", {"tasks": tasks, "isolate": isolate}, c or ctx())
    return r, (json.loads(r.content) if not r.is_error else None)


def names(reg):
    return {s.name for s in reg._all()}


# ─────────────────────────────── spec, effet, famille ───────────────────────────────


def test_spec_et_effet(tmp_path):
    src, _, code = source(Factory())
    spec = src.specs()[0]
    assert (spec.name, spec.core, spec.source, spec.effect) == ("delegate", False, "builtin:delegate", Effect.WRITE)
    code.root = str(tmp_path)
    assert spec.effect_for({"tasks": [], "isolate": False}) is Effect.DESTRUCTIVE  # agit dans le dossier réel
    assert spec.effect_for({"tasks": []}) is Effect.DESTRUCTIVE  # pas de dépôt : pas d'isolation possible
    (tmp_path / ".git").mkdir()
    assert spec.effect_for({"tasks": []}) is Effect.WRITE  # worktree jetable
    assert spec.effect_for({"tasks": [], "isolate": False}) is Effect.DESTRUCTIVE
    assert DelegateToolSource(Factory(), None).specs()[0].effect_for({}) is Effect.DESTRUCTIVE


def test_famille_sous_agents():
    assert any(f.label == "Sous-agents" for f in FAMILIES)
    spec = DelegateToolSource(Factory(), None).specs()[0]
    for text in ("délègue ça", "lance un sous-agent", "fais-le en parallèle", "fais faire la revue", "use a subagent",
                 "delegate this", "run in parallel"):
        assert relevant_tools(text, set(), [spec]) == [spec], text
    assert relevant_tools("bonjour", set(), [spec]) == []


# ─────────────────────────────── enfant : kernel, registre, événements ───────────────────────────────


def test_enfant_filtre_fil_canal_et_resultat():
    f = Factory(["Résumé OK"])
    events = []
    src, _, code = source(f, emit=lambda n, p: events.append((n, p)))
    st = {}
    code.run_impl = git_fake(st)
    r, res = run(src, [{"goal": "corrige le bug", "context": "voir a.py"}])
    assert not r.is_error and len(res) == 1
    e = res[0]
    assert (e["task_index"], e["status"], e["exit_reason"], e["summary"]) == (0, "completed", "completed", "Résumé OK")
    assert e["tokens"] == 15 and e["cost"] == 0.01
    assert e["thread_id"] == f"t1/sa-{e['child_id']}"
    k = f.kernels[0]
    inp = k.inputs[0]
    assert inp.thread_id == f.threads[0] and inp.channel == "subagent" and inp.sender == "alice"
    assert "corrige le bug" in inp.user_text and "Contexte :\nvoir a.py" in inp.user_text
    assert "ISOLATION WORKTREE" in inp.user_text and f"nokker/sa-{e['child_id']}" in inp.user_text
    got = names(f.registries[0])
    assert got == {"web_search", "memory_search", "skill_view", "read_file", "write_file", "patch", "terminal"}
    assert code.scopes and code.scopes[0].subdir == f".nokker/worktrees/{e['child_id']}" and code.scopes[0].closed
    # les outils du parent ne sont pas fermés par l'enfant
    assert not code.closed
    names_ev = [n for n, _ in events]
    assert names_ev[0] == "subagent.started" and names_ev[-1] == "subagent.finished"
    assert events[0][1]["parent_turn"] == 3 and events[0][1]["thread_id"] == "t1" and events[0][1]["goal"]
    assert events[-1][1] == {"parent_thread_id": "t1", "child_id": e["child_id"], "status": "completed",
                             "commits": 0, "dirty": False, "kept": False}
    assert events[0][1]["parent_thread_id"] == "t1"


def test_enfant_appelle_les_outils_filtres_vers_les_sources_du_parent():
    f = Factory()
    src, base, _ = source(f)
    run(src, [{"goal": "g"}], isolate=False)
    reg = f.registries[0]
    c = ToolContext(thread_id=f.threads[0], turn_index=0, channel="subagent")
    assert reg.call("web_search", {}, c).content == "ok" and base.calls == [("web_search", f.threads[0])]
    assert reg.call("mail_send", {}, c).is_error  # filtré : outil inconnu
    reg.close()
    assert not base.closed  # fermer le registre enfant ne ferme rien du parent


def test_reviewer_sans_ecriture():
    f = Factory()
    src, *_ = source(f)
    run(src, [{"goal": "relis", "role": "reviewer"}], isolate=False)
    got = names(f.registries[0])
    assert "write_file" not in got and "patch" not in got and "terminal" in got


def test_progress_relaye_et_observateur_parent_enveloppe():
    events, seen = [], []

    class Parent:
        def tool_result(self, t, call, result, ms):
            seen.append(call.name)

        def context_for(self, tid):
            return ["bloc"]

    def behave(kernel, inp):
        obs = f.observers[0]
        obs.tool_call(None, ToolCall("1", "read_file", {}))
        obs.tool_result(None, ToolCall("1", "read_file", {}), ToolResult("1", "read_file", "x"), 42)
        return turn()

    f = Factory(behave=behave)
    src, *_ = source(f, emit=lambda n, p: events.append((n, p)), parent_observer=Parent())
    run(src, [{"goal": "g"}], isolate=False)
    prog = [p for n, p in events if n == "subagent.progress"]
    assert prog[0]["parent_thread_id"] == "t1"
    assert len(prog) == 1 and prog[0]["tool"] == "read_file" and prog[0]["ms"] == 42 and prog[0]["child_id"]
    assert seen == ["read_file"] and f.observers[0].context_for("x") == ["bloc"]


def test_emit_qui_leve_ne_casse_rien():
    def boom(n, p):
        raise RuntimeError("x")

    src, *_ = source(Factory(), emit=boom)
    _, res = run(src, [{"goal": "g"}], isolate=False)
    assert res[0]["status"] == "completed"


def test_erreur_et_exception_de_l_enfant():
    def behave(kernel, inp):
        if "boom" in inp.user_text:
            raise RuntimeError("panne")
        return turn("", error="modèle indisponible")

    src, *_ = source(Factory(behave=behave))
    _, res = run(src, [{"goal": "boom"}, {"goal": "autre"}], isolate=False)
    assert res[0]["status"] == "failed" and res[0]["exit_reason"] == "exception" and "panne" in res[0]["summary"]
    assert res[1]["status"] == "failed" and res[1]["exit_reason"] == "error"


def test_carte_d_approbation_en_attente():
    def behave(kernel, inp):
        return turn("j'attends", pending_approvals=[ToolCall("1", "terminal", {"command": "rm x"})])

    f = Factory(behave=behave)
    src, *_ = source(f)
    _, res = run(src, [{"goal": "g"}], isolate=False)
    e = res[0]
    assert e["status"] == "interrupted" and e["exit_reason"] == "approval_pending"
    assert f.threads[0] in e["summary"] and "terminal" in e["summary"]


# ─────────────────────────────── profondeur, arguments ───────────────────────────────


def test_profondeur():
    src, *_ = source(Factory(), max_depth=1)
    r, _ = run(src, [{"goal": "g"}], c=ctx("t1/sa-abcd1234"))
    assert r.is_error and "profondeur" in r.content
    # max_depth=2 : l'enfant de niveau 1 garde delegate, celui de niveau 2 non
    f = Factory()
    src, *_ = source(f, max_depth=2)
    run(src, [{"goal": "g"}], isolate=False)
    assert "delegate" in names(f.registries[0])
    f2 = Factory()
    src, *_ = source(f2, max_depth=1)
    run(src, [{"goal": "g"}], isolate=False)
    assert "delegate" not in names(f2.registries[0])


@pytest.mark.parametrize("args", [{}, {"tasks": []}, {"tasks": [{"context": "x"}]},
                                  {"tasks": [{"goal": "g", "role": "boss"}]},
                                  {"tasks": [{"goal": "g", "output_schema": "pas du json"}]}])
def test_arguments_invalides(args):
    src, *_ = source(Factory())
    assert src.call("delegate", args, ctx()).is_error


def test_registre_non_lie():
    src = DelegateToolSource(Factory(), None)
    assert src.call("delegate", {"tasks": [{"goal": "g"}]}, ctx()).is_error


# ─────────────────────────────── concurrence, délai d'inactivité ───────────────────────────────


def test_concurrence_bornee():
    cur, peak, lock = [0], [0], threading.Lock()

    def behave(kernel, inp):
        with lock:
            cur[0] += 1
            peak[0] = max(peak[0], cur[0])
        time.sleep(0.15)
        with lock:
            cur[0] -= 1
        return turn()

    src, *_ = source(Factory(behave=behave), max_children=2)
    _, res = run(src, [{"goal": str(i)} for i in range(5)], isolate=False)
    assert [e["status"] for e in res] == ["completed"] * 5 and [e["task_index"] for e in res] == list(range(5))
    assert peak[0] == 2


def test_delai_d_inactivite():
    release = threading.Event()

    def behave(kernel, inp):
        release.wait(5)
        return turn("trop tard")

    f = Factory(behave=behave)
    src, *_ = source(f, idle_timeout_s=0.2)
    t0 = time.monotonic()
    _, res = run(src, [{"goal": "g"}], isolate=False)
    assert time.monotonic() - t0 < 3
    assert res[0]["status"] == "interrupted" and res[0]["exit_reason"] == "idle_timeout"
    # ses outils refusent tout après l'interruption
    c = ToolContext(thread_id=f.threads[0], turn_index=0, channel="subagent")
    assert f.registries[0].call("web_search", {}, c).is_error
    release.set()


def test_activite_repousse_le_delai():
    def behave(kernel, inp):
        for _ in range(4):
            time.sleep(0.12)
            f.observers[0].tool_result(None, ToolCall("1", "web_search", {}), ToolResult("1", "web_search", ""), 1)
        return turn()

    f = Factory(behave=behave)
    src, *_ = source(f, idle_timeout_s=0.3)
    _, res = run(src, [{"goal": "g"}], isolate=False)
    assert res[0]["status"] == "completed"


# ─────────────────────────────── schéma de sortie ───────────────────────────────

SCHEMA = {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}}


def test_schema_valide_du_premier_coup():
    f = Factory(['{"n": 3}'])
    src, *_ = source(f)
    _, res = run(src, [{"goal": "g", "output_schema": SCHEMA}], isolate=False)
    assert res[0]["status"] == "completed" and len(f.kernels[0].inputs) == 1
    assert "integer" in f.kernels[0].inputs[0].user_text  # le schéma figure dans le but, quelle que soit la brique


def test_schema_invalide_puis_relance_reussie():
    f = Factory(["pas du json", '```json\n{"n": 7}\n```'])
    src, *_ = source(f)
    _, res = run(src, [{"goal": "g", "output_schema": SCHEMA}], isolate=False)
    assert res[0]["status"] == "completed" and len(f.kernels[0].inputs) == 2
    assert f.kernels[0].inputs[1].user_text != f.kernels[0].inputs[0].user_text
    assert res[0]["tokens"] == 30


def test_schema_repli_maison_et_brique_amont(monkeypatch):
    monkeypatch.setattr(dg, "_schema_module", lambda: None)  # repli maison
    assert dg.validate_reply('{"n": 1}', SCHEMA) == (True, [])
    ok, errs = dg.validate_reply('{"n": "x"}', SCHEMA)
    assert not ok and "integer" in errs[0]
    assert "refusée" in dg.retry_message(errs) and "Contrat de sortie" in dg.append_contract("", SCHEMA)
    assert dg.coerce_schema('{"type": "object"}') == ({"type": "object"}, None)
    assert dg.coerce_schema("{") [1]
    fake = type("M", (), {"coerce_output_schema": staticmethod(lambda r: (r, None)),
                          "append_output_contract": staticmethod(lambda c, s: "AMONT"),
                          "validate_output": staticmethod(lambda t, s: (False, ["e1"])),
                          "build_retry_message": staticmethod(lambda e: "RETRY " + e[0])})
    monkeypatch.setattr(dg, "_schema_module", lambda: fake)
    assert dg.append_contract("", SCHEMA) == "AMONT" and dg.retry_message(["e1"]) == "RETRY e1"
    assert dg.validate_reply("x", SCHEMA) == (False, ["e1"])


def test_schema_invalide_deux_fois_failed():
    f = Factory(['{"n": "x"}', '{"m": 1}'])
    src, *_ = source(f)
    _, res = run(src, [{"goal": "g", "output_schema": SCHEMA}], isolate=False)
    e = res[0]
    assert e["status"] == "failed" and e["exit_reason"] == "output_schema_invalid" and e["errors"]
    assert len(f.kernels[0].inputs) == 2  # une relance, pas plus


# ─────────────────────────────── git : faux run ───────────────────────────────


def test_worktree_sans_depot():
    st = {"repo": False}
    f = Factory()
    src, _, code = source(f)
    code.run_impl = git_fake(st)
    _, res = run(src, [{"goal": "g"}])
    e = res[0]
    assert e["status"] == "completed" and e["isolated"] is False and "pas de dépôt" in e["note"]
    assert "worktree" not in e and not code.scopes
    assert "ISOLATION" not in f.kernels[0].inputs[0].user_text


def test_finalize_zero_commit_propre_supprime():
    st = {}
    src, _, code = source(Factory())
    code.run_impl = git_fake(st)
    _, res = run(src, [{"goal": "g"}])
    wt = res[0]["worktree"]
    assert (wt["commits"], wt["dirty"], wt["kept"]) == (0, False, False) and st["removed"]
    assert wt["branch"].startswith("nokker/sa-") and wt["path"].startswith("/workspace/.nokker/worktrees/")
    assert any("branch -D" in c for c in st["log"])


@pytest.mark.parametrize("state,commits,dirty", [({"commits": 2}, 2, False), ({"status": " M a.py\n"}, 0, True)])
def test_finalize_conserve_avec_travail(state, commits, dirty):
    st = dict(state)
    src, _, code = source(Factory())
    code.run_impl = git_fake(st)
    _, res = run(src, [{"goal": "g"}])
    wt = res[0]["worktree"]
    assert (wt["commits"], wt["dirty"], wt["kept"]) == (commits, dirty, True) and "removed" not in st


def test_finalize_sonde_en_echec_conserve():
    st = {"probe_ok": False}
    src, _, code = source(Factory())
    code.run_impl = git_fake(st)
    _, res = run(src, [{"goal": "g"}])
    wt = res[0]["worktree"]
    assert wt["kept"] and wt["inspection_failed"] and "removed" not in st


def test_commandes_git_sans_hooks_et_exclude():
    st = {}
    src, _, code = source(Factory())
    code.run_impl = git_fake(st)
    run(src, [{"goal": "g"}])
    gits = [c for c in st["log"] if " git " in " " + c and "--version" not in c]
    assert gits and all("core.hooksPath=/dev/null" in c for c in gits)
    assert any(".nokker/" in c and "info/exclude" in c for c in st["log"])
    assert not any(".gitignore" in c for c in st["log"])


def test_relative_paths_selon_version_git():
    new = {"version": "2.48.1"}
    old = {"version": "2.47.3"}
    for st, expect in ((new, True), (old, False)):
        i = dg.create_worktree(git_fake(st), "/workspace", "ab12")
        add = next(c for c in st["log"] if "worktree add" in c)
        assert ("--relative-paths" in add) is expect and i["path"] == "/workspace/.nokker/worktrees/ab12"
    # repli : le .git du worktree et le gitdir d'administration sont réécrits en relatif
    rewrite = next(c for c in old["log"] if "printf 'gitdir: %s" in c)
    assert "../../.git/worktrees/x" in rewrite and ".nokker/worktrees/ab12/.git" not in rewrite.split(">")[0]
    assert not any("printf 'gitdir" in c for c in new["log"])


def test_worktree_add_en_echec():
    assert dg.create_worktree(git_fake({"add_ok": False}), "/workspace", "ab12") is None


# ─────────────────────────────── git : vrai dépôt local ───────────────────────────────


def sh(cmd, cwd):
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    for c in ("git init -q -b main", "git config user.email t@t", "git config user.name t",
              "echo hi > a.txt", "git add a.txt", "git commit -q -m init"):
        assert sh(c, r)[0] == 0
    return str(r)


def real_run(cwd):
    return lambda cmd: sh(cmd, cwd)


@pytest.mark.parametrize("rel", [False, True])
def test_vrai_git_creation_finalisation(repo, rel):
    if rel and not dg.supports_relative_paths(real_run(repo)):
        pytest.skip("git < 2.48")
    run_ = real_run(repo)
    info = dg.create_worktree(run_, repo, "t1", relative_paths=rel)
    assert info and os.path.isfile(os.path.join(info["path"], "a.txt"))
    assert info["path"] == os.path.join(repo, ".nokker/worktrees/t1") and info["branch"] == "nokker/sa-t1"
    # .git du worktree en relatif, worktree opérationnel
    dotgit = open(os.path.join(info["path"], ".git")).read()
    assert dotgit.startswith("gitdir: ") and not os.path.isabs(dotgit.split(": ", 1)[1].strip())
    assert run_(f"git -C {info['path']} status --porcelain")[0] == 0
    # .nokker/ exclu sans toucher au .gitignore versionné
    assert ".nokker/" in open(os.path.join(repo, ".git/info/exclude")).read()
    assert not os.path.exists(os.path.join(repo, ".gitignore"))
    assert sh("git status --porcelain", repo)[1].strip() == ""
    # 0 commit, propre : supprimé, branche comprise
    wt = dg.finalize_worktree(run_, info)
    assert (wt["commits"], wt["dirty"], wt["kept"]) == (0, False, False) and not os.path.exists(info["path"])
    assert "nokker/sa-t1" not in sh("git branch", repo)[1]
    # un commit : conservé
    info = dg.create_worktree(run_, repo, "t2", relative_paths=rel)
    sh("echo x > b.txt && git add b.txt && git commit -q -m b", info["path"])
    wt = dg.finalize_worktree(run_, info)
    assert (wt["commits"], wt["dirty"], wt["kept"]) == (1, False, True) and os.path.isdir(info["path"])
    # sale : conservé
    info = dg.create_worktree(run_, repo, "t3", relative_paths=rel)
    open(os.path.join(info["path"], "c.txt"), "w").write("c")
    wt = dg.finalize_worktree(run_, info)
    assert (wt["commits"], wt["dirty"], wt["kept"]) == (0, True, True)
    # exclude non dupliqué
    dg.ensure_excluded(run_, repo)
    assert open(os.path.join(repo, ".git/info/exclude")).read().count(".nokker/") == 1


def test_vrai_git_hook_piege_ne_tourne_pas(repo, tmp_path):
    marker = tmp_path / "pwned"
    for h in ("post-checkout", "pre-commit"):
        p = os.path.join(repo, ".git/hooks", h)
        open(p, "w").write(f"#!/bin/sh\ntouch {marker}\n")
        os.chmod(p, 0o755)
    run_ = real_run(repo)
    info = dg.create_worktree(run_, repo, "h1", relative_paths=False)
    assert info
    dg.finalize_worktree(run_, info)
    assert not marker.exists()
    # témoin : sans le garde-fou, le hook post-checkout tournerait bien
    sh(f"git worktree add -b w {repo}/w HEAD", repo)
    assert marker.exists()


def test_vrai_git_bout_en_bout_avec_delegate(repo):
    """delegate complet avec un faux Kernel qui commite dans le worktree, sur un vrai dépôt."""
    class Code(FakeCode):
        pass

    code = Code(repo, cwd=repo)

    def impl(cmd):
        return sh(cmd, repo)

    code.run_impl = impl

    def behave(kernel, inp):
        wt = os.path.join(repo, f".nokker/worktrees/{f.threads[0].rsplit('sa-', 1)[1]}")
        if "commite" in inp.user_text:
            sh("echo n > n.txt && git add n.txt && git commit -q -m n", wt)
        return turn("ok")

    f = Factory(behave=behave)
    src = DelegateToolSource(f, code)
    src.bind(Registry(sources=[Src("web_search"), code, src]))
    _, res = run(src, [{"goal": "commite"}, {"goal": "rien"}])
    a, b = res
    assert a["worktree"]["kept"] and a["worktree"]["commits"] == 1
    assert not b["worktree"]["kept"] and not os.path.exists(b["worktree"]["path"])
    assert sh(f"git branch --list nokker/sa-{a['child_id']}", repo)[1].strip()


def test_code_source_scoped_borne_au_worktree(tmp_path):
    from nokker.tools.code import CodeToolSource

    (tmp_path / "sub").mkdir()
    base = CodeToolSource(root=str(tmp_path), backend="local")
    sc = base.scoped("sub")
    ws = type("W", (), {"cwd": str(tmp_path / "sub")})()
    assert sc._inside(ws, "f.txt") == os.path.realpath(str(tmp_path / "sub" / "f.txt"))
    with pytest.raises(PermissionError):
        sc._inside(ws, "../x")
    assert base._spaces is not sc._spaces
    with pytest.raises(ValueError):
        base.scoped("../etc")


class PerThread(Src):
    """Source par fil comme `SessionCodeTools` : `for_thread` (un fil `<fil>/sa-<id>` a celle de son parent)."""

    def __init__(self, by_thread):
        super().__init__("read_file", "write_file", "patch", "terminal")
        self.by_thread = by_thread

    def for_thread(self, tid):
        while tid not in self.by_thread and "/sa-" in tid:
            tid = tid.rsplit("/sa-", 1)[0]
        return self.by_thread.get(tid)

    def sessions(self):
        return {t: s.root for t, s in self.by_thread.items()}


def test_source_par_fil(tmp_path):
    (tmp_path / ".git").mkdir()
    st = {}
    mine, other = FakeCode(str(tmp_path)), FakeCode("/autre")
    mine.run_impl = git_fake(st)
    other.run_impl = lambda cmd: (_ for _ in ()).throw(AssertionError("mauvaise source"))
    per = PerThread({"t1": mine, "t2": other})
    f = Factory()
    src = DelegateToolSource(f, per)
    src.bind(Registry(sources=[Src("web_search"), per, src]))
    assert src.specs()[0].effect_for({"tasks": []}) is Effect.DESTRUCTIVE  # "/autre" n'est pas un dépôt
    _, res = run(src, [{"goal": "g"}], c=ctx("t1"))
    assert res[0]["status"] == "completed" and not res[0]["worktree"]["kept"]
    assert mine.scopes and not other.scopes  # worktree ouvert sur la source du fil parent
    reg = f.registries[0]
    assert per not in reg.sources and any(isinstance(s, dg._ChildSource) and s._inner is mine.scopes[0]
                                          for s in reg.sources)
    # fil sans session de code : pas de worktree, l'enfant garde la source par fil (qui répondra « pas de session »)
    _, res = run(src, [{"goal": "g"}], c=ctx("t9"))
    assert res[0]["isolated"] is False
    only = PerThread({"t1": mine})
    (tmp_path / ".git").rmdir()
    assert DelegateToolSource(f, only).specs()[0].effect_for({}) is Effect.DESTRUCTIVE
