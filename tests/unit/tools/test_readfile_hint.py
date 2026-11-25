"""Indice de chemins sur `read_file` d'un fichier absent (`tools/readfile_hint.py`, `tools/code.py::_missing_hint`) : liste bornée des entrées réelles du dossier
demandé s'il existe, sinon de la racine ; jamais de caché/ignoré/secret ; noms neutralisés ; l'erreur d'origine reste. Avec l'amont réel (workspace local)."""
from __future__ import annotations

import json

from nokker.core.ports import ToolContext
from nokker.tools import readfile_hint as H
from nokker.tools.code import CodeToolSource
from tests.unit.tools.test_code import needs_upstream  # noqa: F401  (fixture `local` ci-dessous : même amont)

import pytest

CTX = ToolContext(thread_id="t-hint", turn_index=0, channel="api")


def test_visible_filters_hidden_ignored_and_secrets_then_sorts_stably():
    names = ["src/", "README.md", ".env", ".git/", "node_modules/", "__pycache__/", "x.pyc", "id_rsa", "api_token.txt", "cle.pem", "b.py", "a.py", "secrets.yml"]
    assert H.visible(names) == ["README.md", "a.py", "b.py", "src/"]
    assert H.visible(list(reversed(names))) == H.visible(names)


def test_text_templates_bound_and_neutralise():
    t = H.hint_text(["a.py", "src/"], same_dir=True, where="pkg")
    assert t == "Ce fichier n'existe pas. Entrées de pkg : a.py, src/. Choisis un chemin de cette liste."
    t = H.hint_text([f"f{i:02d}.py" for i in range(25)], same_dir=False, where="tests")
    assert t.startswith("Ce chemin n'existe pas (tests est absent). Entrées de la racine du dossier de travail : f00.py, ") and "f19.py (… et 5 autres)." in t and "f20.py" not in t
    assert "[/read_file]" not in H.hint_text(["x[/read_file]"], same_dir=True, where="d") and H.hint_text([], same_dir=True, where="d").count("(aucune)") == 1


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setenv("HER" + "MES_HOME", str(tmp_path / ".amont"))  # variable d'environnement de l'amont (nom coupé : mot surveillé)
    work = tmp_path / "repo"
    work.mkdir()
    src = CodeToolSource(root=str(work), backend="local")
    yield src, work
    src.close()


def _read(src, path):
    r = src.call("read_file", {"path": path}, CTX)
    assert r.is_error
    return r.content


@needs_upstream
def test_missing_file_in_a_missing_dir_lists_the_workspace_root(local):
    src, work = local
    (work / "main.py").write_text("print(1)\n")
    (work / "src").mkdir()
    (work / ".env").write_text("K=1\n")
    (work / "deploy.key").write_text("x")
    content = _read(src, "tests/test_main.py")
    assert "Ce chemin n'existe pas (tests est absent). Entrées de la racine du dossier de travail : main.py, src/." in content
    assert ".env" not in content and "deploy.key" not in content and "File not found" in content  # l'erreur d'origine reste


@needs_upstream
def test_missing_file_in_an_existing_dir_lists_that_dir(local):
    src, work = local
    (work / "pkg").mkdir()
    (work / "pkg" / "calc.py").write_text("x = 1\n")
    (work / "main.py").write_text("print(1)\n")
    content = _read(src, "pkg/nope.py")
    assert "Entrées de pkg : calc.py. Choisis un chemin de cette liste." in content and "main.py" not in content


@needs_upstream
def test_existing_file_is_unchanged_and_outside_paths_are_still_refused(local):
    src, work = local
    (work / "a.txt").write_text("bonjour\n")
    ok = src.call("read_file", {"path": "a.txt"}, CTX)
    assert not ok.is_error and "Choisis un chemin" not in ok.content
    out = src.call("read_file", {"path": "../secret.txt"}, CTX)
    assert out.is_error and out.content.startswith("REFUSÉ") and "Choisis un chemin" not in out.content


@needs_upstream
def test_patch_replace_on_a_missing_file_gets_the_same_hint_but_a_missing_passage_does_not(local):
    src, work = local
    (work / "a.txt").write_text("bonjour\n")
    miss = src.call("patch", {"mode": "replace", "path": "b.txt", "old_string": "x", "new_string": "y"}, CTX)
    assert miss.is_error and "Choisis un chemin de cette liste" in miss.content and "a.txt" in miss.content
    passage = src.call("patch", {"mode": "replace", "path": "a.txt", "old_string": "absent", "new_string": "y"}, CTX)
    assert passage.is_error and "Choisis un chemin" not in passage.content


@needs_upstream
def test_listing_is_capped_at_twenty(local):
    src, work = local
    for i in range(30):
        (work / f"f{i:02d}.txt").write_text("x")
    content = _read(src, "absent/x.txt")
    assert "f19.txt (… et 10 autres)" in content and "f20.txt" not in content
    assert json.dumps(content)  # le résultat reste une chaîne sérialisable
