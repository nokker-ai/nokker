"""install.ps1 (Windows) émet le code d'accès du premier réglage SANS jamais toucher le jeton local (lot p2/integ-install-ps1-code).

Le jeton est lu DANS WSL par un petit script POSIX (ici extrait d'install.ps1 : le script est servi seul par irm, pas de
fichier voisin) : PowerShell ne le lit ni ne l'affiche, curl le reçoit sur son entrée standard (`-H @-`), jamais dans ses
arguments (visibles dans ps). Le script est joué sous sh/dash/bash avec un FAUX curl, comme deploy/lokkal/tests/
kiosk-launcher.sh. La partie PowerShell ne peut pas tourner ici (pas de pwsh) : elle est vérifiée par lecture du texte ;
deploy/lokkal/tests/i18n.ps1 la joue chez qui a pwsh.
"""
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PS1 = ROOT / "deploy" / "lokkal" / "install.ps1"
TOK = "tok-local-secret-0123456789abcdefghijklmnop"
CODE = "C0deC0deC0deC0deC0de12"


def _script() -> str:
    text = PS1.read_text(encoding="ascii")
    m = re.search(r"\$WslLocal = @'\n(.*?)\n'@\n", text, re.S)
    assert m, "script WSL introuvable dans install.ps1"
    return m.group(1)


def _shells() -> list[str]:
    return [s for s in ("sh", "dash", "bash") if shutil.which(s)]


FAKE_CURL = """#!/bin/sh
# faux curl : note ses arguments et l'en-tête lu sur stdin ; healthz répond ok (ou échoue si HEALTH_EXIT) ; le reste selon
# CURL_REPLY / CURL_EXIT
printf '%s\\n' "ARGS $*" >> "$LOG"
case "$*" in
  *healthz*) [ -n "${HEALTH_EXIT:-}" ] && exit "$HEALTH_EXIT"; printf ok; exit 0 ;;
esac
[ -t 0 ] || printf 'STDIN %s\\n' "$(cat)" >> "$LOG"
[ -n "${CURL_EXIT:-}" ] && exit "$CURL_EXIT"
printf '%s' "$CURL_REPLY"
"""


def _run(tmp: Path, shell: str, mode: str, *, reply: str = "", curl_exit: str = "", health_exit: str = "", token: bool = True, setup_json: str = "",
         env_text: str = "NOKKER_PORT=7420\nNOKKER_API_PORT=8742\n") -> tuple[str, str]:
    d = tmp / "inst"
    (d / "state").mkdir(parents=True, exist_ok=True)
    (tmp / "bin").mkdir(exist_ok=True)
    if token:
        (d / "state" / "local-token").write_text(TOK + "\n")
    elif (d / "state" / "local-token").exists():
        (d / "state" / "local-token").unlink()
    sj = d / "state" / "setup.json"
    if setup_json:
        sj.write_text(setup_json)
    elif sj.exists():
        sj.unlink()
    (d / ".env").write_text(env_text)
    curl = tmp / "bin" / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
    sleep = tmp / "bin" / "sleep"  # faux sleep : les essais de healthz ne ralentissent pas le test
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(sleep.stat().st_mode | stat.S_IXUSR)
    log = tmp / "log"
    log.write_text("")
    env = {"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "LOG": str(log), "CURL_REPLY": reply, "CURL_EXIT": curl_exit, "HEALTH_EXIT": health_exit}
    r = subprocess.run([shell, "-s", "--", str(d), mode], input=_script(), capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout, log.read_text()


@pytest.mark.parametrize("shell", _shells())
def test_code_mode_prints_only_the_code_and_never_puts_the_token_in_curl_arguments(tmp_path, shell):
    out, log = _run(tmp_path, shell, "code", reply='{"code": "%s", "expires_in": 60, "scope": "owner"}' % CODE)
    assert out.strip() == CODE  # le code SEUL : ni jeton, ni JSON (PowerShell rogne la fin de ligne)
    args = [line for line in log.splitlines() if line.startswith("ARGS ") and "healthz" not in line]
    assert len(args) == 1 and TOK not in args[0]
    assert "-H @-" in args[0] and "-X POST" in args[0] and "http://127.0.0.1:8742/v1/local/session-code" in args[0]
    assert "--data {}" in args[0]
    assert f"STDIN Authorization: Bearer {TOK}" in log  # l'en-tête passe par l'entrée standard
    assert TOK not in out


@pytest.mark.parametrize("shell", _shells())
def test_code_mode_prints_nothing_when_it_cannot(tmp_path, shell):
    assert _run(tmp_path, shell, "code", token=False)[0] == ""  # pas de jeton : aucun appel
    assert "ARGS" not in _run(tmp_path, shell, "code", token=False)[1]
    # le noyau n'est pas prêt (healthz ne répond pas, dix essais) : rien, et AUCUN appel avec le jeton ni à session-code
    out, log = _run(tmp_path, shell, "code", health_exit="7", reply='{"code": "%s"}' % CODE)
    assert out == "" and "session-code" not in log and "STDIN" not in log and TOK not in log
    assert log.count("healthz") == 10
    assert _run(tmp_path, shell, "code", reply="{}")[0] == ""  # pas de code dans la réponse
    assert _run(tmp_path, shell, "code", reply='{"code": "court"}')[0] == ""  # forme refusée (16 à 64 caractères)
    assert _run(tmp_path, shell, "code", curl_exit="22")[0] == ""  # noyau injoignable ou refus : rien, pas d'erreur
    assert _run(tmp_path, shell, "code", reply='{"code": "%s"}' % CODE, env_text="NOKKER_PORT=7420\n")[0] == ""  # port inconnu


@pytest.mark.parametrize("shell", _shells())
def test_setup_mode_reads_the_state_without_the_token_in_arguments(tmp_path, shell):
    out, log = _run(tmp_path, shell, "setup", setup_json='{"done": true}')
    assert out.strip() == "1" and "ARGS" not in log  # fait d'après le fichier : aucun appel au noyau
    out, log = _run(tmp_path, shell, "setup", reply='{"done": false, "linked": true}')
    assert out.strip() == "1"
    args = [line for line in log.splitlines() if line.startswith("ARGS ") and "healthz" not in line]
    assert len(args) == 1 and TOK not in args[0] and "-H @-" in args[0] and "-X GET" in args[0]
    assert _run(tmp_path, shell, "setup", reply='{"done": false, "linked": false, "default": null}')[0] == ""
    assert _run(tmp_path, shell, "setup", token=False)[0] == ""
    out, log = _run(tmp_path, shell, "setup", health_exit="7", reply='{"done": true}')
    assert out == "" and "STDIN" not in log  # noyau pas prêt : pas de sonde avec le jeton


def test_powershell_side_never_reads_nor_shows_the_local_token():
    text = PS1.read_text(encoding="ascii")
    outside = re.sub(r"\$WslLocal = @'\n.*?\n'@\n", "", text, flags=re.S)
    code_lines = [line for line in outside.splitlines() if not line.lstrip().startswith("#")]
    body = "\n".join(code_lines)
    assert "#token=" not in body and "?token=" not in body  # jamais dans une URL
    assert "Bearer" not in body  # PowerShell ne construit aucun en-tête d'autorisation
    assert not re.search(r"Get-Content[^\n]*(token|local-token)", body)  # et ne lit pas le fichier du jeton
    assert "setup#code=" in body and "Invoke-WslLocal" in body
    # la clé locale n'est nommée que dans le script WSL (et le chemin de lokkal.json, que la CLI Windows lit)
    assert body.count("local-token") == 0
    # les en-têtes du script WSL : par stdin seulement
    script = _script()
    assert "-H @-" in script and not re.search(r"-H\s+[\"']?Authorization", script)
    # printf est le BUILTIN du shell (invisible dans ps) : jamais /usr/bin/printf ni `env printf` ; le fichier du jeton est entre guillemets
    commands = "\n".join(line.split(" #")[0] for line in script.splitlines() if not line.lstrip().startswith("#"))  # sans commentaires
    assert "/usr/bin/printf" not in commands and not re.search(r"\benv\s+printf", commands)
    assert '"$(cat "$keyfile")"' in script and '[ -r "$keyfile" ]' in script
    # le lien est affiché, jamais ouvert par le script (décision de dev, comme install.sh sans -Open) ; le code est émis juste avant (60 s)
    assert "Start-Process $link" not in body and "Start-Process $conf.web_url" not in body
    assert body.index("Invoke-WslLocal $Distro $conf.install_dir 'code'") > body.index("Invoke-WslLocal $Distro $conf.install_dir 'setup'")


def test_new_messages_are_in_the_french_catalogue_and_the_file_stays_ascii():
    raw = PS1.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and all(b < 128 for b in raw) and b"\r" not in raw
    text = raw.decode("ascii")
    fr = text[text.index("$FR"): text.index("# <<< FR")]
    for key in ("first setup (choose a model): {0}", "  this link works once, for 60 seconds; later: nokker open /setup"):
        assert f"'{key}' =" in fr, key
    assert "{0}" in re.search(r"'first setup \(choose a model\): \{0\}' = '([^\n]*)'\n", fr).group(1)
    # mêmes textes français qu'install.sh
    sh = (ROOT / "deploy" / "lokkal" / "install.sh").read_text(encoding="utf-8")
    assert "ce lien ne sert qu'une fois, pendant 60 secondes ; plus tard : nokker open /setup" in sh
    assert "ce lien ne sert qu''une fois, pendant 60 secondes ; plus tard : nokker open /setup" in fr
