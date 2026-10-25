"""Sources de `nokker review` (lot X13) : le diff, les fichiers entiers de contexte, les consignes du dépôt.

Dépôt git local en LECTURE SEULE, sans hooks ni programme du dépôt : chaque appel passe `core.hooksPath=/dev/null`
(comme les dépôts fantômes du noyau, `tools/checkpoints.py`), `core.fsmonitor=false`, `--no-ext-diff` et
`--no-textconv` (un `.gitattributes` de la PR qui nomme un pilote de diff ne lance rien), `GIT_OPTIONAL_LOCKS=0`
(aucune écriture d'index). Les références sont validées puis résolues en sha avant le diff : `--base=--output=x` ne
devient jamais une option de git. Aucun code de la PR n'est exécuté.

Les consignes (AGENTS.md, NOKKER.md, CLAUDE.md, comme X1) sont lues sur la BASE : la PR ne peut pas réécrire les
règles de sa propre relecture. Elles guident la relecture, elles n'accordent rien (invariant 3).
"""
from __future__ import annotations

import os
import posixpath
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Mêmes fichiers que la mémoire de projet (X1, `context/project_memory.INSTRUCTION_FILES`), recopiés : le paquet de la
# relecture reste léger (pas de CortHeXis, pas de noyau) pour tourner dans une image d'action minimale.
INSTRUCTION_FILES = ("AGENTS.md", "NOKKER.md", "CLAUDE.md")
GIT_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "safe.directory=*",
            "-c", "core.pager=cat", "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false")
_REF = re.compile(r"^(?!-)[A-Za-z0-9._/@{}~^+-]{1,200}$")
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
MAX_FILE_BYTES = 200_000
MAX_INSTRUCTION_BYTES = 16_000


class SourceError(RuntimeError):
    """Entrée illisible (référence inconnue, pas un dépôt, fichier hors du dépôt) : code de sortie 2."""


def git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"})
    return env


def run_git(repo: str | Path, *args: str, check: bool = True, timeout: float = 60.0) -> bytes:
    cmd = ["git", "-C", str(repo), *GIT_SAFE, *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=git_env(), stdin=subprocess.DEVNULL,
                              check=False)
    except FileNotFoundError as e:
        raise SourceError("git introuvable (installe git, ou passe --diff)") from e
    except subprocess.TimeoutExpired as e:
        raise SourceError(f"git {args[0]} : délai dépassé") from e
    if check and proc.returncode != 0:
        raise SourceError(f"git {args[0]} : {proc.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return proc.stdout


def resolve(repo: str | Path, ref: str) -> str:
    if not _REF.match(ref or ""):
        raise SourceError(f"référence invalide : {ref!r}")
    sha = run_git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}",
                  check=False).decode().strip()
    if not _SHA.match(sha):
        raise SourceError(f"référence inconnue dans le dépôt : {ref!r}")
    return sha


def git_diff(repo: str | Path, base_sha: str, head_sha: str, context: int = 3) -> str:
    """Diff de la PR : `base...head` (depuis la base commune, comme GitHub et Forgejo l'affichent)."""
    for s in (base_sha, head_sha):
        if not _SHA.match(s):
            raise SourceError(f"sha attendu : {s!r}")
    out = run_git(repo, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M", f"--unified={context}",
                  "--src-prefix=a/", "--dst-prefix=b/", f"{base_sha}...{head_sha}")
    return out.decode("utf-8", "replace")


def git_blob(repo: str | Path, sha: str, path: str, max_bytes: int = MAX_FILE_BYTES) -> str | None:
    """Contenu d'un fichier à un commit (None : absent, binaire ou trop gros). Le chemin est relatif au dépôt."""
    path = posixpath.normpath(path).lstrip("/")
    if path.startswith("..") or not _SHA.match(sha):
        return None
    try:
        size = int(run_git(repo, "cat-file", "-s", f"{sha}:{path}", check=False).decode().strip() or -1)
    except ValueError:
        return None
    if size < 0 or size > max_bytes:
        return None
    data = run_git(repo, "cat-file", "blob", f"{sha}:{path}", check=False)
    return None if b"\0" in data[:8000] else data.decode("utf-8", "replace")


def disk_file(root: str | Path, path: str, max_bytes: int = MAX_FILE_BYTES) -> str | None:
    """Fichier du disque, confiné à `root` (un lien qui en sort est refusé)."""
    base = Path(os.path.realpath(root))
    p = Path(os.path.realpath(base / path))
    if p != base and base not in p.parents:
        return None
    try:
        if not p.is_file() or p.stat().st_size > max_bytes:
            return None
        data = p.read_bytes()
    except OSError:
        return None
    return None if b"\0" in data[:8000] else data.decode("utf-8", "replace")


@dataclass
class Instruction:
    path: str
    text: str


@dataclass
class Sources:
    """Ce que la relecture lit : le diff, les fichiers de contexte, les consignes (et d'où elles viennent)."""
    diff: str
    files: dict[str, str] = field(default_factory=dict)
    instructions: list[Instruction] = field(default_factory=list)
    instructions_from: str = ""  # "base <sha>" | "disque" | ""
    head_sha: str | None = None
    base_sha: str | None = None


def instruction_dirs(paths: list[str]) -> list[str]:
    """Racine puis dossiers des fichiers modifiés (du plus extérieur au plus proche), sans doublon."""
    dirs: list[str] = [""]
    for p in paths:
        parts = posixpath.dirname(p).split("/") if posixpath.dirname(p) else []
        for i in range(1, len(parts) + 1):
            d = "/".join(parts[:i])
            if d not in dirs:
                dirs.append(d)
    return dirs


def read_instructions(reader, paths: list[str], limit: int = 8) -> list[Instruction]:
    out: list[Instruction] = []
    for d in instruction_dirs(paths):
        for name in INSTRUCTION_FILES:
            rel = posixpath.join(d, name) if d else name
            text = reader(rel)
            if text and text.strip():
                out.append(Instruction(rel, text[:MAX_INSTRUCTION_BYTES]))
                if len(out) >= limit:
                    return out
    return out


__all__ = ["GIT_SAFE", "Instruction", "SourceError", "Sources", "disk_file", "git_blob", "git_diff", "git_env",
           "instruction_dirs", "read_instructions", "resolve", "run_git"]
