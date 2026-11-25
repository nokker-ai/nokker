"""Plafonds du bac docker (`local.code.memory`, `local.code.cpus`) et sa fin de vie (fermeture de session, expiration,
arrêt du noyau) pour un hôte qui embarque le noyau. Sans docker ni amont : l'environnement est un enregistreur ; un test
sous docker réel (NOKKER_TEST_DOCKER_ROOT) établit que le conteneur porte les plafonds et disparaît."""
from __future__ import annotations

import os
import subprocess
import sys
import types

import pytest

from nokker.local.api import LocalApi
from nokker.tools.code import CodeToolSource, parse_cpus_limit, parse_memory_limit

TOKEN_HDR = "Bearer {}"


@pytest.fixture()
def recorded(monkeypatch):
    class _Calls(list):
        applied: dict

    calls = _Calls()

    class FakeDocker:
        def __init__(self, **kw):
            self.kw = kw
            self._container_id = f"cid{len(calls)}"
            calls.append(self)

        cleanups = 0

        def cleanup(self):
            self.cleanups += 1

    mods = {n: types.ModuleType(n) for n in ("tools", "tools.environments", "tools.environments.docker",
                                             "tools.environments.local", "tools.file_operations", "tools.todo_tool")}
    d = mods["tools.environments.docker"]
    d.DockerEnvironment, d._readonly_skill_mount_args, d._BASE_SECURITY_ARGS = FakeDocker, list, ["/tmp:rw"]
    d.DockerEnvironment.wait_for_all_teardowns = staticmethod(lambda timeout=0: True)
    mods["tools.environments.local"].LocalEnvironment = lambda **kw: object()
    mods["tools.file_operations"].ShellFileOperations = lambda *a, **kw: object()
    mods["tools.todo_tool"].TodoStore = lambda: object()
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)
    # `docker inspect` simulé : par défaut le démon pose exactement ce que le bac a demandé
    applied: dict = {}
    monkeypatch.setattr(CodeToolSource, "_inspect_limits", staticmethod(
        lambda cid: applied.get("limits") or (calls[int(cid[3:])].kw["memory"] * 1024 * 1024,
                                              round(calls[int(cid[3:])].kw["cpu"] * 1e9))))
    calls.applied = applied
    return calls


def _api(tmp_path, **code):
    root = tmp_path / "runs"
    (root / "abc").mkdir(parents=True, exist_ok=True)
    state = tmp_path / "etat"
    cfg = {"token_path": str(state / "local-token"), "state_dir": str(state), "instance": "h2unit",
           "code": {"roots": [str(root)], "backend": "docker", **code}}
    return LocalApi.from_config(cfg, port=8742), os.path.realpath(root)


def _open(api, root, tid="th_1"):
    api.code.open(tid, os.path.join(root, "abc"), root)
    api.code.for_thread(tid).workspace(tid)


# ── plafonds ──

@pytest.mark.parametrize("raw,mb", [("2g", 2048), ("2G", 2048), ("512m", 512), ("64m", 64), ("1024g", 1048576),
                                    (None, 0), ("", 0), ("  ", 0), (False, 0)])
def test_memory_parse_ok(raw, mb):
    assert parse_memory_limit(raw) == mb


@pytest.mark.parametrize("bad", ["2", "2gb", "2g --privileged", "2g;x", "-1g", "0g", "63m", "1025g", "1.5g", "2 g",
                                 "--memory=1g", "0x10m", "１g", 2, 2.0, True, ["2g"], {"a": 1}, "m", "g2"])
def test_memory_parse_refuses(bad):
    with pytest.raises(ValueError):
        parse_memory_limit(bad)


@pytest.mark.parametrize("raw,n", [(2, 2.0), (1.5, 1.5), ("2", 2.0), ("0.5", 0.5), (0, 0.0), (None, 0.0), ("", 0.0),
                                   ("256", 256.0), (0.1, 0.1)])
def test_cpus_parse_ok(raw, n):
    assert parse_cpus_limit(raw) == n


@pytest.mark.parametrize("bad", [True, -1, "-1", "1e3", "inf", "nan", "2 --privileged", "2;", "0.05", 257, "257", "abc",
                                 "1.234", ["2"], "٢", "2\n3"])
def test_cpus_parse_refuses(bad):
    with pytest.raises(ValueError):
        parse_cpus_limit(bad)


def test_limits_are_off_by_default(tmp_path, recorded):
    api, root = _api(tmp_path)
    _open(api, root)
    kw = recorded[0].kw
    assert kw["memory"] == 0 and kw["cpu"] == 0 and not any("memory" in str(a) or "cpus" in str(a) for a in kw["extra_args"])


def test_limits_reach_the_sandbox(tmp_path, recorded):
    api, root = _api(tmp_path, memory="2g", cpus=2)
    _open(api, root)
    assert recorded[0].kw["memory"] == 2048 and recorded[0].kw["cpu"] == 2.0


def test_bad_limit_stops_the_config(tmp_path):
    with pytest.raises(ValueError, match="local.code.memory"):
        _api(tmp_path, memory="2g --privileged")
    with pytest.raises(ValueError, match="local.code.cpus"):
        _api(tmp_path, cpus="2; rm")
    with pytest.raises(ValueError, match="idle_ttl_s"):
        _api(tmp_path, idle_ttl_s=5)


def test_source_validates_its_own_limits(tmp_path):
    with pytest.raises(ValueError):
        CodeToolSource(root=str(tmp_path), backend="docker", cpus=-3)
    with pytest.raises(ValueError):
        CodeToolSource(root=str(tmp_path), backend="docker", memory_mb=8)


# ── plafond demandé mais non posé : refus bruyant ──

@pytest.mark.parametrize("posed", [(0, 0), (0, 2_000_000_000), (2048 * 1024 * 1024, 0), (1024 * 1024 * 1024, 2_000_000_000)])
def test_requested_limit_not_applied_refuses_and_removes_the_sandbox(tmp_path, recorded, posed):
    from nokker.tools.code import SandboxLimitsNotApplied

    api, root = _api(tmp_path, memory="2g", cpus=2)
    recorded.applied["limits"] = posed  # le démon n'a pas posé (tout ou partie) du plafond
    with pytest.raises(SandboxLimitsNotApplied, match="plafond mémoire/CPU demandé"):
        api.code.open("th_1", os.path.join(root, "abc"), root)
    assert recorded[0].cleanups == 1 and api.code.sessions() == {}  # bac supprimé, pas de session sans plafond


def test_open_route_answers_503_with_the_clear_message(tmp_path, recorded):
    api, root = _api(tmp_path, memory="2g")
    recorded.applied["limits"] = (0, 0)
    status, body = api.handle("POST", "/v1/local/sessions", f"Bearer {api.token}", {"cwd": os.path.join(root, "abc")})
    assert status == 503 and body["error"]["code"] == "sandbox_unavailable"
    assert "n'a pas été démarré" in body["error"]["message"] and "mémoire demandée 2048 Mo, posée 0 Mo" in body["error"]["message"]
    assert recorded[0].cleanups == 1 and api.code.sessions() == {}


def test_unreadable_inspect_refuses_too(tmp_path, recorded, monkeypatch):
    from nokker.tools.code import SandboxLimitsNotApplied

    def boom(cid):
        raise OSError("docker absent")

    monkeypatch.setattr(CodeToolSource, "_inspect_limits", staticmethod(boom))
    api, root = _api(tmp_path, cpus=1)
    with pytest.raises(SandboxLimitsNotApplied, match="vérification impossible"):
        api.code.open("th_1", os.path.join(root, "abc"), root)


def test_without_limits_nothing_is_inspected(tmp_path, recorded, monkeypatch):
    def boom(cid):
        raise AssertionError("pas d'inspection sans plafond demandé")

    monkeypatch.setattr(CodeToolSource, "_inspect_limits", staticmethod(boom))
    api, root = _api(tmp_path)
    _open(api, root)
    assert recorded[0].cleanups == 0


def test_tool_call_after_idle_expiry_also_refuses(tmp_path, recorded):
    api, root = _api(tmp_path, memory="2g", idle_ttl_s=60)
    _open(api, root)
    src = api.code.for_thread("th_1")
    assert api.code.expire_idle(60, now=src.last_used + 100) == ["th_1"]
    recorded.applied["limits"] = (0, 0)
    r = src.call("terminal", {"command": "true"}, types.SimpleNamespace(thread_id="th_1"))
    assert r.is_error and "n'a pas pu démarrer" in r.content
    from nokker.tools.code import SANDBOX_LIMITS_FOR_MODEL
    assert r.content == f"REFUSÉ : {SANDBOX_LIMITS_FOR_MODEL}" and "local.code" not in r.content and "cgroups" not in r.content


# ── fin de vie ──

def test_delete_route_removes_the_sandbox(tmp_path, recorded):
    api, root = _api(tmp_path)
    _open(api, root)
    hdr = TOKEN_HDR.format(api.token)
    assert api.handle("DELETE", "/v1/local/sessions/th_1", None, {}) == (401, {"error": {"message": "jeton local requis"}})
    assert recorded[0].cleanups == 0
    status, body = api.handle("DELETE", "/v1/local/sessions/th_1", hdr, {})
    assert status == 200 and body == {"closed": True, "thread_id": "th_1"}
    assert recorded[0].cleanups == 1 and "th_1" not in api.code.sessions()
    assert api.handle("DELETE", "/v1/local/sessions/th_1", hdr, {})[0] == 404  # déjà fermée


def test_delete_route_is_refused_through_the_gateway(tmp_path, recorded):
    api, root = _api(tmp_path)
    _open(api, root)
    status, _ = api.handle("DELETE", "/v1/local/sessions/th_1", "Bearer x", {}, relayed=True)
    assert status in (401, 403) and recorded[0].cleanups == 0


def test_idle_expiry_removes_only_idle_sandboxes_and_the_session_stays(tmp_path, recorded):
    api, root = _api(tmp_path, idle_ttl_s=60)
    _open(api, root, "th_a")
    _open(api, root, "th_b")
    src_a, src_b = api.code.for_thread("th_a"), api.code.for_thread("th_b")
    now = src_a.last_used + 100
    src_b.last_used = now - 10  # actif il y a 10 s
    assert api.code.expire_idle(60, now=now) == ["th_a"]
    assert recorded[0].cleanups == 1 and recorded[1].cleanups == 0
    assert set(api.code.sessions()) == {"th_a", "th_b"}  # la session reste, son bac est recréé au prochain appel
    src_a.workspace("th_a")
    assert len(recorded) == 3
    assert api.code.expire_idle(0, now=now) == []  # 0 = jamais


def test_idle_expiry_leaves_a_session_with_a_running_process(tmp_path, recorded):
    api, root = _api(tmp_path, idle_ttl_s=60)
    _open(api, root)
    src = api.code.for_thread("th_1")
    src.has_live_processes = lambda: True
    assert api.code.expire_idle(60, now=src.last_used + 1000) == []
    assert recorded[0].cleanups == 0


def test_a_tool_call_resets_the_idle_clock(tmp_path, recorded):
    api, root = _api(tmp_path, idle_ttl_s=60)
    _open(api, root)
    src = api.code.for_thread("th_1")
    src.last_used = 0.0
    src.call("nope", {}, types.SimpleNamespace(thread_id="th_1"))
    assert src.last_used > 0


def test_kernel_shutdown_closes_every_sandbox(tmp_path, recorded):
    api, root = _api(tmp_path)
    _open(api, root, "th_a")
    _open(api, root, "th_b")
    api.code.close()
    assert [c.cleanups for c in recorded] == [1, 1] and api.code.sessions() == {}


def test_reaper_thread_starts_only_with_a_ttl(tmp_path, recorded):
    api, _ = _api(tmp_path)
    api.code.start_reaper()
    assert api.code._reaper is None
    api2, _ = _api(tmp_path / "b", idle_ttl_s=60)
    api2.code.start_reaper(interval_s=0.05)
    assert api2.code._reaper is not None and api2.code._reaper.is_alive()
    api2.code.close()
    api2.code._reaper.join(2)
    assert not api2.code._reaper.is_alive()


def test_reaper_thread_removes_an_idle_sandbox_and_a_new_call_recreates_it(tmp_path, recorded):
    """Un utilisateur qui quitte sans fermer la session : le fil de ménage retire le bac après `idle_ttl_s` sans appel,
    la session reste ouverte, l'appel suivant recrée le bac (le dossier monté garde les fichiers)."""
    import time

    api, root = _api(tmp_path, idle_ttl_s=60)
    _open(api, root)
    src = api.code.for_thread("th_1")
    src.last_used = time.monotonic() - 3600  # sans appel depuis une heure
    api.code.start_reaper(interval_s=0.05)
    deadline = time.monotonic() + 5
    while recorded[0].cleanups == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        assert recorded[0].cleanups == 1 and set(api.code.sessions()) == {"th_1"}
        src.workspace("th_1")  # l'appel suivant
        assert len(recorded) == 2 and recorded[1].cleanups == 0
    finally:
        api.code.close()


# ── docker réel ──

@pytest.mark.skipif(not os.environ.get("NOKKER_TEST_DOCKER_ROOT"), reason="NOKKER_TEST_DOCKER_ROOT absent")
@pytest.mark.parametrize("how", ["delete", "idle", "shutdown"])
def test_real_docker_limits_and_removal(tmp_path, how):
    import shutil
    import uuid

    from nokker.core.ports import ToolContext

    base = os.environ["NOKKER_TEST_DOCKER_ROOT"]
    top = os.path.join(base, f"h2limits-{uuid.uuid4().hex[:8]}")
    os.makedirs(os.path.join(top, "runs", "abc"))
    os.makedirs(os.path.join(top, "etat"))
    inst = f"h2test-{uuid.uuid4().hex[:8]}"
    cfg = {"token_path": os.path.join(top, "etat", "t"), "state_dir": os.path.join(top, "etat"), "instance": inst,
           "code": {"roots": [os.path.join(top, "runs")], "backend": "docker", "memory": "256m", "cpus": 1.5,
                    "idle_ttl_s": 60, "image": os.environ.get("NOKKER_TEST_SANDBOX_IMAGE",
                                                              "ghcr.io/nokker-ai/sandbox:0.1.0-rc.9.31")}}
    api = LocalApi.from_config(cfg, port=8742)
    root = os.path.realpath(os.path.join(top, "runs"))

    def ids():
        return subprocess.run(["docker", "ps", "-aq", "--filter", f"label=nokker.instance={inst}"],
                              capture_output=True, text=True, check=False).stdout.split()

    try:
        api.code.open("th_r", os.path.join(root, "abc"), root)
        assert "returncode" in api.code.for_thread("th_r").call(
            "terminal", {"command": "echo ok"}, ToolContext(thread_id="th_r", turn_index=0, channel="api")).content
        (cid,) = ids()
        mem, nano = subprocess.run(["docker", "inspect", "--format", "{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}", cid],
                                   capture_output=True, text=True, check=True).stdout.split()
        assert (int(mem), int(nano)) == (256 * 1024 * 1024, 1_500_000_000)
        if how == "delete":
            assert api.handle("DELETE", "/v1/local/sessions/th_r", f"Bearer {api.token}", {})[0] == 200
        elif how == "idle":
            assert api.code.expire_idle(60, now=api.code.for_thread("th_r").last_used + 61) == ["th_r"]
        else:
            api.code.close()
        assert ids() == []
    finally:
        api.code.close()
        left = ids()
        if left:
            subprocess.run(["docker", "rm", "-f", *left], capture_output=True, check=False)
        shutil.rmtree(top, ignore_errors=True)


@pytest.mark.skipif(not os.environ.get("NOKKER_TEST_DOCKER_ROOT"), reason="NOKKER_TEST_DOCKER_ROOT absent")
def test_real_docker_refused_limit_leaves_no_container(monkeypatch):
    """Vrai conteneur créé, lecture des plafonds simulée à zéro (démon qui ne les pose pas) : refus, conteneur supprimé."""
    import shutil
    import uuid

    from nokker.tools.code import SandboxLimitsNotApplied

    base = os.environ["NOKKER_TEST_DOCKER_ROOT"]
    top = os.path.join(base, f"h2refuse-{uuid.uuid4().hex[:8]}")
    os.makedirs(os.path.join(top, "runs", "abc"))
    os.makedirs(os.path.join(top, "etat"))
    inst = f"h2test-{uuid.uuid4().hex[:8]}"
    cfg = {"token_path": os.path.join(top, "etat", "t"), "state_dir": os.path.join(top, "etat"), "instance": inst,
           "code": {"roots": [os.path.join(top, "runs")], "backend": "docker", "memory": "256m",
                    "image": os.environ.get("NOKKER_TEST_SANDBOX_IMAGE", "ghcr.io/nokker-ai/sandbox:0.1.0-rc.9.31")}}
    monkeypatch.setattr(CodeToolSource, "_inspect_limits", staticmethod(lambda cid: (0, 0)))
    api = LocalApi.from_config(cfg, port=8742)
    root = os.path.realpath(os.path.join(top, "runs"))
    try:
        with pytest.raises(SandboxLimitsNotApplied):
            api.code.open("th_x", os.path.join(root, "abc"), root)
        left = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=nokker.instance={inst}"],
                              capture_output=True, text=True, check=False).stdout.split()
        assert left == []
    finally:
        api.code.close()
        left = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=nokker.instance={inst}"],
                              capture_output=True, text=True, check=False).stdout.split()
        if left:
            subprocess.run(["docker", "rm", "-f", *left], capture_output=True, check=False)
        shutil.rmtree(top, ignore_errors=True)
