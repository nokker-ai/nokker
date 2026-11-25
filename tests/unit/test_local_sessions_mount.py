"""`workspace` de `POST|GET /v1/local/sessions` (docs/LOKKAL.md §7.4.1) : le montage du dossier de travail dans le bac
à sable de la session, `null` quand l'exécution est locale (chemin de l'hôte)."""
from __future__ import annotations

from nokker.local.api import LocalApi
from nokker.tools.code import WORKSPACE_MOUNT, CodeToolSource


def _api_backend(tmp_path, backend):
    root = tmp_path / "projets"
    (root / "app").mkdir(parents=True)
    return LocalApi.from_config({"token_path": str(tmp_path / "tok"),
                                 "code": {"roots": [str(root)], "backend": backend}}, port=8742)


def test_local_backend_session_has_null_workspace(tmp_path):
    api = _api_backend(tmp_path, "local")
    auth = f"Bearer {api.token}"
    status, body = api.handle("POST", "/v1/local/sessions", auth, {"cwd": str(tmp_path / "projets" / "app")})
    assert status == 201 and body["workspace"] is None
    assert body["cwd"] == str((tmp_path / "projets" / "app").resolve())  # champs existants inchangés
    row = api.handle("GET", "/v1/local/sessions", auth, {})[1]["sessions"][0]
    assert "workspace" in row and row["workspace"] is None and row["thread_id"] == body["thread_id"]


def test_sandbox_session_reports_the_backend_mount(tmp_path):
    api = _api_backend(tmp_path, "docker")
    auth = f"Bearer {api.token}"
    status, body = api.handle("POST", "/v1/local/sessions", auth, {"cwd": str(tmp_path / "projets" / "app")})
    assert status == 201 and body["workspace"] == WORKSPACE_MOUNT == "/workspace"
    row = api.handle("GET", "/v1/local/sessions", auth, {})[1]["sessions"][0]
    assert row["workspace"] == "/workspace"


def test_mount_comes_from_the_session_backend_not_a_constant(tmp_path):
    api = _api_backend(tmp_path, "docker")
    auth = f"Bearer {api.token}"
    cwd = str(tmp_path / "projets" / "app")
    tid = api.handle("POST", "/v1/local/sessions", auth, {"cwd": cwd})[1]["thread_id"]
    api.code._by_thread[tid] = CodeToolSource(root=cwd, backend="docker", mount="/testbed")
    assert api.handle("GET", "/v1/local/sessions", auth, {})[1]["sessions"][0]["workspace"] == "/testbed"


def test_session_with_cwd_is_mounted_and_a_thread_without_session_gets_a_plain_text(tmp_path):
    """Recette rc.9.30 : « aucun dossier de travail n'est monté » citait des noms d'outils. Un fil de session a bien sa
    source (dossier monté) ; un fil sans session reçoit un texte sans nom d'outil ni de route."""
    import re

    from nokker.core.ports import ToolContext
    from nokker.tools.checkpoints import NO_WORKSPACE

    api = _api_backend(tmp_path, "docker")
    auth = f"Bearer {api.token}"
    body = api.handle("POST", "/v1/local/sessions", auth, {"cwd": str(tmp_path / "projets" / "app")})[1]
    assert api.code.for_thread(body["thread_id"]) is not None and api.code.sessions()[body["thread_id"]] == body["cwd"]
    assert api.code.for_thread(body["thread_id"] + "/sa-1") is not None  # un sous-agent a le dossier de son parent
    res = api.code.call("terminal", {"command": "ls"}, ToolContext(thread_id="th_autre", turn_index=0, channel="api"))
    assert res.is_error and res.content == NO_WORKSPACE
    assert not re.search(r"terminal|read_file|patch|write_file|search_files|memory_note|POST|/v1/", NO_WORKSPACE)
