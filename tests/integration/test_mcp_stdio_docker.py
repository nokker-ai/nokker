"""Serveurs MCP stdio, de bout en bout dans un VRAI container docker (docs/NOKKAN-MCP-STDIO.md §7).

Ce que les tests unitaires simulent (le bac à sable) est réel ici : le serveur de test (SDK officiel) tourne DANS un
container, lancé par le vrai `CodeToolSource.script_command` (`docker exec -i -e NOM -w …`), le noyau ne tenant que le
tube ; le ménage passe par le vrai `CodeToolSource.run_hook`. Seul l'environnement d'exécution amont
(`tools.environments.docker.DockerEnvironment`, dépendance hermes-nokker) est remplacé par un `docker exec` direct sur
un container créé par ce test : la création du container par l'amont n'est pas exercée.

À lancer à la main, sur une machine avec docker :
    NOKKAN_DOCKER_E2E=1 pytest tests/integration/test_mcp_stdio_docker.py
Le container porte l'étiquette `nokkan-mcp-stdio-test` et est supprimé à la fin."""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.environ.get("NOKKAN_DOCKER_E2E") != "1", reason="NOKKAN_DOCKER_E2E=1 pour le lancer")]

FIXTURE = Path(__file__).parents[1] / "unit" / "tools" / "stdio_fixture"
IMAGE = os.environ.get("NOKKAN_DOCKER_E2E_IMAGE", "python:3.12-slim")
IDENTITY = "alice.identite-7f3a@exemple.test"
TOKEN = "jeton-" + "coffre-" + "93b1c0ffee"


def _docker(*args: str, stdin: str | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], input=stdin, capture_output=True, text=True, timeout=timeout)


class _Env:
    """Tient la place de l'environnement amont : un container existant, des commandes par `docker exec`."""

    _docker_exe = "docker"
    cwd = "/tmp"

    def __init__(self, container: str) -> None:
        self._container_id = container

    def execute(self, command: str, timeout: int = 60, stdin_data: str | None = None, **_: object) -> dict:
        p = _docker("exec", "-i", self._container_id, "bash", "-c", command, stdin=stdin_data or "", timeout=timeout)
        return {"returncode": p.returncode, "output": p.stdout + p.stderr}


@pytest.fixture(scope="module")
def container():
    name = f"nokkan-mcp-stdio-{uuid.uuid4().hex[:8]}"
    run = _docker("run", "-d", "--rm", "--name", name, "--label", "nokkan-mcp-stdio-test=1", "--memory", "512m",
                  "--cpus", "1", IMAGE, "sleep", "1200")
    assert run.returncode == 0, run.stderr
    try:
        assert _docker("cp", str(FIXTURE), f"{name}:/fixture").returncode == 0
        pip = _docker("exec", name, "pip", "install", "-q", "--root-user-action=ignore", "mcp==2.0.0", timeout=300)
        assert pip.returncode == 0, pip.stderr[-800:]
        yield name
    finally:
        _docker("rm", "-f", name)


@pytest.fixture
def rig(container, tmp_path):
    from nokker.tools.code import CodeToolSource, Workspace
    from nokker.tools.mcp_stdio import SessionEnv, StdioMcpToolSource

    proof = f"/tmp/proof-{uuid.uuid4().hex[:6]}"
    code = CodeToolSource(root=str(tmp_path), backend="docker")
    code._spaces["th_a"] = Workspace(env=_Env(container), ops=None, todos=None, cwd="/tmp")
    env = SessionEnv()
    env.set("th_a", {"TEST_SESSION_USER": IDENTITY})
    source = StdioMcpToolSource(
        name="srv", command="python3", args=["/fixture/server.py", proof],
        env={"TEST_SESSION_USER": "session", "TEST_API_TOKEN": "secret:API_TOKEN", "TEST_THREAD": "thread"},
        resolve=lambda t: code if t == "th_a" else None, session_env=env, secret=lambda n: TOKEN,
        catalog_path=tmp_path / "catalog.json", timeout_s=30.0, start_timeout_s=60.0)
    yield source, container, proof
    source.close()


def _ctx():
    from nokker.core.ports import ToolContext

    return ToolContext(thread_id="th_a", turn_index=1, channel="api")


def _servers_in(container: str) -> list[str]:
    top = _docker("top", container, "-eo", "pid,args").stdout
    return [line for line in top.splitlines() if "/fixture/server.py" in line]


def _wait(cond, seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


def test_server_runs_in_the_container_with_identity_by_name(rig):
    source, container, proof = rig
    source.turn_done("th_a")
    assert {s.name for s in source.specs()} >= {"echo", "boom", "die", "pid"}
    r = source.call("echo", {"text": "depuis le container"}, _ctx())
    assert not r.is_error and "depuis le container" in r.content
    assert len(_servers_in(container)) == 1  # le processus est DANS le bac à sable
    # Côté noyau : seulement des clients `docker exec`, sans aucune valeur dans leur ligne de commande.
    host = subprocess.run(["ps", "-ww", "-eo", "args"], capture_output=True, text=True).stdout
    clients = [line for line in host.splitlines() if " exec " in line and container in line]
    assert clients and all("-e TEST_SESSION_USER" in c for c in clients)
    assert IDENTITY not in host and TOKEN not in host
    # Côté serveur : l'identité est là.
    env = json.loads(_docker("exec", container, "bash", "-c", f"cat {proof}/env-*.json").stdout)
    assert env["TEST_SESSION_USER"] == IDENTITY and env["TEST_API_TOKEN"] == TOKEN and env["TEST_THREAD"] == "th_a"
    assert "NOKKER_MCP" in env
    # Côté modèle : absente des erreurs et des schémas.
    boom = source.call("boom", {}, _ctx())
    seen = boom.content + json.dumps([[s.name, s.description, s.parameters] for s in source.specs()])
    assert boom.is_error and IDENTITY not in seen and TOKEN not in seen


def test_death_restart_and_clean_stop_in_the_container(rig):
    source, container, _ = rig
    source.turn_done("th_a")
    first = source.call("pid", {}, _ctx()).content
    r = source.call("die", {}, _ctx())
    assert r.is_error and "s'est arrêté" in r.content
    second = source.call("pid", {}, _ctx())
    assert not second.is_error and second.content != first  # relancé
    assert len(_servers_in(container)) == 1
    source.close_thread("th_a")
    assert _wait(lambda: _servers_in(container) == [])  # arrêt propre : plus rien dans le bac à sable


def test_orphan_left_by_a_killed_client_is_reaped_by_its_mark(rig):
    """Le client `docker exec` tué (noyau interrompu) laisse le serveur tourner dans le container : le ménage par la
    marque `NOKKER_MCP` l'arrête, sans toucher aux autres processus du container."""
    source, container, _ = rig
    source.turn_done("th_a")
    assert not source.call("echo", {"text": "x"}, _ctx()).is_error
    _docker("exec", "-d", container, "sleep", "600")  # un voisin sans marque
    for line in subprocess.run(["pgrep", "-af", f"docker exec.*{container}.*server.py"], capture_output=True,
                               text=True).stdout.splitlines():
        os.kill(int(line.split()[0]), 9)
    r = source.call("echo", {"text": "x"}, _ctx())
    assert r.is_error  # tube fermé : mort constatée
    r = source.call("echo", {"text": "revenu"}, _ctx())  # relance : l'orphelin est ramassé avant
    assert not r.is_error and "revenu" in r.content
    assert _wait(lambda: len(_servers_in(container)) == 1)
    source.close()
    assert _wait(lambda: _servers_in(container) == [])
    assert "sleep 600" in _docker("top", container, "-eo", "pid,args").stdout
