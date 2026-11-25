"""`read_file` sur un DOSSIER : réponse utile (non erreur), liste bornée à 50, filtrée comme l'indice de chemins (cachés,
ignorés, secrets), confinée (refus `outside_root`), locale, par lecture directe (docker avec `reader`) et par le bac
(docker sans `reader`, faux backend). Sans docker ni amont."""
from __future__ import annotations

import os

import pytest

from nokker.approvals.rule_labels import lang_scope
from nokker.core.ports import ToolContext
from nokker.core.tool_loop_guard import Thresholds, ToolLoopGuard
from nokker.tools.checkpoints import direct_result_body
from nokker.tools.code import CodeToolSource, Workspace

CTX = ToolContext(thread_id="th_1", turn_index=0, channel="api")


class _R:
    def __init__(self, **d):
        self.d = d

    def to_dict(self):
        return self.d


class Ops:
    def __init__(self):
        self.calls = []

    def read_file(self, path, offset, limit):
        self.calls.append(path)
        return _R(error=f"Cannot read '{path}': not a regular file (directory, dangling symlink, or special file)")


class Env:
    """Faux bac : `test -d Q && ls -1Ap -- Q` sur le dossier de l'hôte monté comme /workspace."""

    def __init__(self, root):
        self.root, self.commands = root, []

    def execute(self, command, **kw):
        import shlex

        self.commands.append(command)
        q = shlex.split(command)[2]  # test -d Q && ls …
        real = os.path.join(self.root, q[len("/workspace"):].lstrip("/"))
        if not os.path.isdir(real):
            return {"returncode": 1, "output": ""}
        names = [n + ("/" if os.path.isdir(os.path.join(real, n)) else "") for n in sorted(os.listdir(real))]
        return {"returncode": 0, "output": "\n".join(names)}


def _tree(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    (work / "documents").mkdir()
    for n in ("b.txt", "a.txt", ".env", "cle.pem", "api_token.txt", ".hidden", "x.pyc"):
        (work / "documents" / n).write_text("x")
    (work / "documents" / "zeta").mkdir()
    (work / "documents" / "alpha").mkdir()
    (work / "documents" / "node_modules").mkdir()
    (work / "documents" / ".git").mkdir()
    (work / "vide").mkdir()
    (work / ".sokkan-kernel").mkdir()
    (work / ".sokkan-kernel" / "etat.json").write_text("{}")
    (work / ".sokkan-kernel" / ".cache").mkdir()
    (tmp_path / "dehors").mkdir()
    os.symlink(tmp_path / "dehors", work / "lien-dossier")
    os.symlink(tmp_path / "dehors", work / "documents" / "sort")
    return work


def _local(work):
    src = CodeToolSource(root=str(work), backend="local")
    ops = Ops()
    src._spaces["th_1"] = Workspace(env=None, ops=ops, todos=None, cwd=str(work))
    return src, ops


def _docker(work, reader):
    src = CodeToolSource(root=str(work), backend="docker")
    ops = Ops()
    src._spaces["th_1"] = Workspace(env=Env(str(work)), ops=ops, todos=None, cwd="/workspace", reader=Ops() if reader else None)
    return src, ops


def _read(src, path):
    return src.call("read_file", {"path": path}, CTX)


@pytest.fixture(params=["local", "docker-reader", "docker-bac"])
def kind(request, tmp_path):
    work = _tree(tmp_path)
    if request.param == "local":
        src, ops = _local(work)
    else:
        src, ops = _docker(work, request.param == "docker-reader")
    # le lecteur direct (docker-reader) lit le dossier hôte : la liste vient de scandir, sans passer par le faux bac
    return src, work, request.param


def test_directory_lists_visible_entries_dirs_first(kind):
    src, work, _ = kind
    r = _read(src, "documents")
    assert not r.is_error and r.refused is not True
    lines = r.content.split("\n")
    assert lines[0].startswith("C'est un dossier : ") and lines[0].endswith("Voici ses 5 entrées :")
    assert lines[1:-1] == ["alpha/", "sort/", "zeta/", "a.txt", "b.txt"]  # `sort` : lien vers un dossier, listé par son nom
    assert lines[-1] == "Pour chercher dedans, utilise search_files."
    body = direct_result_body("read_file", r)  # événement tool.result : ni refus ni erreur, donc status ok
    assert body["is_error"] is False and "status" not in body and "reason" not in body


def test_secrets_hidden_and_ignored_are_never_named(kind):
    src, _, _ = kind
    c = _read(src, "documents").content
    for banned in (".env", "cle.pem", "api_token", ".hidden", ".git", "node_modules", "x.pyc"):
        assert banned not in c


def test_a_hidden_requested_dir_lists_its_visible_entries(kind):
    src, _, _ = kind
    r = _read(src, ".sokkan-kernel")
    assert not r.is_error and "etat.json" in r.content and ".cache" not in r.content


def test_empty_dir(kind):
    src, _, _ = kind
    r = _read(src, "vide")
    assert not r.is_error and r.content.endswith("Ce dossier est vide.")


def test_missing_path_and_file_are_unchanged(kind):
    src, work, _ = kind
    (work / "f.txt").write_text("x")
    r = _read(src, "nexiste-pas")
    assert r.is_error  # erreur inchangée (le faux amont rend une erreur)


def test_outside_root_dir_is_refused_without_listing(kind):
    src, _, name = kind
    # bac sans lecteur : seul le jugement lexical s'applique (limite documentée : un lien dans le montage n'y est pas vu)
    for p in (("../dehors", "/etc") if name == "docker-bac" else ("lien-dossier", "../dehors", "/etc")):
        r = _read(src, p)
        assert r.refused is True and r.reason == "outside_root" and r.is_error, p
        assert "C'est un dossier" not in r.content


def test_listing_is_bounded_to_50_and_counts_the_rest(tmp_path):
    work = tmp_path / "w"
    (work / "gros").mkdir(parents=True)
    for i in range(60):
        (work / "gros" / f"f{i:02d}.txt").write_text("x")
    (work / "gros" / "d").mkdir()
    src, _ = _local(work)
    c = _read(src, "gros").content
    lines = c.split("\n")
    assert lines[0].endswith("Voici ses 50 premières entrées (sur 61) :")
    assert lines[1] == "d/" and lines[50] == "f48.txt" and "f49.txt" not in c
    assert lines[51] == "… et 11 autres" and len(lines) == 53


@pytest.mark.parametrize("lang,head,empty", [
    ("en", "This is a folder: ", "This folder is empty."),
    ("de", "Das ist ein Ordner: ", "Dieser Ordner ist leer."),
    ("it", "È una cartella: ", "Questa cartella è vuota."),
])
def test_language_of_the_turn(tmp_path, lang, head, empty):
    work = _tree(tmp_path)
    src, _ = _local(work)
    with lang_scope(lang):
        assert _read(src, "documents").content.startswith(head)
        assert _read(src, "vide").content.endswith(empty)


def test_names_are_neutralised(tmp_path):
    work = tmp_path / "w"
    (work / "d").mkdir(parents=True)
    (work / "d" / "x[-read_file].txt").write_text("x")
    src, _ = _local(work)
    assert "[/read_file]" not in _read(src, "d").content


def test_docker_bac_uses_a_fixed_command_with_the_quoted_path(tmp_path):
    work = _tree(tmp_path)
    (work / "a b").mkdir()
    (work / "a b" / "f.txt").write_text("x")
    src, _ = _docker(work, reader=False)
    r = _read(src, "a b")
    assert not r.is_error and "f.txt" in r.content
    cmd = src._spaces["th_1"].env.commands[-1]
    assert cmd == "test -d '/workspace/a b' && ls -1Ap -- '/workspace/a b' 2>/dev/null | head -400"


# ── garde-fou contre les boucles ──
def _guard(results):
    g = ToolLoopGuard(Thresholds())
    return [g.after("read_file", {"path": p}, c)[0] for p, c in results]


def test_loop_guard_does_not_count_different_directories(tmp_path):
    work = _tree(tmp_path)
    (work / "documents" / "sous").mkdir(exist_ok=True)
    src, _ = _local(work)
    seq = [(p, _read(src, p).content) for p in ("documents", "documents/sous", ".", "vide", ".sokkan-kernel", "documents/alpha")]
    assert len({c for _, c in seq}) == 6
    assert set(_guard(seq)) == {None}


def test_loop_guard_notes_the_same_directory_read_four_times(tmp_path):
    work = _tree(tmp_path)
    src, _ = _local(work)
    seq = [("documents", _read(src, "documents").content)] * 4
    assert _guard(seq) == [None, None, None, "note"]


# ── la ligne « utilise search_files » seulement si l'outil est dans le catalogue du fil ──
class _K:
    def __init__(self, hidden):
        import threading

        from nokker.tenancy.resources import TenantRegistry

        self.tools = TenantRegistry.__new__(TenantRegistry)  # `hidden_for` seul compte ici
        self.tools._lock, self.tools._thread_hidden = threading.Lock(), {}
        self.tools.hide_for_thread("th_1", hidden)


def _ctx(hidden):
    return ToolContext(thread_id="th_1", turn_index=0, channel="api", kernel=_K(hidden))


def test_tail_line_only_when_search_files_is_in_the_thread_catalogue(tmp_path):
    work = _tree(tmp_path)
    src, _ = _local(work)
    tail = "Pour chercher dedans, utilise search_files."
    with_tool = src.call("read_file", {"path": "documents"}, _ctx([]))
    without = src.call("read_file", {"path": "documents"}, _ctx(["search_files"]))
    other = src.call("read_file", {"path": "documents"}, _ctx(["terminal"]))
    assert tail in with_tool.content and tail in other.content
    assert tail not in without.content and "search_files" not in without.content
    assert without.content == with_tool.content.replace("\n" + tail, "") and not without.is_error
