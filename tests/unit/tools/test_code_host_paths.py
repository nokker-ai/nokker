"""Outils fichiers dans le bac docker : un chemin ABSOLU de l'hôte situé sous le dossier de la session (ou sous la racine
vue par le démon) est traduit vers /workspace ; hors du dossier, il reste tel quel (le bac ne le voit pas, rien de
l'hôte n'en sort). Sans docker ni amont : opérations de fichiers remplacées par des enregistreurs."""
from __future__ import annotations

import os

import pytest

from nokker.core.ports import ToolContext
from nokker.tools.code import CodeToolSource, Workspace

CTX = ToolContext(thread_id="th_1", turn_index=0, channel="api")


class _R:
    def __init__(self, **d):
        self.d = d

    def to_dict(self):
        return self.d


class Ops:
    """Enregistre les chemins reçus (vus du bac : ce qui part vers `docker exec`)."""

    def __init__(self):
        self.calls = []

    def write_file(self, path, content):
        self.calls.append(("write_file", path))
        return _R(path=path, bytes_written=len(content))

    def patch_replace(self, path, old, new, replace_all):
        self.calls.append(("patch_replace", path))
        return _R(success=True, path=path)

    def patch_v4a(self, text):
        self.calls.append(("patch_v4a", text))
        return _R(success=True)

    def read_file(self, path, offset, limit):
        self.calls.append(("read_file", path))
        return _R(path=path, content=f"     1|contenu de {path}", total_lines=1)

    def search(self, pattern, path, **kw):
        self.calls.append(("search", path))
        return _R(total_count=1, files=[os.path.join(path, "a.py")])


class Env:
    def execute(self, command, **kw):
        return {"returncode": 1, "output": ""}


def _src(root, subdir="", host_root=None, reader=True):
    src = CodeToolSource(root=str(root), backend="docker", host_root=host_root, subdir=subdir)
    ops, rd = Ops(), Ops()
    src._spaces["th_1"] = Workspace(env=Env(), ops=ops, todos=None, cwd=src.mount + ("/" + subdir if subdir else ""),
                                    reader=rd if reader else None)
    return src, ops, rd


def _all(ops, rd):
    return ops.calls + rd.calls


def test_write_file_translates_a_host_path_under_the_cwd(tmp_path):
    ws = tmp_path / "srv" / "ws" / "proj"
    ws.mkdir(parents=True)
    src, ops, _ = _src(ws)
    r = src.call("write_file", {"path": f"{ws}/sous/f.txt", "content": "x"}, CTX)
    assert not r.is_error and ops.calls == [("write_file", "/workspace/sous/f.txt")]
    assert str(ws) not in r.content  # le modèle ne voit que /workspace


def test_every_file_tool_translates(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "m.py").write_text("x")
    src, ops, rd = _src(ws)
    src.call("write_file", {"path": f"{ws}/a.txt", "content": "x"}, CTX)
    src.call("patch", {"mode": "replace", "path": f"{ws}/m.py", "old_string": "a", "new_string": "b"}, CTX)
    src.call("patch", {"mode": "patch", "patch": f"*** Begin Patch\n*** Update File: {ws}/m.py\n@@\n-a\n+b\n"
                                                  f"*** Move File: {ws}/m.py -> {ws}/n.py\n*** End Patch"}, CTX)
    r1 = src.call("read_file", {"path": f"{ws}/m.py"}, CTX)
    r2 = src.call("search_files", {"pattern": "x", "path": str(ws)}, CTX)
    r3 = src.call("search_files", {"pattern": "*.py", "target": "files", "path": f"{ws}/"}, CTX)
    assert ops.calls[0] == ("write_file", "/workspace/a.txt")
    assert ops.calls[1] == ("patch_replace", "/workspace/m.py")
    v4a = ops.calls[2][1]
    assert "Update File: /workspace/m.py" in v4a and "Move File: /workspace/m.py -> /workspace/n.py" in v4a
    assert str(ws) not in v4a
    assert rd.calls[0][0] == "read_file" and rd.calls[0][1] == str(ws / "m.py")  # le lecteur lit côté noyau
    assert rd.calls[1] == ("search", str(ws)) and rd.calls[2] == ("search", str(ws) + "/") or rd.calls[2][1].rstrip("/") == str(ws)
    for r in (r1, r2, r3):  # résultats : /workspace, jamais le chemin de l'hôte
        assert str(ws) not in r.content, r.content
    assert "/workspace/m.py" in r1.content or "/workspace" in r1.content


def test_cwd_itself_and_trailing_slash(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    src, _, _ = _src(ws)
    assert src._to_mount_view(str(ws)) == "/workspace"
    assert src._to_mount_view(str(ws) + "/") == "/workspace/"
    assert src._to_mount_view(str(ws) + "/a/./b//c") == "/workspace/a/b/c"


def test_host_path_outside_the_cwd_is_not_translated_nor_leaked(tmp_path):
    ws = tmp_path / "ws"
    (tmp_path / "ws-voisin").mkdir()
    ws.mkdir()
    src, ops, _ = _src(ws)
    for outside in (str(tmp_path / "ws-voisin" / "f"), str(tmp_path / "f"), "/etc/passwd", str(ws) + "x/f",
                    f"{ws}/../ws-voisin/f", f"{ws}/a/../../f"):
        assert src._to_mount_view(outside) == outside or src._to_mount_view(outside).startswith("/workspace") is False
        src.call("write_file", {"path": outside, "content": "x"}, CTX)
    assert not any(p.startswith("/workspace") for _, p in ops.calls), ops.calls
    # le préfixe n'est pas une frontière de dossier : « ws » ne couvre pas « ws-voisin »
    assert src._to_mount_view(str(tmp_path / "ws-voisin" / "f")).startswith(str(tmp_path))


def test_traversal_inside_the_cwd_is_normalised_and_escape_is_left_alone(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    src, _, _ = _src(ws)
    assert src._to_mount_view(f"{ws}/a/../b") == "/workspace/b"
    assert src._to_mount_view(f"{ws}/../x") == f"{ws}/../x"
    assert src._to_mount_view(f"{ws}/a/../../x") == f"{ws}/a/../../x"


def test_symlink_escape_is_refused_by_the_reader(tmp_path):
    ws = tmp_path / "ws"
    out = tmp_path / "dehors"
    ws.mkdir()
    out.mkdir()
    (out / "secret").write_text("s")
    os.symlink(out, ws / "lien")
    src, _, rd = _src(ws)
    r = src.call("read_file", {"path": f"{ws}/lien/secret"}, CTX)
    assert r.is_error and "sort du dossier de travail" in r.content and not rd.calls
    r = src.call("read_file", {"path": "/workspace/lien/secret"}, CTX)  # même refus qu'avant en /workspace
    assert r.is_error and "sort du dossier de travail" in r.content


def test_cwd_with_spaces_and_unicode(tmp_path):
    ws = tmp_path / "mon projet é"
    ws.mkdir()
    src, ops, _ = _src(ws)
    r = src.call("write_file", {"path": f"{ws}/sous dossier/f g.txt", "content": "x"}, CTX)
    assert ops.calls == [("write_file", "/workspace/sous dossier/f g.txt")] and not r.is_error
    r = src.call("read_file", {"path": f"{ws}/f g.txt"}, CTX)
    assert str(ws) not in r.content


def test_already_container_paths_do_not_move(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    src, ops, _ = _src(ws)
    for p in ("/workspace/a.txt", "/workspace", "a.txt", "./a.txt", "sub/../a.txt"):
        before = len(ops.calls)
        src.call("write_file", {"path": p, "content": "x"}, CTX)
        assert ops.calls[before][1] in ("/workspace/a.txt", "/workspace")
    # un cwd hôte dont le chemin commence lui-même par /workspace : pas de double traduction
    ws2 = tmp_path / "w2"
    ws2.mkdir()
    s2, _, _ = _src(ws2)
    assert s2._to_mount_view("/workspace/ws/a") == "/workspace/ws/a"


def test_subdir_session_translates_against_the_whole_root(tmp_path):
    root = tmp_path / "depot"
    (root / ".w" / "ab").mkdir(parents=True)
    src, ops, _ = _src(root, subdir=".w/ab")
    src.call("write_file", {"path": f"{root}/.w/ab/f.txt", "content": "x"}, CTX)
    src.call("write_file", {"path": f"{root}/README", "content": "x"}, CTX)
    src.call("write_file", {"path": "g.txt", "content": "x"}, CTX)
    assert [p for _, p in ops.calls] == ["/workspace/.w/ab/f.txt", "/workspace/README", "/workspace/.w/ab/g.txt"]


def test_daemon_view_root_is_translated_too(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    src, ops, _ = _src(ws, host_root="/hote/runs/abc")
    src.call("write_file", {"path": "/hote/runs/abc/x/f.txt", "content": "x"}, CTX)
    assert ops.calls == [("write_file", "/workspace/x/f.txt")]
    r = src.call("write_file", {"path": "/hote/runs/abcd/f.txt", "content": "x"}, CTX)  # voisin : hors du montage, refusé (n°27)
    assert r.refused and r.reason == "outside_root" and len(ops.calls) == 1


def test_local_backend_is_unchanged(tmp_path):
    src = CodeToolSource(root=str(tmp_path), backend="local")
    assert src._to_mount_view(str(tmp_path / "a")) == str(tmp_path / "a")


def test_workspace_path_for_ssh_copy_translates(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    src, _, _ = _src(ws)
    assert src.workspace_path("th_1", f"{ws}/out/f.bin") == "/workspace/out/f.bin"
    with pytest.raises(PermissionError):
        src.workspace_path("th_1", "/etc/x")


@pytest.mark.skipif(not os.environ.get("NOKKER_TEST_DOCKER_ROOT"), reason="NOKKER_TEST_DOCKER_ROOT absent")
def test_real_docker_host_path_write_read_search(monkeypatch):
    """Sous docker réel, cwd hôte avec espace : écrire, lire, chercher et patcher par le chemin de l'hôte."""
    import shutil
    import uuid

    base = os.environ["NOKKER_TEST_DOCKER_ROOT"]
    ws = os.path.join(base, f"h2paths-{uuid.uuid4().hex[:8]}", "mon projet")
    os.makedirs(ws)
    monkeypatch.setenv("HERMES_HOME", os.path.join(base, ".hermes"))
    src = CodeToolSource(root=ws, backend="docker",
                         image=os.environ.get("NOKKER_TEST_SANDBOX_IMAGE", "ghcr.io/nokker-ai/sandbox:0.1.0-rc.9.31"))
    try:
        w = src.call("write_file", {"path": f"{ws}/sous/m.py", "content": "def f():\n    return 1\n"}, CTX)
        assert not w.is_error, w.content
        assert os.path.isfile(os.path.join(ws, "sous", "m.py"))
        p = src.call("patch", {"mode": "replace", "path": f"{ws}/sous/m.py", "old_string": "return 1",
                               "new_string": "return 2"}, CTX)
        assert not p.is_error, p.content
        r = src.call("read_file", {"path": f"{ws}/sous/m.py"}, CTX)
        assert "return 2" in r.content and ws not in r.content
        s = src.call("search_files", {"pattern": "return 2", "path": ws}, CTX)
        assert "/workspace/sous/m.py" in s.content and ws not in s.content
        out = src.call("write_file", {"path": "/srv/hors/x.txt", "content": "x"}, CTX)
        assert out.is_error or not os.path.exists("/srv/hors/x.txt")
    finally:
        src.close()
        shutil.rmtree(os.path.dirname(ws), ignore_errors=True)


# ── terminal : jamais réécrit ; un indice seulement après un échec qui cite un chemin de l'hôte ──

class _ShellEnv:
    def __init__(self, rc, output):
        self.rc, self.output, self.commands = rc, output, []

    def execute(self, command, **kw):
        self.commands.append(command)
        return {"returncode": self.rc, "output": self.output}


def _term(ws, command, rc, output, backend="docker"):
    from nokker.tools.code import HOST_PATH_HINT

    src = CodeToolSource(root=str(ws), backend=backend)
    env = _ShellEnv(rc, output)
    src._spaces["th_1"] = Workspace(env=env, ops=None, todos=None, cwd=str(ws) if backend == "local" else "/workspace")
    r = src.call("terminal", {"command": command}, CTX)
    return r, env, HOST_PATH_HINT


def test_terminal_never_rewrites_the_command_and_adds_a_hint_after_a_host_path_failure(tmp_path):
    ws = tmp_path / "mon projet"
    ws.mkdir()
    cmd = f"mkdir -p {ws}/out && ls {ws}/out"
    r, env, hint = _term(ws, cmd, 1, "mkdir: cannot create directory: No such file or directory")
    assert env.commands == [cmd]  # commande exécutée telle quelle
    assert r.content.endswith("\n" + hint) and r.exit_code == 1 and r.content.count(hint) == 1
    r, env, hint = _term(ws, f"cat {ws}/x", 1, "cat: Permission denied")
    assert hint in r.content


@pytest.mark.parametrize("cmd,rc,out", [
    ("ls /workspace/x", 1, "No such file or directory"),            # pas de chemin de l'hôte
    ("cat /srv/autre/x", 1, "No such file or directory"),            # chemin hors du dossier
    ("true", 0, "No such file or directory"),                        # succès
    ("false", 1, "autre erreur"),                                    # autre échec
])
def test_terminal_no_hint_otherwise(tmp_path, cmd, rc, out):
    ws = tmp_path / "ws"
    ws.mkdir()
    r, _, hint = _term(ws, cmd, rc, out)
    assert hint not in r.content


def test_terminal_no_hint_for_a_neighbour_prefix_nor_in_local_backend(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r, _, hint = _term(ws, f"cat {ws}-voisin/x", 1, "No such file or directory")
    assert hint not in r.content
    r, _, hint = _term(ws, f"cat {ws}/x", 1, "No such file or directory", backend="local")
    assert hint not in r.content
