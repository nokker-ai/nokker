"""Service des sessions de code multi-utilisateur (docs/LOKKAL.md §7.28) sur le vrai `CodeToolSource` (backend
kubernetes, faux module du bac à sable, briques de fichiers factices) : isolation entre deux utilisateurs d'un même
locataire et entre deux locataires, évasion de chemin (`../`, lien symbolique), plafond par utilisateur, nettoyage à
l'inactivité (bac à sable libéré, espace gardé), identifiant de forge jamais exposé."""
from __future__ import annotations

import json
import os

import pytest

from nokker.code_sessions.config import CodeConfig
from nokker.code_sessions.secrets import TenantSecrets
from nokker.code_sessions.service import CodeError, CodeSessions
from nokker.code_sessions.store import MemoryCodeStore
from nokker.core.ports import ToolContext
from nokker.tenancy.context import tenant_scope
from tests.unit.code_sessions.conftest import MemorySecrets, code_block

TA, TB = "tn_aaaaaaaaaaaa", "tn_bbbbbbbbbbbb"
ANA, BEN, CAM = "us_aaaaaaaaaaa1", "us_aaaaaaaaaaa2", "us_bbbbbbbbbbb1"  # ana et ben chez A, cam chez B


class Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def svc(cluster, fake_upstream):
    clones = []

    def cloner(url, dest, token, *, ref=None, depth=None, timeout=300):
        clones.append({"url": url, "dest": dest, "token": token, "ref": ref})
        with open(os.path.join(dest, "README.md"), "w") as f:
            f.write("dépôt cloné\n")

    clock = Clock()
    vaults: dict[str, MemorySecrets] = {}
    secrets = TenantSecrets(lambda tn: vaults.setdefault(tn, MemorySecrets()))
    cfg = CodeConfig.from_config(code_block(cluster.workspace_root), lokkal=False)
    s = CodeSessions(cfg=cfg, store=MemoryCodeStore(), secrets=secrets, cloner=cloner, clock=clock)
    s.clones, s.clock_, s.vaults_ = clones, clock, vaults  # type: ignore[attr-defined]
    yield s
    s.close_all()


def _call(svc, tenant, user, thread, name, args):
    with tenant_scope(tenant):
        r = svc.tools().call(name, args, ToolContext(thread_id=thread, turn_index=1, channel="api", sender=user))
    return r


def _open(svc, tenant, user, body=None):
    with tenant_scope(tenant):
        return svc.create(user, body or {})


def test_create_gives_a_scoped_thread_on_its_own_root(svc, cluster):
    a = _open(svc, TA, ANA)
    assert a["id"].startswith("cs_") and a["thread_id"] == a["id"] and a["status"] == "active"
    assert a["workspace"] == "/workspace" and a["mode"] == "accept_edits" and a["repo"] is None
    root = os.path.join(cluster.workspace_root, TA, ANA, a["id"])
    assert os.path.isdir(root)
    with tenant_scope(TA):
        s = svc.session_for_thread(f"{TA}:{a['id']}")
    assert s is not None and s.user_id == ANA and s.thread_id == f"{TA}:{a['id']}"
    r = _call(svc, TA, ANA, s.thread_id, "write_file", {"path": "src/m.py", "content": "x = 1\n"})
    assert not r.is_error, r.content
    assert open(os.path.join(root, "src", "m.py")).read() == "x = 1\n"
    pod = cluster.pods[-1]
    assert pod.cfg == {**svc.cfg.kubernetes, "idle_ttl_s": 1800}
    assert pod.sub_path == f"{TA}/{ANA}/{a['id']}" and pod.cwd == "/workspace" and pod.timeout == 120
    assert pod.labels == {"app.kubernetes.io/name": "nokker-sandbox", "nokker.instance": "nokker-test",
                          "nokker.tenant": "tn-aaaaaaaaaaaa", "nokker.session": a["id"].replace("_", "-")}


def test_two_users_two_tenants_are_isolated(svc, cluster):
    a, b, c = _open(svc, TA, ANA), _open(svc, TA, BEN), _open(svc, TB, CAM)
    ta, tb, tc = f"{TA}:{a['id']}", f"{TA}:{b['id']}", f"{TB}:{c['id']}"
    for tenant, user, thread in ((TA, ANA, ta), (TA, BEN, tb), (TB, CAM, tc)):
        assert not _call(svc, tenant, user, thread, "write_file", {"path": "secret.txt", "content": user}).is_error
    # listes : chacun les siennes
    with tenant_scope(TA):
        assert [x["id"] for x in svc.list(ANA)["sessions"]] == [a["id"]]
        assert [x["id"] for x in svc.list(BEN)["sessions"]] == [b["id"]]
        with pytest.raises(CodeError) as e:
            svc.get(BEN, a["id"])  # même locataire, autre utilisateur : n'existe pas
        assert e.value.status == 404
        with pytest.raises(CodeError):
            svc.close(BEN, a["id"])
        with pytest.raises(CodeError):
            svc.get(ANA, c["id"])  # autre locataire
    with tenant_scope(TB):
        assert svc.list(CAM)["sessions"][0]["id"] == c["id"] and svc.session_for_thread(ta) is None
    # le fil d'ana appelé par ben (même locataire) : refusé, rien lu
    r = _call(svc, TA, BEN, ta, "read_file", {"path": "secret.txt"})
    assert r.is_error and ANA not in r.content
    # le fil d'ana appelé depuis le locataire B : refusé
    r = _call(svc, TB, CAM, ta, "read_file", {"path": "secret.txt"})
    assert r.is_error and ANA not in r.content
    # chacun lit le sien
    assert ANA in _call(svc, TA, ANA, ta, "read_file", {"path": "secret.txt"}).content
    assert CAM in _call(svc, TB, CAM, tc, "read_file", {"path": "secret.txt"}).content


def test_path_escape_dotdot_and_symlink(svc, cluster):
    a, b = _open(svc, TA, ANA), _open(svc, TA, BEN)
    ta, tb = f"{TA}:{a['id']}", f"{TA}:{b['id']}"
    _call(svc, TA, BEN, tb, "write_file", {"path": "secret.txt", "content": "BEN-SECRET"})
    root_a = os.path.join(cluster.workspace_root, TA, ANA, a["id"])
    root_b = os.path.join(cluster.workspace_root, TA, BEN, b["id"])
    for path in (f"../../{BEN}/{b['id']}/secret.txt", f"/workspace/../../{BEN}/{b['id']}/secret.txt",
                 f"{root_b}/secret.txt", "../../../" + TB, "/etc/passwd"):
        r = _call(svc, TA, ANA, ta, "read_file", {"path": path})
        assert r.is_error and "BEN-SECRET" not in r.content, path
        w = _call(svc, TA, ANA, ta, "write_file", {"path": path, "content": "pwn"})
        assert w.is_error, path
    assert open(os.path.join(root_b, "secret.txt")).read() == "BEN-SECRET"
    # lien symbolique posé dans le dépôt d'ana (un dépôt cloné peut en contenir) vers le dossier de ben
    os.symlink(os.path.join(root_b, "secret.txt"), os.path.join(root_a, "leak.txt"))
    os.symlink(root_b, os.path.join(root_a, "leakdir"))
    for path in ("leak.txt", "leakdir/secret.txt", "/workspace/leak.txt"):
        r = _call(svc, TA, ANA, ta, "read_file", {"path": path})
        assert r.is_error and "BEN-SECRET" not in r.content, path
    s = _call(svc, TA, ANA, ta, "search_files", {"pattern": "BEN-SECRET", "path": "leakdir"})
    assert "BEN-SECRET" not in s.content and root_b not in s.content
    w = _call(svc, TA, ANA, ta, "write_file", {"path": "leakdir/secret.txt", "content": "pwn"})
    assert w.is_error and open(os.path.join(root_b, "secret.txt")).read() == "BEN-SECRET"


def test_a_minor_cannot_open_a_code_session(svc):
    """Foyer (§7.29) : un profil mineur n'ouvre pas de session de code (terminal, écritures) — les portes de Lokkal
    d'une session ne passent pas par celles du mineur ; refus avant toute création."""
    svc.minor = lambda account: account == BEN
    with pytest.raises(CodeError) as e:
        _open(svc, TA, BEN)
    assert e.value.status == 403 and e.value.code == "minor_not_allowed"
    with tenant_scope(TA):
        assert svc.list(BEN)["sessions"] == []
    assert _open(svc, TA, ANA)["status"] == "active"  # un adulte du même locataire : inchangé


def test_cap_per_user(svc):
    ids = [_open(svc, TA, ANA)["id"] for _ in range(3)]
    with pytest.raises(CodeError) as e:
        _open(svc, TA, ANA)
    assert e.value.status == 409 and e.value.code == "session_limit"
    _open(svc, TA, BEN)  # le plafond est par utilisateur
    with tenant_scope(TA):
        svc.close(ANA, ids[0])
    assert _open(svc, TA, ANA)["status"] == "active"  # une session fermée libère sa place


def test_idle_cleanup_releases_the_sandbox_and_keeps_the_workspace(svc, cluster):
    a = _open(svc, TA, ANA)
    ta = f"{TA}:{a['id']}"
    _call(svc, TA, ANA, ta, "write_file", {"path": "keep.txt", "content": "garde"})
    pod = cluster.pods[-1]
    svc.clock_.t += 1000
    assert svc.reap_idle() == 0 and not pod.closed  # pas encore inactive
    svc.clock_.t += 1801
    assert svc.reap_idle() == 1 and pod.closed
    with tenant_scope(TA):
        assert svc.get(ANA, a["id"])["status"] == "idle"
    root = os.path.join(cluster.workspace_root, TA, ANA, a["id"])
    assert open(os.path.join(root, "keep.txt")).read() == "garde"
    # le prochain appel rouvre un bac à sable sur le même espace
    assert "garde" in _call(svc, TA, ANA, ta, "read_file", {"path": "keep.txt"}).content
    _call(svc, TA, ANA, ta, "terminal", {"command": "ls"})
    assert len(cluster.pods) == 2 and not cluster.pods[-1].closed
    with tenant_scope(TA):
        assert svc.get(ANA, a["id"])["status"] == "active"


def test_close_keeps_or_purges_the_workspace(svc, cluster):
    a, b = _open(svc, TA, ANA), _open(svc, TA, ANA)
    for x in (a, b):
        _call(svc, TA, ANA, f"{TA}:{x['id']}", "terminal", {"command": "true"})
    with tenant_scope(TA):
        assert svc.close(ANA, a["id"]) == {"id": a["id"], "closed": True, "purged": False}
        assert svc.close(ANA, b["id"], purge=True)["purged"] is True
        assert svc.list(ANA)["sessions"] == []
    assert all(p.closed for p in cluster.pods)
    assert os.path.isdir(os.path.join(cluster.workspace_root, TA, ANA, a["id"]))
    assert not os.path.exists(os.path.join(cluster.workspace_root, TA, ANA, b["id"]))
    r = _call(svc, TA, ANA, f"{TA}:{a['id']}", "read_file", {"path": "x"})
    assert r.is_error and "session" in r.content  # fil d'une session fermée : plus d'outils


def test_repo_clone_with_a_named_credential_never_exposed(svc, cluster):
    with tenant_scope(TA):
        assert svc.put_credential("forge-bot", "bot:tok-TOPSECRET-42") == {"name": "forge-bot", "set": True}
        listing = svc.credentials()
    assert listing == {"credentials": [{"name": "forge-bot", "updated_at": None}]}
    assert {tn: v.values for tn, v in svc.vaults_.items()} == {TA: {"forge-bot": "bot:tok-TOPSECRET-42"}}  # par locataire
    body = {"repo": {"url": "https://git.example.org/team/app.git", "credential": "forge-bot", "ref": "main"}}
    a = _open(svc, TA, ANA, body)
    assert svc.clones[-1]["token"] == "bot:tok-TOPSECRET-42" and svc.clones[-1]["ref"] == "main"
    assert a["repo"] == {"url": "https://git.example.org/team/app.git", "ref": "main", "credential": "forge-bot"}
    with tenant_scope(TA):
        dump = json.dumps([a, svc.list(ANA), svc.get(ANA, a["id"]), svc.credentials()])
        rows = repr(svc.store.list_user(ANA))
    assert "TOPSECRET" not in dump and "TOPSECRET" not in rows
    # l'autre locataire ne peut pas utiliser le nom (son coffre ne l'a pas)
    with pytest.raises(CodeError) as e:
        _open(svc, TB, CAM, body)
    assert e.value.status == 400 and e.value.code == "credential_missing"
    # identifiants dans l'URL : refusés sans écho
    with pytest.raises(CodeError) as e:
        _open(svc, TA, ANA, {"repo": {"url": "https://bot:tok-INURL@git.example.org/a.git"}})
    assert e.value.code == "credentials_in_url" and "tok-INURL" not in e.value.message
    with pytest.raises(CodeError) as e:
        _open(svc, TA, ANA, {"repo": {"url": "https://evil.example.com/a.git"}})
    assert e.value.code == "host_not_allowed"
    # une valeur dans le corps n'est jamais acceptée
    with pytest.raises(CodeError) as e:
        _open(svc, TA, ANA, {"repo": {"url": "https://git.example.org/a.git", "token": "tok-BODY"}})
    assert e.value.code == "secret_value_refused"


def test_clone_failure_is_redacted_and_leaves_nothing(svc, cluster):
    from nokker.code_sessions.forge import ForgeError

    def failing(url, dest, token, **_):
        raise ForgeError("clone_failed", f"fatal: auth failed for https://x:{token}@git.example.org/a.git ({token})")

    svc.cloner = failing
    with tenant_scope(TA):
        svc.put_credential("bot", "tok-FAIL-77")
        with pytest.raises(CodeError) as e:
            svc.create(ANA, {"repo": {"url": "https://git.example.org/a.git", "credential": "bot"}})
        assert svc.list(ANA)["sessions"] == []
    assert e.value.status == 502 and "tok-FAIL-77" not in e.value.message
    assert os.listdir(os.path.join(cluster.workspace_root, TA, ANA)) == []


def test_modes(svc):
    assert _open(svc, TA, ANA, {"mode": "plan"})["mode"] == "plan"
    with pytest.raises(CodeError) as e:
        _open(svc, TA, ANA, {"mode": "auto"})  # pas dans code.modes par défaut
    assert e.value.code == "bad_mode"


def test_restart_reopens_from_the_store(svc, cluster, fake_upstream):
    a = _open(svc, TA, ANA)
    _call(svc, TA, ANA, f"{TA}:{a['id']}", "write_file", {"path": "f.txt", "content": "avant"})
    again = CodeSessions(cfg=svc.cfg, store=svc.store, secrets=svc.secrets)  # noyau redémarré, même base
    try:
        assert "avant" in _call(again, TA, ANA, f"{TA}:{a['id']}", "read_file", {"path": "f.txt"}).content
    finally:
        again.close_all()


def test_tools_outside_a_code_session(svc):
    r = _call(svc, TA, ANA, f"{TA}:th_plain", "read_file", {"path": "x"})
    assert r.is_error and "/v1/code/sessions" in r.content
    specs = {s.name for s in svc.tools().specs()}
    assert {"read_file", "write_file", "patch", "terminal", "search_files", "todo"} <= specs


def test_a_long_clone_blocks_nobody_else(svc):
    import threading

    started, release = threading.Event(), threading.Event()

    def slow(url, dest, token, **_):
        started.set()
        assert release.wait(10)

    b = _open(svc, TA, BEN)
    svc.cloner = slow
    t = threading.Thread(target=lambda: _open(svc, TA, ANA, {"repo": {"url": "https://git.example.org/a.git"}}))
    t.start()
    try:
        assert started.wait(5)
        r = _call(svc, TA, BEN, f"{TA}:{b['id']}", "write_file", {"path": "x", "content": "1"})  # pendant le clone
        assert not r.is_error
        assert _open(svc, TB, CAM)["status"] == "active"
    finally:
        release.set()
        t.join(10)
