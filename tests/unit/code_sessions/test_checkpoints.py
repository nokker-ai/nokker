"""Points de retour des sessions de code multi-utilisateur : un magasin PAR SESSION, sous
`<state_dir>/<tenant>/<user>/checkpoints/<session>` (hors de toute racine montée dans un bac à sable), jamais commun
entre locataires ; la purge d'une session l'efface. Partie 1 (toujours jouée) : le routage, avec un gestionnaire
factice qui range ses points sous le « home » actif. Partie 2 (amont importable : image) : le vrai gestionnaire de
l'amont, checkpoint_list et checkpoint_diff d'un locataire ne lisent jamais un point d'un autre."""
from __future__ import annotations

import contextvars
import importlib.util
import json
import os

import pytest

from nokker.code_sessions.checkpoints import SessionCheckpoints
from nokker.code_sessions.config import CodeConfig
from nokker.code_sessions.service import CodeSessions
from nokker.code_sessions.store import MemoryCodeStore
from nokker.core.ports import ToolContext
from nokker.tenancy.context import tenant_scope
from nokker.tools.checkpoints import CheckpointToolSource, CheckpointingToolSource
from tests.unit.code_sessions.conftest import code_block

TA, TB = "tn_aaaaaaaaaaaa", "tn_bbbbbbbbbbbb"
ANA, CAM = "us_aaaaaaaaaaa1", "us_bbbbbbbbbbb1"
UPSTREAM = importlib.util.find_spec("tools") is not None and importlib.util.find_spec(
    "tools.checkpoint_manager") is not None and importlib.util.find_spec("hermes_constants") is not None

_HOME: contextvars.ContextVar[str | None] = contextvars.ContextVar("fake_home", default=None)


class FakeManager:
    """Range ses points dans un fichier JSON sous le « home » actif au moment de l'appel, comme le vrai gestionnaire
    range son dépôt fantôme sous `<home>/checkpoints/store`."""

    instances: list[FakeManager] = []

    def __init__(self) -> None:
        self._checkpointed_dirs: set[str] = set()
        self.turns = 0
        FakeManager.instances.append(self)

    def _db(self) -> str:
        home = _HOME.get()
        assert home, "appel sans home de session"
        return os.path.join(home, "checkpoints", "store.json")

    def _load(self) -> dict:
        p = self._db()
        return json.load(open(p)) if os.path.isfile(p) else {}

    def new_turn(self) -> None:
        self.turns += 1
        self._checkpointed_dirs.clear()

    def ensure_checkpoint(self, working_dir: str, reason: str = "auto") -> bool:
        d = self._load()
        d.setdefault(working_dir, []).append({"hash": f"h{len(d.get(working_dir, []))}", "reason": reason})
        os.makedirs(os.path.dirname(self._db()), exist_ok=True)
        json.dump(d, open(self._db(), "w"))
        return True

    def list_checkpoints(self, working_dir: str) -> list[dict]:
        return list(self._load().get(working_dir, []))

    def diff(self, working_dir: str, commit_hash: str) -> dict:
        known = {c["hash"] for c in self.list_checkpoints(working_dir)}
        return {"success": commit_hash in known, "error": "inconnu", "diff": f"d:{commit_hash}", "stat": ""}

    def session_diff(self, working_dir: str) -> dict:
        return {"success": True, "empty": True}

    def restore(self, working_dir: str, commit_hash: str, file_path=None, safe=True) -> dict:
        return {"success": commit_hash in {c["hash"] for c in self.list_checkpoints(working_dir)}}

    def record_agent_write(self, file_path: str) -> None:
        pass


def _override(path):
    return _HOME.set(str(path))


@pytest.fixture
def svc(cluster, fake_upstream):
    s = CodeSessions(cfg=CodeConfig.from_config(code_block(cluster.workspace_root), lokkal=False),
                     store=MemoryCodeStore())
    yield s
    s.close_all()


def _sources(svc, manager):
    return (CheckpointingToolSource(svc.tools(), svc.root_of, manager=manager),
            CheckpointToolSource(svc.root_of, manager=manager))


def _ck(tools, tenant, user, thread, name, args=None):
    with tenant_scope(tenant):
        return tools.call(name, args or {}, ToolContext(thread_id=thread, turn_index=1, channel="api", sender=user))


def test_one_store_per_session_outside_the_sandbox(svc, cluster):
    mgr = SessionCheckpoints(svc, factory=FakeManager, override=(_override, _HOME.reset))
    _, tools = _sources(svc, mgr)
    with tenant_scope(TA):
        a = svc.create(ANA, {})
    with tenant_scope(TB):
        b = svc.create(CAM, {})
    ta, tb = f"{TA}:{a['id']}", f"{TB}:{b['id']}"
    assert json.loads(_ck(tools, TA, ANA, ta, "checkpoint_create", {"reason": "A"}).content)["created"] is True
    home_a = os.path.join(svc.cfg.state_dir, TA, ANA, "checkpoints", a["id"])
    assert os.path.isfile(os.path.join(home_a, "checkpoints", "store.json"))
    assert not home_a.startswith(os.path.join(cluster.workspace_root, TA) + os.sep)  # hors de toute racine montée
    # B : rien de A, ni en liste ni en diff
    assert json.loads(_ck(tools, TB, CAM, tb, "checkpoint_list").content) == []
    r = _ck(tools, TB, CAM, tb, "checkpoint_diff", {"hash": "h0"})
    assert r.is_error and "d:h0" not in r.content
    assert json.loads(_ck(tools, TA, ANA, ta, "checkpoint_list").content)[0]["reason"] == "A"
    assert not os.path.exists(os.path.join(svc.cfg.state_dir, TB, CAM, "checkpoints", b["id"], "checkpoints",
                                           "store.json"))
    # le fil de A appelé depuis B : pas de racine, aucun accès
    r = _ck(tools, TB, CAM, ta, "checkpoint_list")
    assert r.is_error and "A" not in r.content


def test_a_path_outside_the_current_tenant_is_refused(svc):
    mgr = SessionCheckpoints(svc, factory=FakeManager, override=(_override, _HOME.reset))
    with tenant_scope(TA):
        a = svc.create(ANA, {})
    root_a = os.path.join(svc.cfg.workspace_root, TA, ANA, a["id"])
    with tenant_scope(TB), pytest.raises(PermissionError):
        mgr.list_checkpoints(root_a)
    with tenant_scope(TA):
        for bad in ("/etc", svc.cfg.workspace_root, os.path.join(svc.cfg.workspace_root, TA, ANA)):
            with pytest.raises(PermissionError):
                mgr.ensure_checkpoint(bad, "x")
        assert mgr.list_checkpoints(root_a) == []


def test_wrapper_snapshots_before_a_write_and_purge_erases(svc, cluster):
    mgr = SessionCheckpoints(svc, factory=FakeManager, override=(_override, _HOME.reset))
    wrapper, tools = _sources(svc, mgr)
    with tenant_scope(TA):
        a = svc.create(ANA, {})
    ta = f"{TA}:{a['id']}"
    assert not _ck(wrapper, TA, ANA, ta, "write_file", {"path": "f.txt", "content": "1"}).is_error
    assert [c["reason"] for c in json.loads(_ck(tools, TA, ANA, ta, "checkpoint_list").content)] == [
        "avant write_file"]
    home_a = os.path.join(svc.cfg.state_dir, TA, ANA, "checkpoints", a["id"])
    with tenant_scope(TA):
        svc.close(ANA, a["id"])  # fermer sans purge : les points de retour restent
        assert os.path.isdir(home_a)
        b = svc.create(ANA, {})
        _ck(tools, TA, ANA, f"{TA}:{b['id']}", "checkpoint_create")
        svc.close(ANA, b["id"], purge=True)
    assert not os.path.exists(os.path.join(svc.cfg.state_dir, TA, ANA, "checkpoints", b["id"]))
    assert not os.path.exists(os.path.join(cluster.workspace_root, TA, ANA, b["id"]))


def test_unavailable_upstream_makes_the_tools_say_so(svc, monkeypatch):
    mgr = SessionCheckpoints(svc)  # amont factice (fake_upstream) : pas de gestionnaire
    assert mgr.available() is False


@pytest.mark.skipif(not UPSTREAM, reason="amont (tools.checkpoint_manager, hermes_constants) non importable")
def test_real_upstream_never_reads_another_tenants_checkpoint(cluster, tmp_path, monkeypatch):
    import nokker.tools.checkpoints as ckmod

    monkeypatch.setattr(ckmod, "_cm_module", None)  # pas de cache d'un test précédent
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    svc = CodeSessions(cfg=CodeConfig.from_config(code_block(cluster.workspace_root), lokkal=False),
                       store=MemoryCodeStore())
    mgr = SessionCheckpoints(svc)
    assert mgr.available()
    tools = CheckpointToolSource(svc.root_of, manager=mgr)
    with tenant_scope(TA):
        a = svc.create(ANA, {})
    with tenant_scope(TB):
        b = svc.create(CAM, {})
    ta, tb = f"{TA}:{a['id']}", f"{TB}:{b['id']}"
    root_a = os.path.join(cluster.workspace_root, TA, ANA, a["id"])
    root_b = os.path.join(cluster.workspace_root, TB, CAM, b["id"])
    open(os.path.join(root_a, "secret.txt"), "w").write("SECRET-DE-A\n")
    open(os.path.join(root_b, "b.txt"), "w").write("b\n")
    created = json.loads(_ck(tools, TA, ANA, ta, "checkpoint_create", {"reason": "a1"}).content)
    assert created["created"] is True, created
    open(os.path.join(root_a, "secret.txt"), "w").write("SECRET-DE-A modifié\n")
    h = created["hash"]
    diff_a = _ck(tools, TA, ANA, ta, "checkpoint_diff", {"hash": h})
    assert "SECRET-DE-A" in diff_a.content  # A lit les siens
    assert json.loads(_ck(tools, TB, CAM, tb, "checkpoint_list").content) == []
    for args in ({"hash": h}, {}):
        r = _ck(tools, TB, CAM, tb, "checkpoint_diff", args)
        assert "SECRET-DE-A" not in r.content, r.content
    r = _ck(tools, TB, CAM, tb, "checkpoint_restore", {"hash": h})
    assert r.is_error and not os.path.exists(os.path.join(root_b, "secret.txt"))
    # aucun magasin commun : rien sous le home du noyau, un magasin par session sous state_dir
    assert not (tmp_path / "hermes-home" / "checkpoints" / "store").exists()
    assert os.path.isdir(os.path.join(svc.cfg.state_dir, TA, ANA, "checkpoints", a["id"], "checkpoints", "store"))
    with tenant_scope(TA):
        svc.close(ANA, a["id"], purge=True)
    assert not os.path.exists(os.path.join(svc.cfg.state_dir, TA, ANA, "checkpoints", a["id"]))
    svc.close_all()
