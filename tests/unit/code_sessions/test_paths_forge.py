"""Racines des sessions (`<workspace_root>/<tenant>/<user>/<session>`, liens symboliques refusés) et dépôt de travail
(URL propre, hôtes autorisés, identifiant de forge jamais dans l'URL, la ligne de commande, un journal ou une erreur)."""
from __future__ import annotations

import os
import shutil

import pytest

from nokker.code_sessions import forge
from nokker.code_sessions.paths import PathRefused, session_root

T, U, S = "tn_aaaaaaaaaaaa", "us_aaaaaaaaaaaa", "cs_0123456789abcdef"


def test_session_root_layout(tmp_path):
    root = session_root(str(tmp_path), T, U, S)
    assert root == os.path.join(str(tmp_path), T, U, S) and os.path.isdir(root)
    assert session_root(str(tmp_path), T, U, S) == root  # rouvrir : même dossier


@pytest.mark.parametrize("tenant,user,session", [
    ("..", U, S), (T, "../us_x", S), (T, U, "cs_../../x"), ("tn_a/b", U, S), (T, U, "cs_short"), ("", U, S),
    (T, "alice", S),
])
def test_bad_ids_are_refused(tmp_path, tenant, user, session):
    with pytest.raises(PathRefused):
        session_root(str(tmp_path), tenant, user, session)


def test_symlinked_component_is_refused(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (tmp_path / T).mkdir()
    os.symlink(other, tmp_path / T / U)  # le dossier de l'utilisateur pointe ailleurs
    with pytest.raises(PathRefused, match="lien"):
        session_root(str(tmp_path), T, U, S)
    assert not any(other.iterdir())  # rien créé de l'autre côté du lien
    os.unlink(tmp_path / T / U)
    os.symlink(other, tmp_path / T / U)
    os.rmdir(other)
    with pytest.raises(PathRefused):
        session_root(str(tmp_path), T, U, S)


def test_parse_repo():
    allowed = ["git.example.org", "forge.example.net:8443"]
    assert forge.parse_repo("https://git.example.org/team/app.git", allowed) == "https://git.example.org/team/app.git"
    assert forge.parse_repo("https://GIT.example.org/team/app", allowed) == "https://git.example.org/team/app"
    assert forge.parse_repo("https://forge.example.net:8443/a/b.git", allowed) == \
        "https://forge.example.net:8443/a/b.git"
    for bad, code in (("http://git.example.org/a.git", "https_required"), ("ssh://git.example.org/a.git",
                      "https_required"), ("https://evil.example.com/a.git", "host_not_allowed"),
                      ("https://git.example.org.evil.com/a.git", "host_not_allowed"),
                      ("https://git.example.org/a.git?x=1", "bad_url"), ("https://git.example.org", "bad_url"),
                      ("https://git.example.org/../a.git", "bad_url"), ("file:///etc/passwd", "https_required"),
                      ("https://git.example.org/-upload-pack=x", "bad_url")):
        with pytest.raises(forge.ForgeError) as e:
            forge.parse_repo(bad, allowed)
        assert e.value.code == code, bad


def test_credentials_in_url_are_refused_without_echo():
    with pytest.raises(forge.ForgeError) as e:
        forge.parse_repo("https://bob:tok-SECRET-123@git.example.org/a.git", ["git.example.org"])
    assert e.value.code == "credentials_in_url" and "tok-SECRET-123" not in str(e.value) and "bob" not in str(e.value)


def test_redact():
    text = "fatal: unable to access 'https://bob:tok-123@git.example.org/a.git/': x tok-123 y"
    out = forge.redact(text, ["tok-123"])
    assert "tok-123" not in out and "bob" not in out and "https://***@git.example.org" in out


def test_clone_keeps_the_token_out_of_argv_and_url(tmp_path):
    seen = {}

    def run(argv, env, timeout):
        seen["argv"], seen["env"] = argv, env
        return 0, ""

    dest = tmp_path / "s"
    dest.mkdir()
    forge.clone("https://git.example.org/a.git", str(dest), "bot:tok-SECRET", ref="main", depth=1, run=run)
    argv, env = seen["argv"], seen["env"]
    assert "tok-SECRET" not in " ".join(argv) and argv[-2:] == ["https://git.example.org/a.git", str(dest)]
    assert "--branch" in argv and "main" in argv and "--depth" in argv and "--no-recurse-submodules" in argv
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert pairs["core.hooksPath"] == os.devnull and pairs["protocol.allow"] == "never"
    assert pairs["protocol.https.allow"] == "always" and pairs["http.followRedirects"] == "false"
    assert pairs["http.extraHeader"].startswith("Authorization: Basic ")
    import base64

    assert base64.b64decode(pairs["http.extraHeader"].split()[-1]).decode() == "bot:tok-SECRET"
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_clone_error_is_redacted(tmp_path):
    def run(argv, env, timeout):
        return 128, "fatal: could not read from 'https://x-access-token:tok-LEAK@git.example.org/a.git' tok-LEAK"

    with pytest.raises(forge.ForgeError) as e:
        forge.clone("https://git.example.org/a.git", str(tmp_path), "tok-LEAK", run=run)
    assert "tok-LEAK" not in str(e.value) and e.value.code == "clone_failed"


@pytest.mark.skipif(shutil.which("git") is None, reason="git absent")
def test_real_git_failure_never_shows_the_token(tmp_path):
    with pytest.raises(forge.ForgeError) as e:
        forge.clone("https://127.0.0.1:1/team/a.git", str(tmp_path / "d"), "bot:tok-REAL-999", timeout=20)
    assert "tok-REAL-999" not in str(e.value) and "Ym90O" not in str(e.value)  # ni la valeur, ni son base64
