"""Chemins de travail connus : le dossier d'une session (nom tiré au hasard) ne passe plus par le repli par entropie, sans
affaiblir la détection d'un vrai secret (chemin inconnu, valeur collée après la racine, motifs précis)."""
from __future__ import annotations

import pytest

from nokker.qa import secrets as S

ROOT = "/workspace/Qk3Xv9LmN2pRtZ7wYb4C-074012"
KEY = "Zx9Qw3Er" + "7Ty1Ui5O" + "p2As6Df8" + "Gh4Jk0Lm"  # 32 caractères mêlés : clé base64 probable


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    monkeypatch.setattr(S, "_KNOWN_PATHS", {})


def test_unknown_path_is_masked_as_before():
    out = S.redact(f"répertoire : {ROOT}/src").text
    assert "[secret:entropy-" in out  # reproduction : le nom du dossier tiré au hasard est pris pour un secret


def test_known_root_is_kept_in_listings_and_errors():
    S.add_known_path(ROOT)
    for text in (f"{ROOT}", f"répertoire : {ROOT}/src/app.py", f"erreur : '{ROOT}' est un dossier", f"{ROOT}/."):
        r = S.redact(text)
        assert r.text == text and not r.names, (text, r.text)


def test_known_root_does_not_hide_a_secret_elsewhere_or_after_it():
    S.add_known_path(ROOT)
    elsewhere = S.redact(f"ouvre {ROOT} puis /tmp/{KEY}/data").text
    assert ROOT in elsewhere and KEY not in elsewhere and "[secret:entropy-" in elsewhere  # chemin inconnu : masqué
    after = S.redact(f"{ROOT}/{KEY}").text
    assert after.startswith(ROOT) and KEY not in after  # valeur collée après la racine : masquée
    glued = S.redact(f"{ROOT}{KEY}").text  # collée SANS séparateur : ce n'est plus la racine
    assert KEY not in glued


def test_precise_patterns_still_run_inside_a_known_root():
    S.add_known_path(ROOT)
    token = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"
    out = S.redact(f"{ROOT}/.env : OPENAI_API_KEY={token}").text
    assert token not in out and ROOT in out


def test_add_known_path_ignores_short_relative_and_empty():
    for bad in ("", None, "workspace/x", "/tmp", "/a/b"):
        S.add_known_path(bad)
    assert S._KNOWN_PATHS == {}


def test_code_tool_source_registers_its_workspace_roots(tmp_path, monkeypatch):
    from nokker.tools.code import CodeToolSource, Workspace

    root = tmp_path / "Qk3Xv9LmN2pRtZ7wYb4C-074012"
    root.mkdir()
    src = CodeToolSource(root=str(root), backend="local")
    monkeypatch.setattr(CodeToolSource, "_open",
                        lambda self, tid: Workspace(env=None, ops=None, todos=None, cwd=str(root.resolve())))
    src.workspace("th_1")
    assert str(root.resolve()) in S._KNOWN_PATHS
