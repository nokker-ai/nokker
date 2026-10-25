"""Sous-agents (`delegate`, `delegate_status`, `delegate_cancel`) dans une session de code multi-utilisateur
(docs/LOKKAL.md §7.28, §7.22) : l'enfant tourne dans le MÊME bac à sable que la session (aucun second pod), dans son
worktree sous `.nokker/worktrees/<id>` ; il hérite des portes de la session (mode, règles) ; au plus
`code.max_subagents` enfants vivants par session ; la fermeture arrête et nettoie ; un autre utilisateur, un autre
locataire ne voient ni ne décident rien ; une carte d'enfant se décide par le propriétaire de la session seulement."""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import threading
import time
import types

import pytest

from nokker.approvals.memory import InMemoryApprovals
from nokker.code_sessions.config import CodeConfig
from nokker.code_sessions.service import NO_SESSION, CodeSessions
from nokker.code_sessions.store import MemoryCodeStore
from nokker.core.loop import Kernel
from nokker.core.ports import ToolContext
from nokker.core.turn import Decision, Gate, InputCheck, Message, Tier, ToolCall, Turn, TurnInput, Verdict
from nokker.tenancy.context import tenant_scope
from nokker.tools.registry import Registry
from tests.unit.code_sessions.conftest import code_block
from tests.unit.test_loop_approvals import Ctx, Esc, Gates, Inv, LedgerFake, Mem, Pol, ScriptedProvider, StoreFake
from tests.unit.tools.test_delegate import Factory, turn as fake_turn

TA, TB = "tn_aaaaaaaaaaaa", "tn_bbbbbbbbbbbb"
ANA, BEN = "us_aaaaaaaaaaa1", "us_aaaaaaaaaaa2"
REPO = "https://git.example.org/team/app.git"
_GIT = ["git", "-c", "user.email=t@example.org", "-c", "user.name=t", "-c", "init.defaultBranch=main"]


def git_cloner(url, dest, token, **_):
    """Un « clone » : un vrai dépôt git avec un commit (le worktree d'un enfant part de HEAD)."""
    subprocess.run([*_GIT, "init", "-q", dest], check=True)
    with open(os.path.join(dest, "README.md"), "w", encoding="utf-8") as f:
        f.write("app\n")
    subprocess.run([*_GIT, "-C", dest, "add", "."], check=True)
    subprocess.run([*_GIT, "-C", dest, "commit", "-qm", "init"], check=True)


class GitPods:
    """Faux module du bac à sable kubernetes dont le « pod » exécute vraiment les commandes (bash, git) sur la racine
    de SA session, vue au point de montage : un pod par environnement ouvert, comme le vrai module."""

    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = workspace_root
        self.pods: list = []

    def module(self) -> types.ModuleType:
        pods = self

        class Pod:
            def __init__(self, cfg, cwd, sub_path, labels, timeout):
                self.mount = cfg.get("workspace_mount") or "/workspace"
                self.cwd, self.sub_path, self.labels = cwd, sub_path, labels
                self.root = os.path.realpath(os.path.join(pods.workspace_root, sub_path))
                self.ran: list[tuple[str, str]] = []
                self.closed = False
                pods.pods.append(self)

            def real(self, path):
                p = os.path.normpath(path)
                if p != self.mount and not p.startswith(self.mount + "/"):
                    return None  # rien hors du montage de SA session
                full = os.path.realpath(os.path.join(self.root, p[len(self.mount):].lstrip("/")))
                return full if full == self.root or full.startswith(self.root + os.sep) else None

            def _host(self, text: str) -> str:
                return text.replace(self.mount, self.root)

            def execute(self, command, cwd="", timeout=120, **_):
                if self.closed:
                    raise RuntimeError("bac à sable fermé")
                where = cwd or self.cwd
                self.ran.append((where, command))
                p = subprocess.run(["bash", "-c", self._host(command)], cwd=self._host(where), capture_output=True,
                                   text=True, timeout=60)
                return {"output": (p.stdout + p.stderr).replace(self.root, self.mount), "returncode": p.returncode}

            def cleanup(self):
                self.closed = True

        mod = types.ModuleType("nokker.tools.k8s_sandbox")
        mod.KubernetesEnvironment = Pod  # type: ignore[attr-defined]
        mod.reap_k8s_sandboxes = lambda instance: 0  # type: ignore[attr-defined]
        return mod


@pytest.fixture
def pods(tmp_path, monkeypatch, fake_upstream):
    root = tmp_path / "workspaces"
    root.mkdir()
    p = GitPods(str(root))
    monkeypatch.setitem(sys.modules, "nokker.tools.k8s_sandbox", p.module())
    return p


@pytest.fixture
def make(pods):
    made = []

    def _make(**over):
        over.setdefault("modes", ["default", "accept_edits", "plan", "auto"])
        cfg = CodeConfig.from_config(code_block(pods.workspace_root, **over), lokkal=False)
        s = CodeSessions(cfg=cfg, store=MemoryCodeStore(), cloner=git_cloner)
        made.append(s)
        return s
    yield _make
    for s in made:
        s.close_all()


def _open(svc, user=ANA, tenant=TA, **body):
    body.setdefault("repo", {"url": REPO})
    with tenant_scope(tenant):
        s = svc.create(user, body)
    return s, f"{tenant}:{s['id']}"


def _ctx(thread, sender=ANA):
    return ToolContext(thread_id=thread, turn_index=1, channel="api", sender=sender)


class KernelGates(Gates):
    def __init__(self, inv):
        super().__init__(inv)
        self.invariants = inv


def _kernel(svc, script):
    """Un vrai noyau sur les outils de la session ; ses enfants sont des copies de lui-même (comme `serve`)."""
    inv = Inv()
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=svc.gates(KernelGates(inv)),
               escalator=Esc(), policy=Pol(), providers={Tier.SHIP: ScriptedProvider(script)},
               tools=Registry(sources=[svc.tools(), svc.subagent_tools()]), ledger=LedgerFake())
    svc.kernel_factory = lambda registry, thread_id, observer: dataclasses.replace(k, observer=observer,
                                                                                   tools=registry)
    svc.parent_registry = lambda: k.tools
    return k


def _result(turn_, name):
    return next(m.content for m in turn_.messages if m.role == "tool" and m.name == name)


# ─────────────────────────────── dans le bac à sable de la session ───────────────────────────────


def test_child_works_in_the_session_sandbox_and_its_worktree(make, pods):
    svc = make()
    events: list = []
    svc.emit = lambda name, payload: events.append((name, payload))
    _, other_thread = _open(svc, mode="auto")
    other_root = os.path.join(pods.workspace_root, TA, ANA, other_thread.split(":")[1])
    with open(os.path.join(other_root, "secret.txt"), "w", encoding="utf-8") as f:
        f.write("SECRET-DE-L-AUTRE-SESSION")
    s, thread = _open(svc, mode="auto")
    root = os.path.join(pods.workspace_root, TA, ANA, s["id"])
    other = other_thread.split(":")[1]
    script = [
        Message("assistant", tool_calls=[ToolCall(id="d1", name="delegate", arguments={
            "tasks": [{"goal": "ajoute b.txt"}], "isolate": True})]),
        # l'enfant : écrit dans son worktree, lance une commande, tente de lire une autre session
        Message("assistant", tool_calls=[ToolCall(id="w1", name="write_file",
                                                  arguments={"path": "b.txt", "content": "de l'enfant"})]),
        Message("assistant", tool_calls=[ToolCall(id="t1", name="terminal", arguments={"command": "pwd"})]),
        Message("assistant", tool_calls=[ToolCall(id="r1", name="read_file",
                                                  arguments={"path": f"/workspace/../{other}/secret.txt"})]),
        Message("assistant", "fini"),
        Message("assistant", "fait"),
    ]
    k = _kernel(svc, script)
    with tenant_scope(TA):
        t = k.run_turn(TurnInput(thread, "délègue", sender=ANA))
    assert t.reply == "fait", t.messages
    [child] = json.loads(_result(t, "delegate"))
    assert child["status"] == "completed", child
    wt = child["worktree"]
    assert wt["path"].startswith("/workspace/.nokker/worktrees/") and wt["kept"] and wt["dirty"]
    host_wt = os.path.join(root, wt["path"][len("/workspace/"):])
    assert open(os.path.join(host_wt, "b.txt"), encoding="utf-8").read() == "de l'enfant"
    assert not os.path.exists(os.path.join(root, "b.txt"))  # pas dans le dossier de la session
    # un seul pod pour la session (+ celui de l'autre session, jamais ouvert ici) : l'enfant partage celui du parent
    session_pods = [p for p in pods.pods if p.sub_path.endswith(s["id"])]
    assert len(session_pods) == 1 and not session_pods[0].closed
    assert (wt["path"], "pwd") in session_pods[0].ran
    child_msgs = [x for x in k.store.turns if x.thread_id == child["thread_id"]]
    assert child_msgs and child_msgs[0].sender == ANA
    seen = json.dumps([m.content for x in child_msgs for m in x.messages], ensure_ascii=False)
    assert "SECRET-DE-L-AUTRE-SESSION" not in seen
    # événements : au propriétaire seulement, dans son locataire
    started = [p for n, p in events if n == "subagent.started"]
    assert started and all(p["_audience"] == [ANA] and p["_tenant"] == TA for _, p in events)


def test_isolation_required_for_an_isolated_delegate(make):
    """`isolate: true` (effet WRITE) ne retombe JAMAIS sur le dossier de la session quand un worktree est impossible
    (pas de dépôt git) : refus, l'enfant ne part pas."""
    svc = make()
    svc.cloner = lambda *a, **k: None
    f = Factory()
    svc.kernel_factory, svc.parent_registry = f, lambda: Registry(sources=[svc.tools()])
    s, thread = _open(svc, repo=None, mode="auto")
    with tenant_scope(TA):
        r = svc.subagent_tools().call("delegate", {"tasks": [{"goal": "g"}], "isolate": True}, _ctx(thread))
    assert r.is_error and "isolate=false" in r.content and f.kernels == []


def test_child_inherits_the_session_gates(make):
    """Le fil d'un enfant `<session>/sa-<id>` a les portes de la session : mode plan → aucune écriture."""
    svc = make()
    s, thread = _open(svc, mode="plan")
    gates = svc.gates(KernelGates(Inv()))
    spec = next(x for x in svc.tools().specs() if x.name == "write_file")
    t = Turn(thread_id=f"{thread}/sa-0123abcd", index=0, input=InputCheck(text=""), channel="subagent", sender=ANA)
    with tenant_scope(TA):
        v = gates.action(ToolCall(id="c", name="write_file", arguments={"path": "a", "content": ""}), spec, t)
    assert v[0].decision is Decision.DENY


# ─────────────────────────────── plafond, fermeture ───────────────────────────────


def _gated_factory():
    release = threading.Event()

    def behave(kernel, inp):
        release.wait(5)
        return fake_turn("fini en fond")

    return release, Factory(behave=behave)


def test_live_subagents_cap_per_session(make):
    svc = make(max_subagents=2)
    release, f = _gated_factory()
    svc.kernel_factory, svc.parent_registry = f, lambda: Registry(sources=[svc.tools()])
    s, thread = _open(svc, mode="auto")
    tools = svc.subagent_tools()
    try:
        with tenant_scope(TA):
            r = tools.call("delegate", {"tasks": [{"goal": "a"}, {"goal": "b"}, {"goal": "c"}], "isolate": False,
                                        "background": True}, _ctx(thread))
            assert r.is_error and "2" in r.content
            r = tools.call("delegate", {"tasks": [{"goal": "a"}, {"goal": "b"}], "isolate": False,
                                        "background": True}, _ctx(thread))
            assert not r.is_error, r.content
            r = tools.call("delegate", {"tasks": [{"goal": "c"}], "isolate": False, "background": True}, _ctx(thread))
            assert r.is_error  # deux vivants : plus rien
            assert svc.get(ANA, s["id"])["subagents"] == {"limit": 2, "live": 2}
            # une autre session du même compte a son propre plafond
            _, t2 = _open(svc, mode="auto")
            r = tools.call("delegate", {"tasks": [{"goal": "d"}], "isolate": False, "background": True}, _ctx(t2))
            assert not r.is_error, r.content
    finally:
        release.set()


def test_close_cancels_and_cleans_the_children(make):
    svc = make()
    release, f = _gated_factory()
    svc.kernel_factory, svc.parent_registry = f, lambda: Registry(sources=[svc.tools()])
    s, thread = _open(svc, mode="auto")
    tools = svc.subagent_tools()
    try:
        with tenant_scope(TA):
            out = json.loads(tools.call("delegate", {"tasks": [{"goal": "long"}], "isolate": False,
                                                     "background": True}, _ctx(thread)).content)
            child = out["children"][0]
            state = os.path.join(svc.cfg.state_dir, TA, ANA, "subagents", f"{s['id']}.json")
            assert os.path.isfile(state)
            svc.close(ANA, s["id"])
            assert f.kernels and f.kernels[0].cancelled == [child["thread_id"]]  # son noyau reçoit l'annulation
            assert not os.path.exists(state)
            # ses outils ne servent plus rien : la session est fermée
            r = svc.tools().call("write_file", {"path": "x", "content": ""}, _ctx(child["thread_id"]))
            assert r.is_error and r.content == NO_SESSION
            assert svc.subagent_kernel(child["thread_id"]) is None
            r = tools.call("delegate_status", {}, _ctx(thread))
            assert r.is_error and r.content == NO_SESSION
        release.set()  # l'enfant rend la main : son fil de garde finit sans réécrire l'état d'une session fermée
        deadline = time.monotonic() + 5
        while any(t.name.startswith("nokker-sa-") for t in threading.enumerate()) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not os.path.exists(state)
    finally:
        release.set()


def test_purge_removes_the_worktrees_with_the_workspace(make, pods):
    svc = make()
    s, thread = _open(svc, mode="auto")
    root = os.path.join(pods.workspace_root, TA, ANA, s["id"])
    os.makedirs(os.path.join(root, ".nokker", "worktrees", "x"))
    with tenant_scope(TA):
        assert svc.close(ANA, s["id"], purge=True)["purged"] is True
    assert not os.path.exists(root)


# ─────────────────────────────── autre utilisateur, autre locataire ───────────────────────────────


def test_other_user_and_other_tenant_see_and_decide_nothing(make):
    svc = make()
    release, f = _gated_factory()
    svc.kernel_factory, svc.parent_registry = f, lambda: Registry(sources=[svc.tools()])
    s, thread = _open(svc, mode="auto")
    tools = svc.subagent_tools()
    try:
        with tenant_scope(TA):
            out = json.loads(tools.call("delegate", {"tasks": [{"goal": "g"}], "isolate": False,
                                                     "background": True}, _ctx(thread)).content)
            child = out["children"][0]
            for name in ("delegate", "delegate_status", "delegate_cancel"):  # ben, même locataire, fil d'ana
                args = {"tasks": [{"goal": "x"}], "isolate": False} if name == "delegate" else {}
                r = tools.call(name, args, _ctx(thread, sender=BEN))
                assert r.is_error and "autre utilisateur" in r.content, name
            r = svc.tools().call("read_file", {"path": "README.md"}, _ctx(child["thread_id"], sender=BEN))
            assert r.is_error and "autre utilisateur" in r.content
        with tenant_scope(TB):  # autre locataire : le fil n'est pas une session ici
            for name in ("delegate_status", "delegate_cancel"):
                r = tools.call(name, {}, _ctx(thread, sender="us_bbbbbbbbbbb1"))
                assert r.is_error and r.content == NO_SESSION
            assert svc.owner_of_thread(child["thread_id"]) == ANA  # le propriétaire suit le fil, pas l'appelant
        # carte d'un enfant : décidée par le propriétaire de la session seulement
        approvals = InMemoryApprovals(account_tenant=lambda u: TA if u in (ANA, BEN) else None)
        approvals.thread_owner = svc.owner_of_thread
        card_turn = Turn(thread_id=child["thread_id"], index=0, input=InputCheck(text=""), channel="subagent",
                         sender=ANA)
        verdict = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="permissions:default")
        with tenant_scope(TA):
            card = approvals.request(card_turn, ToolCall(id="c", name="write_file", arguments={}), verdict, None)
            assert approvals.decider_allowed(ANA, TA, card) is True
            assert approvals.decider_allowed(BEN, TA, card) is False
            assert approvals.visible_to(BEN, card) is False and approvals.visible_to(ANA, card) is True
            plain = approvals.request(dataclasses.replace(card_turn, thread_id=f"{TA}:th_plain"),
                                      ToolCall(id="d", name="x", arguments={}), verdict, None)
            assert approvals.decider_allowed(BEN, TA, plain) is True  # hors session : inchangé
    finally:
        release.set()


def test_subagents_off_with_zero(make):
    svc = make(max_subagents=0)
    assert svc.subagent_tools().specs() == []
    s, _ = _open(svc)
    assert "subagents" not in s


def test_shared_env_never_moves_or_closes_the_parent_sandbox():
    from nokker.tools.code import SharedEnv

    class Inner:
        cwd = "/workspace"

        def execute(self, command, cwd="", **kw):
            self.cwd = cwd + "/sub"  # l'amont range le dossier observé chez lui
            return {"output": "", "returncode": 0, "cwd_observed": True, "cwd": cwd + "/sub"}

        def cleanup(self):
            raise AssertionError("le bac à sable du parent ne se ferme jamais par l'enfant")

    inner = Inner()
    env = SharedEnv(inner, "/workspace/.nokker/worktrees/a")
    env.execute("cd sub")
    assert env.cwd == "/workspace/.nokker/worktrees/a/sub" and inner.cwd == "/workspace"
    env.cleanup()
