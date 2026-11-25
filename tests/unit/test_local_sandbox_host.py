"""Bac docker pour un hôte qui embarque le noyau (`local.code.host_volumes`, `network`, `require_sandbox`) : le modèle ne
lit ni le jeton local, ni l'environnement du noyau, ni son état, et ne peut donc pas approuver ses propres cartes.
Sans docker ni amont : l'environnement docker est remplacé par un enregistreur d'arguments."""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from nokker.local.api import LocalApi, SandboxRequired
from nokker.tools.code import CodeToolSource

SECRET_ENV = "NOKKER_TEST_KERNEL_SECRET"


@pytest.fixture()
def recorded(monkeypatch):
    """Faux amont : `DockerEnvironment` note ses arguments, les opérations de fichier ne font rien."""
    calls: list[dict] = []

    class FakeDocker:
        def __init__(self, **kw):
            calls.append(kw)

        def cleanup(self):
            pass

    mods = {
        "tools": types.ModuleType("tools"),
        "tools.environments": types.ModuleType("tools.environments"),
        "tools.environments.docker": types.ModuleType("tools.environments.docker"),
        "tools.environments.local": types.ModuleType("tools.environments.local"),
        "tools.file_operations": types.ModuleType("tools.file_operations"),
        "tools.todo_tool": types.ModuleType("tools.todo_tool"),
    }
    d = mods["tools.environments.docker"]
    d.DockerEnvironment, d._readonly_skill_mount_args, d._BASE_SECURITY_ARGS = FakeDocker, (lambda: ["x"]), ["/tmp:rw"]
    mods["tools.environments.local"].LocalEnvironment = lambda **kw: object()
    mods["tools.file_operations"].ShellFileOperations = lambda *a, **kw: object()
    mods["tools.todo_tool"].TodoStore = lambda: object()
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)
    monkeypatch.setenv(SECRET_ENV, "valeur-du-noyau-a-ne-jamais-voir")
    return calls


def _api(tmp_path, **code):
    root = tmp_path / "runs"
    (root / "abc").mkdir(parents=True, exist_ok=True)
    state = tmp_path / "etat"
    cfg = {"token_path": str(state / "local-token"), "state_dir": str(state),
           "code": {"roots": [str(root)], **code}}
    return LocalApi.from_config(cfg, port=8742), root


def test_named_volume_is_mounted_by_subpath_only(tmp_path, recorded):
    root = (tmp_path / "runs")
    api, root = _api(tmp_path, backend="docker", host_volumes={str(root): "hote_runs"}, image="img:1",
                     network_name="hote-sandbox", network=False)
    cwd = str((root / "abc").resolve())
    api.code.open("th_1", cwd, os.path.realpath(root))
    api.code.for_thread("th_1").workspace("th_1")
    kw = recorded[0]
    assert kw["image"] == "img:1" and kw["network"] is False and kw["volumes"] == []
    args = kw["extra_args"]
    assert args[:2] == ["--mount", "type=volume,source=hote_runs,target=/workspace,volume-subpath=abc"]
    assert "--network" not in args  # réseau coupé : le nom de réseau n'est pas posé


def test_session_at_root_mounts_whole_volume_without_subpath(tmp_path, recorded):
    root = tmp_path / "runs"
    api, root = _api(tmp_path, host_volumes={str(root): "v"})
    api.code.open("th_r", os.path.realpath(root), os.path.realpath(root))
    api.code.for_thread("th_r").workspace("th_r")
    assert recorded[0]["extra_args"][1] == "type=volume,source=v,target=/workspace"


@pytest.mark.parametrize("bad", ["a,b", "x=y,target=/etc", "../up", "a b"])
def test_volume_subpath_cannot_inject_mount_options(tmp_path, bad):
    with pytest.raises(ValueError):
        CodeToolSource(root=str(tmp_path), backend="docker", volume="v", volume_subpath=bad)


def test_volume_name_is_validated(tmp_path):
    with pytest.raises(ValueError):
        CodeToolSource(root=str(tmp_path), backend="docker", volume="v,target=/")


def test_path_mount_stays_the_default(tmp_path, recorded):
    api, root = _api(tmp_path, backend="docker")
    cwd = str((root / "abc").resolve())
    api.code.open("th_2", cwd, os.path.realpath(root))
    api.code.for_thread("th_2").workspace("th_2")
    kw = recorded[0]
    assert kw["volumes"] == [f"{cwd}:/workspace"] and kw["network"] is True
    assert not any(a == "--mount" for a in kw["extra_args"])


def test_sandbox_sees_nothing_of_the_kernel(tmp_path, recorded):
    """Ce que le bac reçoit : HOME et rien d'autre ; pas de transfert d'environnement ; le seul montage est le dossier
    de la session ; le jeton, l'état et la config du noyau ne sont ni dans ce montage ni sous sa racine."""
    api, root = _api(tmp_path, backend="docker", require_sandbox=True)
    cwd = str((root / "abc").resolve())
    api.code.open("th_3", cwd, os.path.realpath(root))
    api.code.for_thread("th_3").workspace("th_3")
    kw = recorded[0]
    assert kw["env"] == {"HOME": "/tmp"} and "forward_env" not in kw
    blob = repr(kw)
    assert api.token not in blob and "valeur-du-noyau" not in blob
    assert str(tmp_path / "etat") not in blob and "local-token" not in blob
    assert kw["volumes"] == [f"{cwd}:/workspace"]
    token_file = os.path.realpath(tmp_path / "etat" / "local-token")
    assert not token_file.startswith(os.path.realpath(root) + os.sep)
    assert os.path.exists(token_file) and Path(token_file).read_text().strip() == api.token


def test_model_cannot_approve_without_the_token(tmp_path):
    """La décision d'une carte demande le jeton local : sans lui (le bac ne l'a pas), refus."""
    api, _ = _api(tmp_path, backend="docker", require_sandbox=True)
    assert not api.is_owner(None) and not api.is_owner("") and not api.is_owner("Bearer ")
    assert not api.is_owner("Bearer devine") and not api.is_owner(api.token)  # sans « Bearer »
    assert api.is_owner(f"Bearer {api.token}")


def test_require_sandbox_refuses_the_local_backend(tmp_path):
    with pytest.raises(SandboxRequired, match="backend « local » refusé"):
        _api(tmp_path, backend="local", require_sandbox=True)
    _api(tmp_path, backend="local")  # drapeau éteint : comportement inchangé


def test_require_sandbox_refuses_a_root_that_holds_the_token(tmp_path):
    state = tmp_path / "work"
    state.mkdir()
    cfg = {"token_path": str(state / "local-token"), "state_dir": str(tmp_path / "etat"),
           "code": {"roots": [str(state)], "backend": "docker", "require_sandbox": True}}
    with pytest.raises(SandboxRequired, match="se recouvrent"):
        LocalApi.from_config(cfg, port=8742)
    cfg["code"]["roots"] = [str(tmp_path)]  # la racine contient aussi le dossier d'état
    with pytest.raises(SandboxRequired):
        LocalApi.from_config(cfg, port=8742)
    cfg["code"]["require_sandbox"] = False
    LocalApi.from_config(cfg, port=8742)


@pytest.mark.skipif(not os.environ.get("NOKKER_TEST_DOCKER_ROOT"), reason="NOKKER_TEST_DOCKER_ROOT absent")
def test_real_sandbox_hides_token_env_and_state(tmp_path, monkeypatch):
    """Joué avec un vrai docker et l'amont importable (PYTHONPATH) : depuis le bac, ni le jeton local, ni l'environnement
    du noyau, ni son dossier d'état ne se lisent ; l'API locale (loopback du noyau) n'y est pas joignable."""
    import shutil
    import uuid

    from nokker.core.ports import ToolContext

    base = os.environ["NOKKER_TEST_DOCKER_ROOT"]
    top = os.path.join(base, f"host-{uuid.uuid4().hex[:8]}")
    os.makedirs(os.path.join(top, "runs", "abc"))
    os.makedirs(os.path.join(top, "etat"))
    monkeypatch.setenv("HERMES_HOME", os.path.join(base, ".hermes"))
    monkeypatch.setenv(SECRET_ENV, "valeur-du-noyau-a-ne-jamais-voir")
    cfg = {"token_path": os.path.join(top, "etat", "local-token"), "state_dir": os.path.join(top, "etat"),
           "code": {"roots": [os.path.join(top, "runs")], "backend": "docker", "require_sandbox": True,
                    "image": os.environ.get("NOKKER_TEST_SANDBOX_IMAGE", "ghcr.io/nokker-ai/sandbox:0.1.0-rc.9.28")}}
    api = LocalApi.from_config(cfg, port=8742)
    root = os.path.realpath(os.path.join(top, "runs"))
    cwd = os.path.join(root, "abc")
    api.code.open("th_real", cwd, root)
    ctx = ToolContext(thread_id="th_real", turn_index=0, channel="api")
    try:
        cmd = (f"env; cat {os.path.join(top, 'etat', 'local-token')} 2>&1; ls {top} 2>&1; ls /proc/1/environ 2>&1; "
               "cat /proc/1/environ 2>&1 | tr '\\0' '\\n'; (curl -s -m 3 http://127.0.0.1:8742/v1/local/sessions "
               "|| wget -q -T 3 -O- http://127.0.0.1:8742/ ) 2>&1; echo FIN")
        out = api.code.for_thread("th_real").call("terminal", {"command": cmd}, ctx).content
        assert "FIN" in out
        assert api.token not in out and "valeur-du-noyau" not in out and SECRET_ENV not in out
        assert "No such file" in out  # ni le jeton ni le dossier d'état n'existent dans le bac
    finally:
        api.code.for_thread("th_real").close()
        shutil.rmtree(top, ignore_errors=True)
