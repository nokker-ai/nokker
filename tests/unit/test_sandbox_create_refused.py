"""Échec de CRÉATION du bac (`docker run` refusé par le démon) : refus net, jamais un 502 ni une trace Python.

Constat « avant » : avec `cpus: 4` sur une machine à 2 CPU, `docker run` échoue (`CalledProcessError`), la trace part au
journal et `POST /v1/local/sessions` répond 502 au lieu du refus propre. Maintenant : `SandboxUnavailable` → 503
`sandbox_unavailable`, message clair qui cite ce qui est demandé et la limite que docker donne, sans trace. Faux démon."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import types

import pytest

from nokker.local.api import LocalApi
from nokker.tools.code import SandboxCreateRefused

RANGE_CPUS = ("docker: Error response from daemon: Range of CPUs is from 0.01 to 2.00, as there are only 2 CPUs "
              "available.\nSee 'docker run --help'.")
MIN_MEMORY = "docker: Error response from daemon: Minimum memory limit allowed is 6MB.\nSee 'docker run --help'."
NO_IMAGE = ("Unable to find image 'nokker/sandbox:x' locally\ndocker: Error response from daemon: pull access denied for "
            "nokker/sandbox, repository does not exist.\nSee 'docker run --help'.")


@pytest.fixture()
def refusing_daemon(monkeypatch):
    """Faux démon : `DockerEnvironment(...)` lève ce que lève l'amont quand `docker run` sort avec un code non nul."""
    box: dict = {"stderr": RANGE_CPUS, "created": 0}

    class FakeDocker:
        def __init__(self, **kw):
            box["created"] += 1
            box["kw"] = kw
            raise subprocess.CalledProcessError(
                125, ["docker", "run", "-d", "--cpus", "4", "-e", "SECRET_TOKEN_VALUE=hunter2"], output="", stderr=box["stderr"])

    mods = {n: types.ModuleType(n) for n in ("tools", "tools.environments", "tools.environments.docker",
                                             "tools.environments.local", "tools.file_operations", "tools.todo_tool")}
    d = mods["tools.environments.docker"]
    d.DockerEnvironment, d._readonly_skill_mount_args, d._BASE_SECURITY_ARGS = FakeDocker, list, ["/tmp:rw"]
    mods["tools.environments.local"].LocalEnvironment = lambda **kw: object()
    mods["tools.file_operations"].ShellFileOperations = lambda *a, **kw: object()
    mods["tools.todo_tool"].TodoStore = lambda: object()
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)
    monkeypatch.setattr("nokker.local.api.check_sandbox", lambda *a, **kw: None)  # le démon répond ; c'est `run` qui échoue
    return box


def _api(tmp_path, **code):
    root = tmp_path / "runs"
    (root / "abc").mkdir(parents=True, exist_ok=True)
    state = tmp_path / "etat"
    cfg = {"token_path": str(state / "local-token"), "state_dir": str(state), "instance": "h3eunit",
           "code": {"roots": [str(root)], "backend": "docker", **code}}
    return LocalApi.from_config(cfg, port=8742), os.path.realpath(root)


def _open_route(api, root):
    return api.handle("POST", "/v1/local/sessions", f"Bearer {api.token}", {"cwd": os.path.join(root, "abc")})


def test_invalid_cpu_range_answers_503_citing_the_value_asked_and_the_machine_limit(tmp_path, refusing_daemon, caplog):
    caplog.set_level(logging.DEBUG)
    api, root = _api(tmp_path, cpus=4)
    status, body = _open_route(api, root)
    assert status == 503 and body["error"]["code"] == "sandbox_unavailable"
    msg = body["error"]["message"]
    assert "cpus 4" in msg and "Range of CPUs is from 0.01 to 2.00, as there are only 2 CPUs available." in msg
    assert "docker run" not in msg and "SECRET_TOKEN_VALUE" not in msg and "See 'docker" not in msg  # jamais la ligne de commande
    assert api.code.sessions() == {}  # pas de session sans bac
    assert refusing_daemon["created"] == 1


def test_no_python_trace_reaches_the_log(tmp_path, refusing_daemon, caplog):
    caplog.set_level(logging.DEBUG)
    api, root = _api(tmp_path, cpus=4)
    _open_route(api, root)
    assert not any(r.exc_info for r in caplog.records)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Traceback" not in text and "CalledProcessError" not in text and "hunter2" not in text


def test_invalid_memory_range_is_refused_the_same_way(tmp_path, refusing_daemon):
    refusing_daemon["stderr"] = MIN_MEMORY
    api, root = _api(tmp_path, memory="64m")
    status, body = _open_route(api, root)
    assert status == 503 and body["error"]["code"] == "sandbox_unavailable"
    assert "mémoire 64 Mo" in body["error"]["message"] and "Minimum memory limit allowed is 6MB." in body["error"]["message"]


def test_any_other_daemon_refusal_is_refused_the_same_way(tmp_path, refusing_daemon):
    refusing_daemon["stderr"] = NO_IMAGE
    api, root = _api(tmp_path, cpus=1)
    status, body = _open_route(api, root)
    assert status == 503 and "pull access denied for nokker/sandbox, repository does not exist." in body["error"]["message"]


def test_refusal_without_stderr_still_gives_a_clean_message(tmp_path, refusing_daemon):
    refusing_daemon["stderr"] = ""
    api, root = _api(tmp_path, cpus=1)
    status, body = _open_route(api, root)
    assert status == 503 and "CalledProcessError" in body["error"]["message"]  # le type, jamais une trace


def test_a_timeout_while_creating_is_a_refusal_too(tmp_path, refusing_daemon, monkeypatch):
    def boom(**kw):
        raise subprocess.TimeoutExpired(["docker", "run"], 120)

    monkeypatch.setattr(sys.modules["tools.environments.docker"], "DockerEnvironment", boom)
    api, root = _api(tmp_path, cpus=1)
    status, body = _open_route(api, root)
    assert status == 503 and "délai dépassé" in body["error"]["message"]


def test_a_programming_error_is_not_swallowed(tmp_path, refusing_daemon, monkeypatch):
    def boom(**kw):
        raise TypeError("bug")

    monkeypatch.setattr(sys.modules["tools.environments.docker"], "DockerEnvironment", boom)
    api, root = _api(tmp_path, cpus=1)
    with pytest.raises(TypeError):
        api.code.open("th_1", os.path.join(root, "abc"), root)


def test_without_limits_the_failure_comes_at_the_first_tool_call_and_the_model_sees_no_docker_text(tmp_path, refusing_daemon):
    from nokker.core.ports import ToolContext

    api, root = _api(tmp_path)  # pas de plafond : le bac n'est créé qu'au premier appel d'outil
    api.code.open("th_1", os.path.join(root, "abc"), root)
    src = api.code.for_thread("th_1")
    res = src.call("terminal", {"command": "ls"}, ToolContext(thread_id="th_1", turn_index=0, channel="api", sender="owner"))
    assert res.is_error and res.content.startswith("REFUSÉ : Le bac à sable n'a pas pu démarrer")
    assert "Range of CPUs" not in res.content and "docker run" not in res.content


def test_create_refused_is_a_sandbox_unavailable():
    from nokker.tools.code import SandboxUnavailable

    assert issubclass(SandboxCreateRefused, SandboxUnavailable)
