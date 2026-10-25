"""Dépôt de travail d'une session : clone d'un dépôt https d'une forge autorisée, avec un identifiant référencé par NOM.

Règles :
  - URL `https://hôte[:port]/chemin` seulement ; hôte dans `code.forge.allowed_hosts` (égalité exacte, port compris) ;
    ni identifiants (`user:token@` : refusés SANS écho de l'URL), ni requête, ni fragment, ni `..` ;
  - l'identifiant (valeur du coffre, `jeton` ou `utilisateur:jeton`) ne passe JAMAIS par l'URL ni par la ligne de
    commande : en-tête `Authorization: Basic …` posé par l'environnement de git (`GIT_CONFIG_COUNT`), redirections
    coupées (l'en-tête ne part pas vers un autre hôte), invite de mot de passe coupée ;
  - git sans hooks (`core.hooksPath=/dev/null`), sans config système ni globale, https seul (`protocol.allow=never`,
    `protocol.https.allow=always` : pas de `file://` ni `ext::` par un sous-module), sans sous-modules ;
  - toute sortie de git qui remonte (erreur) est caviardée : la valeur, son encodage base64, et tout `://x:y@`.

Le clone tourne dans le noyau, pas dans le bac à sable : le dépôt est neuf (aucun hook cloné, et les hooks sont coupés),
et l'identifiant n'entre jamais dans un bac à sable où le modèle lance ses commandes."""
from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from urllib.parse import urlsplit

_USERINFO = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s'\"]+@")
_SEGMENT = re.compile(r"^[A-Za-z0-9._~][A-Za-z0-9._~-]*$")
_HOST = re.compile(r"^[a-z0-9.-]+(:[0-9]{1,5})?$")
MAX_OUTPUT = 2000


class ForgeError(Exception):
    """`code` : `https_required`, `host_not_allowed`, `credentials_in_url`, `bad_url`, `clone_failed`,
    `git_missing`. Le message ne contient jamais de valeur de secret."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def parse_repo(url: str, allowed_hosts: list[str] | tuple[str, ...]) -> str:
    """L'URL propre (hôte en minuscules), ou `ForgeError`."""
    raw = str(url or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        raise ForgeError("bad_url", "URL de dépôt illisible") from None
    if parts.scheme.lower() != "https":
        raise ForgeError("https_required", "dépôt : https seulement")
    if "@" in parts.netloc:
        raise ForgeError("credentials_in_url", "identifiants dans l'URL refusés : range-les au coffre "
                                               "(PUT /v1/code/credentials/{nom}) et donne `credential: <nom>`")
    host = parts.netloc.lower()
    if not _HOST.fullmatch(host):
        raise ForgeError("bad_url", "hôte de dépôt invalide")
    if host not in {h.lower() for h in allowed_hosts}:
        raise ForgeError("host_not_allowed", f"forge non autorisée : {host} (code.forge.allowed_hosts)")
    if parts.query or parts.fragment or raw.endswith(("?", "#")):
        raise ForgeError("bad_url", "URL de dépôt : ni requête ni fragment")
    segs = [s for s in parts.path.split("/") if s]
    if not segs or any(s in (".", "..") or not _SEGMENT.fullmatch(s) for s in segs):
        raise ForgeError("bad_url", "chemin de dépôt invalide")
    return f"https://{host}/" + "/".join(segs)


def redact(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """Caviarde les valeurs données (et leur base64 `utilisateur:jeton`) et tout `schéma://identifiants@`."""
    out = str(text or "")
    for s in secrets:
        if not s:
            continue
        variants = {s, base64.b64encode(s.encode()).decode()}
        if ":" not in s:
            variants.add(base64.b64encode(f"x-access-token:{s}".encode()).decode())
        else:
            variants.add(s.split(":", 1)[1])
        for v in sorted(variants, key=len, reverse=True):
            if len(v) >= 4:
                out = out.replace(v, "[secret]")
    return _USERINFO.sub(r"\1***@", out)


def _header(token: str) -> str:
    user, sep, secret = token.partition(":")
    pair = token if sep and user and secret else f"x-access-token:{token}"
    return "Authorization: Basic " + base64.b64encode(pair.encode()).decode()


def _run(argv: list[str], env: dict[str, str], timeout: int) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise ForgeError("git_missing", "git absent de l'image du noyau") from None
    except subprocess.TimeoutExpired:
        return 124, f"délai dépassé ({timeout} s)"
    return p.returncode, (p.stderr or "") + (p.stdout or "")


def clone(url: str, dest: str, token: str | None, *, ref: str | None = None, depth: int | None = None,
          timeout: int = 300, run: Callable[[list[str], dict[str, str], int], tuple[int, str]] | None = None) -> None:
    """`git clone` de `url` (déjà passé par `parse_repo`) dans `dest`. `ForgeError` caviardée sinon."""
    config = [("core.hooksPath", os.devnull), ("protocol.allow", "never"), ("protocol.https.allow", "always"),
              ("http.followRedirects", "false"), ("credential.helper", ""), ("core.askPass", "")]
    if token:
        config.append(("http.extraHeader", _header(token)))
    with tempfile.TemporaryDirectory(prefix="nokker-git-") as home:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "GIT_TERMINAL_PROMPT": "0",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ASKPASS": "",
               "SSH_ASKPASS": "", "LC_ALL": "C", "GIT_CONFIG_COUNT": str(len(config))}
        for i, (k, v) in enumerate(config):
            env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = k, v
        argv = ["git", "clone", "--no-recurse-submodules", "--quiet"]
        if ref:
            argv += ["--branch", ref, "--single-branch"]
        if depth:
            argv += ["--depth", str(int(depth))]
        argv += ["--", url, dest]
        code, output = (run or _run)(argv, env, timeout)
    if code != 0:
        msg = redact(output, [token] if token else [])[-MAX_OUTPUT:].strip()
        raise ForgeError("clone_failed", f"clone du dépôt en échec (code {code}) : {msg or 'sans message'}")


__all__ = ["ForgeError", "clone", "parse_repo", "redact"]
