"""HERMES.lock est l'unique source de l'épingle hermes-nokker (docs/UPSTREAM.md). Ce test échoue si un fichier qui la
répète en diverge : le TAG partout (CI GitHub, workflows d'images, Dockerfile, commentaire de pyproject, CI de la
forge si elle est présente) ; le COMMIT seulement contre la CI GitHub, qui clone le dépôt public (un miroir peut
porter un autre sha pour le même tag, donc HERMES.lock ne garde que le commit public)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TAG_RE = re.compile(r"nokker-hermes-v[0-9][0-9A-Za-z.\-]*")


def _lock() -> dict[str, str]:
    out = {}
    for line in (ROOT / "HERMES.lock").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_lock_is_well_formed():
    lock = _lock()
    assert set(lock) == {"repo", "tag", "commit", "extras"}
    assert re.fullmatch(r"[0-9a-f]{40}", lock["commit"])
    assert TAG_RE.fullmatch(lock["tag"])
    assert lock["repo"].startswith("https://github.com/")
    assert lock["extras"]


@pytest.mark.parametrize("rel", [
    ".github/workflows/ci.yml",
    ".github/workflows/lokkal-images.yml",
    "Dockerfile",
    "pyproject.toml",
    ".forgejo/workflows/ci.yml",  # retirés de l'export public : ignorés s'ils sont absents
    ".forgejo/workflows/release.yml",
])
def test_every_mention_of_the_tag_matches_the_lock(rel):
    if not (ROOT / rel).exists():
        pytest.skip(f"{rel} absent (export public)")
    tags = set(TAG_RE.findall(_text(rel)))
    assert tags <= {_lock()["tag"]}, f"{rel} diverge de HERMES.lock : {sorted(tags)} (lock : {_lock()['tag']})"


@pytest.mark.parametrize("rel", ["pyproject.toml", "Dockerfile", ".github/workflows/lokkal-images.yml"])
def test_the_tag_is_still_named_where_the_docs_say(rel):
    assert _lock()["tag"] in _text(rel), f"{rel} ne nomme plus le tag de HERMES.lock"


def test_github_ci_pins_the_public_repo_tag_commit_and_extras():
    ci = _text(".github/workflows/ci.yml")
    lock = _lock()
    for var, key in (("HERMES_REPO", "repo"), ("HERMES_TAG", "tag"), ("HERMES_COMMIT", "commit"),
                     ("HERMES_EXTRAS", "extras")):
        m = re.search(rf"^\s*{var}:\s*(\S+)", ci, re.MULTILINE)
        assert m, f"ci.yml ne définit plus {var}"
        assert m.group(1) == lock[key], f"ci.yml {var}={m.group(1)} diverge de HERMES.lock {key}={lock[key]}"


def test_extras_in_the_dockerfile_and_pyproject_match_the_lock():
    extras = _lock()["extras"]
    assert f"[{extras}]" in _text("Dockerfile")
    assert f"[{extras}]" in _text("pyproject.toml")


def test_forge_workflows_read_the_tag_from_the_lock():
    for rel in (".forgejo/workflows/ci.yml", ".forgejo/workflows/release.yml"):
        if (ROOT / rel).exists():
            assert "HERMES.lock" in _text(rel), f"{rel} doit lire le tag dans HERMES.lock"
