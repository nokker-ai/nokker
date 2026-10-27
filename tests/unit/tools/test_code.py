"""Outils de code (nokker/tools/code.py, Nokker Lokkal L0).

Partie 1, toujours jouée (CI unitaire, sans l'amont) : schémas, effets, classement prudent des commandes, refus.
Partie 2, jouée seulement si l'amont est importable (image nokker, ou PYTHONPATH vers hermes-nokker avec ses
dépendances) : un vrai dossier de travail en backend local — écrire, lire, patcher (remplacement et V4A),
chercher, lancer les tests, tenir le plan, refuser ce qui sort du dossier."""
from __future__ import annotations

import importlib.util
import json
import os

import pytest

from nokker.core.ports import ToolContext
from nokker.core.turn import Effect
from nokker.tools import code
from nokker.tools.code import CODE_TOOLS, CodeToolSource, classify_command

UPSTREAM = importlib.util.find_spec("tools") is not None and importlib.util.find_spec("tools.file_operations") is not None
needs_upstream = pytest.mark.skipif(not UPSTREAM, reason="amont (tools.file_operations) non importable")
CTX = ToolContext(thread_id="t-code", turn_index=0, channel="api")


def test_specs_and_declared_effects(tmp_path):
    src = CodeToolSource(root=str(tmp_path), backend="local")
    specs = {s.name: s for s in src.specs()}
    assert tuple(specs) == CODE_TOOLS
    assert {n: s.effect for n, s in specs.items()} == {
        "read_file": Effect.READ, "search_files": Effect.READ, "write_file": Effect.WRITE, "patch": Effect.WRITE,
        "terminal": Effect.DESTRUCTIVE, "todo": Effect.READ}
    assert all(s.core and s.source == "builtin:code" for s in specs.values())
    assert specs["terminal"].effect_of is not None and specs["read_file"].effect_of is None


def test_bad_root_or_backend_is_refused(tmp_path):
    with pytest.raises(ValueError, match="introuvable"):
        CodeToolSource(root=str(tmp_path / "absent"))
    with pytest.raises(ValueError, match="backend"):
        CodeToolSource(root=str(tmp_path), backend="ssh")


@pytest.mark.parametrize("command,read_only", [
    ("ls -la", True), ("git status && git diff", True), ("grep -rn foo . | head", True),
    ("find . -name '*.py'", True), ("find . -name '*.pyc' -delete", False), ("cat a > b", False),
    ("echo $(rm -rf x)", False), ("python3 -m pytest", False), ("git branch -D main", False),
    ("git push origin main", False), ("npm install", False), ("ls; rm x", False),
    # une commande composée de lectures reste une lecture (vu en bêta le 25.10 : une carte pour un `ls`)
    ("id -u; pwd; ls -la; git log --oneline -1", True), ("whoami && uname -a", True),
    # `env CMD` lance CMD ; des options font écrire une commande de lecture
    ("env", True), ("env python3 x.py", False), ("sort a", True), ("sort -o out a", False), ("sort -uo out a", False),
    ("sort --output=out a", False), ("tree -L 2", True), ("tree -o f", False), ("uniq -c a", True), ("uniq a b", False),
    ("file -C -m x", False),
])
def test_read_only_detection(command, read_only):
    assert code._read_only(command) is read_only


def test_without_upstream_classification_is_never_read(monkeypatch):
    monkeypatch.setattr(code, "_upstream_detection", lambda: None)
    assert classify_command("ls") is Effect.WRITE  # pas de READ sans la détection de l'amont
    assert classify_command("python3 -m pytest") is Effect.DESTRUCTIVE
    assert code.hardline_reason("rm -rf /") is None


def test_with_a_detector_dangerous_is_destructive(monkeypatch):
    class Det:
        @staticmethod
        def detect_hardline_command(c):
            return (c == "mkfs /dev/sda", "format")

        @staticmethod
        def detect_dangerous_command(c):
            return (c.startswith("rm -rf"), "rm", "rm")

    monkeypatch.setattr(code, "_upstream_detection", lambda: Det)
    assert classify_command("ls") is Effect.READ
    assert classify_command("python3 -m pytest") is Effect.WRITE
    assert classify_command("rm -rf build") is Effect.DESTRUCTIVE
    assert classify_command("mkfs /dev/sda") is Effect.DESTRUCTIVE
    assert code.hardline_reason("mkfs /dev/sda") == "format"


def test_unknown_tool_and_missing_argument(tmp_path, monkeypatch):
    src = CodeToolSource(root=str(tmp_path), backend="local")
    monkeypatch.setattr(src, "_open", lambda tid: code.Workspace(env=None, ops=None, todos=None, cwd=str(tmp_path)))
    assert src.call("rm_everything", {}, CTX).is_error
    r = src.call("read_file", {}, CTX)
    assert r.is_error and "path" in r.content


def test_local_backend_confines_file_tools_to_the_root(tmp_path, monkeypatch):
    (tmp_path / "w").mkdir()
    (tmp_path / "secret.txt").write_text("x")
    (tmp_path / "w" / "lien").symlink_to(tmp_path / "secret.txt")
    src = CodeToolSource(root=str(tmp_path / "w"), backend="local")
    monkeypatch.setattr(src, "_open", lambda tid: code.Workspace(env=None, ops=None, todos=None, cwd=src.root))
    for path in ("../secret.txt", str(tmp_path / "secret.txt"), "lien", "~/.ssh/id_ed25519"):
        r = src.call("read_file", {"path": path}, CTX)
        assert r.is_error and r.content.startswith("REFUSÉ"), path
    r = src.call("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Add File: ../evil.py\n+x\n*** End Patch"}, CTX)
    assert r.is_error and r.content.startswith("REFUSÉ")


# ─────────────────────────────── avec l'amont ───────────────────────────────


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    work = tmp_path / "repo"
    work.mkdir()
    src = CodeToolSource(root=str(work), backend="local")
    yield src, work
    src.close()


def _j(result):
    assert not result.is_error, result.content
    return json.loads(result.content)


@needs_upstream
def test_write_read_patch_search_in_a_real_workspace(local):
    src, work = local
    _j(src.call("write_file", {"path": "pkg/calc.py", "content": "def add(a, b):\n    return a - b\n"}, CTX))
    assert (work / "pkg" / "calc.py").read_text().endswith("return a - b\n")
    assert "return a - b" in _j(src.call("read_file", {"path": "pkg/calc.py"}, CTX))["content"]

    _j(src.call("patch", {"mode": "replace", "path": "pkg/calc.py", "old_string": "a - b", "new_string": "a + b"}, CTX))
    assert "a + b" in (work / "pkg" / "calc.py").read_text()

    v4a = ("*** Begin Patch\n*** Update File: pkg/calc.py\n@@ def add(a, b):\n     return a + b\n"
           "+\n+\n+def mul(a, b):\n+    return a * b\n*** Add File: pkg/__init__.py\n+\n*** End Patch")
    _j(src.call("patch", {"mode": "patch", "patch": v4a}, CTX))
    assert "def mul" in (work / "pkg" / "calc.py").read_text() and (work / "pkg" / "__init__.py").exists()

    found = src.call("search_files", {"pattern": "def mul"}, CTX)
    assert not found.is_error and "calc.py" in found.content
    miss = src.call("patch", {"mode": "replace", "path": "pkg/calc.py", "old_string": "absent", "new_string": "x"}, CTX)
    assert miss.is_error


@needs_upstream
def test_terminal_runs_tests_and_classifies(local):
    src, work = local
    (work / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test(self):\n"
                                     "        self.assertEqual(1 + 1, 2)\n")
    r = _j(src.call("terminal", {"command": "python3 -m unittest -q test_ok"}, CTX))
    assert r["returncode"] == 0
    spec = {s.name: s for s in src.specs()}["terminal"]
    assert spec.effect_for({"command": "ls -la"}) is Effect.READ
    assert spec.effect_for({"command": "python3 -m unittest"}) is Effect.WRITE
    assert spec.effect_for({"command": "rm -rf /"}) is Effect.DESTRUCTIVE
    refused = src.call("terminal", {"command": "rm -rf /"}, CTX)
    assert refused.is_error and "jamais exécuté" in refused.content


@needs_upstream
def test_todo_plan_and_workspaces_per_thread(local):
    src, _ = local
    plan = [{"id": "1", "content": "reproduire", "status": "in_progress"},
            {"id": "2", "content": "corriger", "status": "pending"}]
    assert _j(src.call("todo", {"todos": plan}, CTX))["summary"]["total"] == 2
    other = ToolContext(thread_id="t-autre", turn_index=0, channel="api")
    assert _j(src.call("todo", {}, other))["summary"]["total"] == 0  # un plan par fil
    assert _j(src.call("todo", {}, CTX))["todos"][0]["content"] == "reproduire"


# ─────────────────────────────── bac à sable docker ───────────────────────────────
# Joué seulement avec NOKKER_TEST_DOCKER_ROOT : un dossier vu au MÊME chemin par le démon docker et par ce processus
# (sur l'hôte, ou monté au même chemin dans le container du noyau), et l'amont importable.


@pytest.mark.skipif(not (UPSTREAM and os.environ.get("NOKKER_TEST_DOCKER_ROOT")),
                    reason="NOKKER_TEST_DOCKER_ROOT absent ou amont non importable")
def test_docker_sandbox_end_to_end(monkeypatch):
    import shutil
    import subprocess
    import uuid

    base = os.environ["NOKKER_TEST_DOCKER_ROOT"]
    work = os.path.join(base, f"ws-{uuid.uuid4().hex[:8]}")
    os.makedirs(work)
    monkeypatch.setenv("HERMES_HOME", os.path.join(base, ".hermes"))
    src = CodeToolSource(root=work, backend="docker", image=os.environ.get("NOKKER_TEST_SANDBOX_IMAGE",
                                                                         code.DEFAULT_SANDBOX_IMAGE))
    ctx = ToolContext(thread_id="t-docker", turn_index=0, channel="api")
    try:
        _j(src.call("write_file", {"path": "m.py", "content": "def f():\n    return 1\n"}, ctx))
        assert open(os.path.join(work, "m.py")).read().startswith("def f")  # écrit dans le container, vu sur l'hôte
        _j(src.call("patch", {"mode": "replace", "path": "m.py", "old_string": "return 1", "new_string": "return 2"},
                    ctx))
        r = _j(src.call("terminal", {"command": "python3 -c 'import m; print(m.f())' && pwd && id -u"}, ctx))
        assert r["returncode"] == 0 and r["output"].split()[:2] == ["2", "/workspace"], r
        found = src.call("search_files", {"pattern": "return 2"}, ctx).content
        assert "/workspace/m.py" in found and work not in found  # lu sur place, chemins du container seulement
        read = src.call("read_file", {"path": "/workspace/m.py"}, ctx).content
        assert "return 2" in read and work not in read
        escape = src.call("read_file", {"path": "/workspace/../etc/passwd"}, ctx)
        assert work not in escape.content  # hors de /workspace : laissé au bac à sable, jamais l'hôte
        leak = _j(src.call("terminal", {"command": f"ls /var/run/docker.sock 2>&1; ls {work} 2>&1"}, ctx))
        assert "No such file" in leak["output"] and leak["output"].count("No such file") == 2  # rien de l'hôte
        cid = src.workspace("t-docker").env._container_id
        labels = subprocess.run(["docker", "inspect", "--format", "{{json .Config.Labels}}", cid],
                                capture_output=True, text=True, check=True).stdout
        assert '"nokker.sandbox":"t-docker"' in labels
    finally:
        src.close()
        shutil.rmtree(work, ignore_errors=True)


def test_mount_path_is_configurable_for_bench_images(tmp_path):
    """Banc SWE-bench : le dépôt est monté où l'image de l'environnement l'attend (/testbed) ; le modèle ne voit que
    ce chemin, jamais celui de l'hôte, et les points de retour retrouvent le fichier hôte."""
    from types import SimpleNamespace

    from nokker.tools.checkpoints import host_path

    src = CodeToolSource(root=str(tmp_path), backend="docker", mount="/testbed")
    assert src._to_container(f"modifié : {tmp_path}/pkg/mod.py") == "modifié : /testbed/pkg/mod.py"
    ws = SimpleNamespace(cwd="/testbed")
    (tmp_path / "pkg").mkdir()
    assert src._host_path(ws, "pkg") == str(tmp_path / "pkg")
    assert src._host_path(ws, "/workspace/pkg") is None  # l'ancien montage n'existe pas dans ce bac
    assert host_path(tmp_path, "/testbed/pkg/mod.py", "/testbed") == str(tmp_path / "pkg" / "mod.py")
    assert CodeToolSource(root=str(tmp_path), backend="docker").mount == "/workspace"
