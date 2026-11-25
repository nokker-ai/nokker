"""Backend docker demandé et bac injoignable : refus net, rien d'exécuté, jamais de repli sur la machine.

Sans docker ni amont : un faux exécutable `docker` (script) et un faux environnement amont qui échoue comme le vrai
(`EnvironmentConnectionError`, constaté sur un `DOCKER_HOST` injoignable)."""
from __future__ import annotations

import sys
import types

import pytest

import nokker.local.api as api_mod
from nokker.core.ports import ToolContext
from nokker.local.api import LocalApi
from nokker.tools.code import SANDBOX_UNAVAILABLE, CodeToolSource, SandboxUnavailable, check_sandbox

CTX = ToolContext(thread_id="th_x", turn_index=0, channel="api")


def _docker(tmp_path, body: str) -> str:
    exe = tmp_path / "docker"
    exe.write_text(f"#!/bin/sh\n{body}\n")
    exe.chmod(0o755)
    return str(exe)


def test_check_sandbox_refuses_when_docker_fails_is_missing_or_hangs(tmp_path):
    check_sandbox(_docker(tmp_path, "echo 27.0"))  # répond : rien
    with pytest.raises(SandboxUnavailable) as e:
        check_sandbox(_docker(tmp_path, "echo 'Cannot connect to the Docker daemon' >&2; exit 1"))
    assert str(e.value) == SANDBOX_UNAVAILABLE and "Cannot connect" in e.value.detail
    with pytest.raises(SandboxUnavailable):
        check_sandbox(str(tmp_path / "absent"))
    with pytest.raises(SandboxUnavailable):
        check_sandbox(_docker(tmp_path, "sleep 5"), timeout=0.3)


def test_the_message_is_plain_and_says_what_to_do():
    assert "bac à sable docker n'est pas disponible" in SANDBOX_UNAVAILABLE
    assert "docker est-il démarré" in SANDBOX_UNAVAILABLE and "ta machine" not in SANDBOX_UNAVAILABLE
    assert "Local" not in SANDBOX_UNAVAILABLE and "hermes" not in SANDBOX_UNAVAILABLE.lower()


@pytest.fixture()
def upstream(monkeypatch):
    """Faux amont : `DockerEnvironment` lève comme le vrai tant que `state["up"]` est faux."""
    state = {"up": False, "made": 0}

    class EnvironmentConnectionError(Exception):
        pass

    class FakeDocker:
        def __init__(self, **kw):
            if not state["up"]:
                raise EnvironmentConnectionError("Docker command is available but 'docker version' failed. "
                                                 "Run `hermes setup terminal` to switch to Local.")
            state["made"] += 1

        def cleanup(self):
            pass

    mods = {k: types.ModuleType(k) for k in ("tools", "tools.environments", "tools.environments.docker",
                                             "tools.environments.local", "tools.file_operations", "tools.todo_tool")}
    d = mods["tools.environments.docker"]
    d.DockerEnvironment, d._readonly_skill_mount_args, d._BASE_SECURITY_ARGS = FakeDocker, list, ["/tmp:rw"]
    mods["tools.environments.local"].LocalEnvironment = lambda **kw: object()
    mods["tools.file_operations"].ShellFileOperations = lambda *a, **kw: object()
    mods["tools.todo_tool"].TodoStore = lambda: object()
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)
    return state


def test_first_tool_call_is_refused_clearly_then_works_once_the_sandbox_is_back(tmp_path, upstream):
    src = CodeToolSource(root=str(tmp_path), backend="docker")
    for name, args in (("terminal", {"command": "id"}), ("write_file", {"path": "a", "content": "x"})):
        r = src.call(name, args, CTX)
        assert r.is_error and r.content == f"REFUSÉ : {SANDBOX_UNAVAILABLE}"
        assert "hermes" not in r.content.lower() and "Local" not in r.content  # le conseil de l'amont ne passe pas
    assert upstream["made"] == 0 and src.backend == "docker"  # rien d'ouvert, pas de repli
    upstream["up"] = True
    src.workspace("th_x")  # le bac est revenu : l'espace s'ouvre (rien n'a été mis en cache à tort)
    assert upstream["made"] == 1


def _api(tmp_path):
    root = tmp_path / "projets"
    (root / "app").mkdir(parents=True)
    return LocalApi.from_config({"token_path": str(tmp_path / "tok"), "code": {"roots": [str(root)],
                                                                             "backend": "docker"}}, port=8742), root


def test_session_open_is_a_503_when_the_sandbox_is_down_and_a_201_when_it_is_back(tmp_path, monkeypatch, upstream):
    api, root = _api(tmp_path)
    auth = f"Bearer {api.token}"
    docker = _docker(tmp_path, "echo 'Cannot connect' >&2; exit 1")
    monkeypatch.setattr(api_mod, "check_sandbox", lambda: check_sandbox(docker))
    status, body = api.handle("POST", "/v1/local/sessions", auth, {"cwd": str(root / "app")})
    assert status == 503
    assert body == {"error": {"code": "sandbox_unavailable", "message": SANDBOX_UNAVAILABLE}}
    assert api.code.sessions() == {}  # aucun fil ouvert, aucun CodeToolSource (donc aucun repli) créé
    assert api.code.backend == "docker"
    docker = _docker(tmp_path, "echo 27.0")
    status, body = api.handle("POST", "/v1/local/sessions", auth, {"cwd": str(root / "app")})
    assert status == 201 and body["thread_id"] in api.code.sessions()


def test_other_validations_still_come_first(tmp_path, monkeypatch):
    api, _root = _api(tmp_path)
    monkeypatch.setattr(api_mod, "check_sandbox", lambda: pytest.fail("la sonde ne passe qu'après les contrôles"))
    assert api.handle("POST", "/v1/local/sessions", f"Bearer {api.token}", {"cwd": "/etc"})[0] == 403


def test_cli_code_refuses_with_exit_3_when_the_sandbox_is_down(monkeypatch, tmp_path, capsys):
    import nokker.cli as cli_mod

    monkeypatch.setattr("nokker.tools.code.check_sandbox",
                        lambda *a, **k: (_ for _ in ()).throw(SandboxUnavailable("daemon")))
    monkeypatch.setattr(cli_mod, "build_kernel", lambda *a, **k: pytest.fail("rien ne doit démarrer"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{}\n", encoding="utf-8")
    args = cli_mod.build_parser().parse_args(["--config", str(cfg), "code", "tâche", "--workspace", str(tmp_path)])
    assert args.func(args) == 3
    cap = capsys.readouterr()
    assert cap.out == "" and SANDBOX_UNAVAILABLE in cap.err
