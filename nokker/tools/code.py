"""nokker/tools/code.py — outils de code de Nokker Lokkal (docs/LOKKAL.md, phase L0) : lire, chercher, écrire,
patcher, lancer une commande, tenir un plan de travail, dans UN dossier de travail.

Reprise de la bibliothèque d'agent amont (règle produit du 16.10 : on s'appuie sur ce qui marche, on écrit ce qu'il faut) au niveau de ses
BRIQUES, appelées en direct :
  - `tools.file_operations.ShellFileOperations` : lecture paginée et numérotée, écriture atomique avec contrôle de
    syntaxe (JSON/YAML/TOML), remplacement tolérant (`patch_replace`), lint après écriture, recherche (rg, sinon
    grep/find) — chaque opération est une commande passée à l'environnement, donc la même en local et en docker ;
  - `tools.patch_parser` (format V4A, multi-fichiers) via `ShellFileOperations.patch_v4a` ;
  - `tools.environments.docker.DockerEnvironment` (bac à sable : le dossier monté sur /workspace) et
    `tools.environments.local.LocalEnvironment` (sur l'hôte, sur demande explicite seulement) ;
  - `tools.approval_detection` : classement des commandes (dangereuses, et « hardline » jamais exécutées) ;
  - `tools.todo_tool.TodoStore` : le plan de travail.

Ce qui n'est PAS repris : les fonctions-outils de l'amont (`read_file_tool`, `terminal_tool`…). Elles lisent la
config amont (~/.hermes/config.yaml), tiennent leur propre registre d'environnements par tâche et leur propre
approbation des commandes ; ici ces trois rôles sont à nous (un `Workspace` par fil, nos portes, notre `Effect`).

Effets (`nokker/tools/effects.py`, PLAN-P1 §2.3 étape 5) : lire, chercher et le plan = READ ; écrire et patcher =
WRITE ; `terminal` = classé PAR APPEL (`ToolSpec.effect_of`) : commande dangereuse → DESTRUCTIVE (carte
d'approbation forte), commande de lecture pure → READ, le reste → WRITE ; déclaré DESTRUCTIVE si le classement
échoue. Une commande « hardline » (rm -rf /, mkfs, fork bomb…) est refusée par la source même après approbation.

Commandes en arrière-plan (lot X6, `nokker/tools/processes.py`, drapeau `background`) : `process_start` a l'effet de
SA commande, classée comme `terminal` (mêmes cartes, mêmes règles, « hardline » jamais) ; `process_list` et
`process_logs` = READ ; `process_stop` = READ aussi : il n'arrête que ce que le fil a lui-même lancé (sans carte).

Diagnostics de langage (lot 13, `nokker/tools/lsp.py`, drapeaux `local.lsp.enabled` et `local.lsp.inline`, éteints par
défaut) : avec `lsp`, un `write_file`/`patch` réussi synchronise le serveur de langage du fil (dans son bac à sable) ;
en `inline`, le bloc `[Diagnostics]` s'ajoute à la FIN du résultat, rien d'autre ne bouge. `lsp` à None : aucun
chemin pris, schémas et résultats identiques au caractère près (tests/unit/tools/test_lsp.py).

L'amont n'est importé qu'à la création d'un dossier de travail : sans lui (CI unitaire), le module se charge, les
schémas existent, et `classify_command` retombe sur un classement prudent (jamais READ, DESTRUCTIVE au moindre
doute).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from nokker.core import model_texts as mt
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.tools.lsp import LSP_TOOL, language_of
from nokker.tools.processes import SANDBOX_LOG_DIR, ProcessError, ProcessTable, reap_processes

logger = logging.getLogger(__name__)

WORKSPACE_MOUNT = "/workspace"
# Python 3.14 + Node 22 : de quoi lancer les tests des dépôts Python et TypeScript du banc de code. Remplacée en L2
# par notre image publique signée (docs/LOKKAL.md §3).
DEFAULT_SANDBOX_IMAGE = "nikolaik/python-nodejs:python3.14-nodejs22"
MAX_OUTPUT_CHARS = 30_000  # sortie de commande gardée (tête + queue) ; le réservoir range le reste hors fenêtre

# Commandes de lecture pure : aucune écriture, aucun réseau, aucun code du dépôt exécuté. Une commande composée
# n'est READ que si CHAQUE segment l'est et qu'aucune redirection n'écrit.
_READ_ONLY = {"ls", "cat", "head", "tail", "wc", "pwd", "grep", "rg", "file", "stat", "tree", "du", "which",
              "echo", "diff", "sort", "uniq", "cut", "basename", "dirname", "realpath", "env", "printenv",
              "id", "whoami", "groups", "uname", "nproc",
              # état d'un hôte (ops, ssh_run : test réel du 05.11, « uptime » demandait une carte)
              "uptime", "df", "free", "w", "who", "ps", "lsblk", "vmstat", "hostname"}
# Lecture seulement SANS argument : `env CMD…` lance CMD, `hostname NOM` renomme la machine.
_READ_ONLY_BARE = {"env", "hostname"}
# Sous-commandes de lecture (comme `git`) : docker et systemctl ; journalctl sauf ses options qui écrivent.
_DOCKER_READ = {"ps", "images", "logs", "inspect", "version", "info", "stats"}
_SYSTEMCTL_READ = {"status", "is-active", "is-enabled", "is-failed", "list-units", "list-timers", "list-unit-files",
                   "show", "cat"}
_JOURNALCTL_WRITES = re.compile(r"--(?:vacuum-\w+|rotate|flush|sync|relinquish-var|smart-relinquish-var|setup-keys|"
                                r"update-catalog)\b")
# Options qui font écrire une commande de lecture (fichier de sortie, base magique compilée).
_WRITES_WITH = {"sort": ("-o", "--output"), "tree": ("-o",), "file": ("-C", "--compile"), "uniq": ()}
_GIT_READ = {"status", "diff", "log", "show", "blame", "rev-parse", "ls-files", "branch", "grep"}
_FIND_WRITES = re.compile(r"\s-(delete|exec|execdir|ok|okdir|fprint|fprintf|fls)\b")
_SEGMENTS = re.compile(r"&&|\|\||[;|&\n]")


# ─────────────────────────────── classement des commandes ───────────────────────────────


def _upstream_detection() -> Any | None:
    try:
        from tools import approval_detection  # amont (dépendance épinglée, docs/REPO.md)
    except Exception:  # noqa: BLE001 — absent (CI unitaire) : classement prudent
        return None
    return approval_detection


def hardline_reason(command: str) -> str | None:
    """Motif « hardline » de l'amont (jamais exécutable, même approuvée), ou None."""
    det = _upstream_detection()
    if det is None:
        return None
    hit, why = det.detect_hardline_command(command)
    return str(why) if hit else None


def _has_option(args: list[str], opt: str) -> bool:
    """`--output`, `--output=x` ; `-o`, `-ox`, et une lettre groupée (`-uo`) : prudent, une fausse alerte donne WRITE."""
    if opt.startswith("--"):
        return any(a == opt or a.startswith(opt + "=") for a in args)
    return any(a.startswith("-") and not a.startswith("--") and opt[1] in a[1:] for a in args)


def _read_only(command: str) -> bool:
    if re.search(r"[<>`]|\$\(", command):  # redirection, substitution : on ne lit pas ce qui s'exécute
        return False
    for seg in _SEGMENTS.split(command):
        seg = seg.strip()
        if not seg:
            continue
        try:
            words = shlex.split(seg)
        except ValueError:
            return False
        if not words:
            continue
        head = os.path.basename(words[0])
        if head == "git":
            sub = next((w for w in words[1:] if not w.startswith("-")), "")
            if sub not in _GIT_READ or (sub == "branch" and any(w in ("-d", "-D", "-m", "-M") for w in words)):
                return False
        elif head == "find":
            if _FIND_WRITES.search(seg):
                return False
        elif head in ("docker", "systemctl"):
            allowed = _DOCKER_READ if head == "docker" else _SYSTEMCTL_READ
            sub = next((w for w in words[1:] if not w.startswith("-")), "")
            if sub not in allowed:
                return False
        elif head == "journalctl":
            if _JOURNALCTL_WRITES.search(seg):
                return False
        elif head not in _READ_ONLY:
            return False
        elif head in _READ_ONLY_BARE and len(words) > 1:
            return False
        elif head == "uniq" and len([w for w in words[1:] if not w.startswith("-")]) > 1:  # uniq ENTRÉE SORTIE
            return False
        elif any(_has_option(words[1:], o) for o in _WRITES_WITH.get(head, ())):
            return False
    return True


# Règle du NOYAU : ces commandes sont DESTRUCTIVE quoi que dise l'amont (l'amont peut relever l'effet, jamais l'abaisser).
EXCLUDED_COMMAND = re.compile(
    r"(?:^|[\s;&|(`$])(?:rm|rmdir|shred|unlink|truncate|dd|mkfs(?:\.\w+)?|wipefs|scp|rsync|sftp|ftp|nc|ncat|ssh|"
    r"curl|wget|sendmail|mail|mutt)(?=\s|$)"
    r"|git\s+(?:push|reset\s+--hard|clean|branch\s+-[dD]|tag\s+-d|filter-branch|update-ref\s+-d)"
    r"|(?:npm|pnpm|yarn|cargo|gem|twine|poetry)\s+publish|docker\s+(?:push|rm|rmi|system\s+prune)|"
    r"kubectl\s+(?:delete|apply|replace|scale)|helm\s+(?:install|upgrade|uninstall|delete)|"
    r"gh\s+(?:pr\s+merge|release|repo\s+delete)|terraform\s+(?:apply|destroy)")
# Lecture d'un secret combinée à un tube ou une redirection : la valeur sort de son fichier.
_SECRET_READ = re.compile(r"(?:\.ssh\b|id_rsa|id_ed25519|(?:^|[\s/])\.env\b|/etc/shadow|credentials|\.pem\b)")
_PIPE_OR_REDIRECT = re.compile(r"[|<>]")


def _kernel_destructive(command: str) -> bool:
    return bool(EXCLUDED_COMMAND.search(command) or (_SECRET_READ.search(command) and _PIPE_OR_REDIRECT.search(command)))


def classify_command(command: str) -> Effect:
    """Effet d'une commande `terminal`. Sans la détection de l'amont : WRITE si la commande est de lecture pure
    (on ne parie pas sur READ sans la détection), DESTRUCTIVE sinon."""
    det = _upstream_detection()
    if det is None:
        return Effect.WRITE if _read_only(command) else Effect.DESTRUCTIVE
    if _kernel_destructive(command):  # nos motifs priment sur l'amont
        return Effect.DESTRUCTIVE
    if det.detect_hardline_command(command)[0] or det.detect_dangerous_command(command)[0]:
        return Effect.DESTRUCTIVE
    return Effect.READ if _read_only(command) else Effect.WRITE


# ── classe d'AFFICHAGE d'une commande (`command_kind`) ──
# `classify_command` décide (cartes, verdicts) ; `command_kind` ne fait QU'ÉTIQUETER pour la carte : « run » = lancer
# des tests, un linter, un build, un formatage en simple vérification ; « other » = tout le reste. Il ne remplace jamais
# `effect` et aucune porte ne le lit.
_KIND_SHELL = re.compile(r"[;&|<>`$()\\\n\r]")  # enchaînement, tube, redirection, substitution, expansion
# Options qui écrivent (correction, sortie, rapport, mise à jour de snapshots, config, installation) ou ne finissent pas.
_KIND_WRITE_OPT = re.compile(
    r"^--?(?:fix|write|update|output(?!-format)|outfile|out-?dir|out-?file|junit|html|report|cov-report|"
    r"coverage-?dir|\w*profile|trace|watch|init|install|add-noqa|unsafe|config|target-dir|exec|toolexec|overlay|"
    r"modfile|work|non-interactive|cache-clear)",
    re.I)
_KIND_SHORT_WRITE = re.compile(r"^-[A-Za-z]{1,4}$")  # -u -w -o (et groupes) ; pas pour `go` (-run, -count…)
_KIND_PREFIX = (".venv/bin/", "venv/bin/")
_KIND_NAMES = frozenset({"pytest", "py.test", "python", "python3", "npm", "pnpm", "yarn", "vitest", "jest", "tsc",
                         "ruff", "mypy", "eslint", "go", "cargo", "make", "mvn", "gradle", "tox", "nox", "black",
                         "prettier"})


def _kind_head(word: str) -> str | None:
    if word == "./gradlew":
        return "gradle"
    if word == "./mvnw":
        return "mvn"
    for pre in _KIND_PREFIX:
        if word.startswith(pre):
            word = word[len(pre):]
            break
    return word if word in _KIND_NAMES else None


def _kind_args_ok(head: str, args: list[str]) -> bool:
    for i, a in enumerate(args):
        if _KIND_WRITE_OPT.match(a) or a == "watch":
            return False
        if a == "-o" or (head != "go" and _KIND_SHORT_WRITE.match(a) and re.search(r"[uwo]", a[1:])):
            return False
        if a.startswith("-p") and head in ("pytest", "py.test", "python", "python3"):  # greffon : seulement `no:`
            val = a[2:] or (args[i + 1] if i + 1 < len(args) else "")
            if not val.startswith("no:"):
                return False
    return True


def _kind_is_run(words: list[str]) -> bool:
    head = _kind_head(words[0])
    if head is None:
        return False
    args = words[1:]
    if not _kind_args_ok(head, args):
        return False
    sub = next((a for a in args if not a.startswith("-")), "")
    if head in ("pytest", "py.test", "mypy", "tox", "nox", "vitest", "jest", "eslint"):
        return True
    if head in ("python", "python3"):
        return len(args) >= 2 and args[0] == "-m" and args[1] in ("pytest", "unittest")
    if head in ("npm", "pnpm", "yarn"):
        return args[:1] == ["test"] or (args[:1] == ["run"] and args[1:2] in (["test"], ["lint"], ["build"]))
    if head == "tsc":
        return "--noEmit" in args and not any(a in ("-b", "--build") for a in args)
    if head == "ruff":
        if sub == "check":
            return True
        return sub == "format" and any(a in ("--check", "--diff") for a in args)
    if head in ("black", "prettier"):
        return any(a in ("--check", "--diff", "-c") for a in args)
    if head == "go":
        return args[:1] in (["test"], ["vet"], ["build"])
    if head == "cargo":
        return args[:1] in (["test"], ["check"], ["build"], ["clippy"])
    if head == "make":
        tg = ("test", "check", "lint", "build")
        return all(a in tg or re.fullmatch(r"-j\d*", a) for a in args) and any(a in tg for a in args)
    if head in ("mvn", "gradle"):
        targets = ("test",) if head == "mvn" else ("test", "build")
        return sub in targets and all(a in targets or re.fullmatch(r"-[qB]|--(?:quiet|batch-mode|offline)", a)
                                      for a in args)
    return False


def command_kind(command: str) -> str:
    """Classe d'AFFICHAGE d'une commande `terminal` : « run » (tests, linter, build, formatage en vérification,
    reconnus de façon FERMÉE) ou « other ». PUREMENT D'AFFICHAGE : la décision d'approbation reste celle de
    `classify_command` (qui étiquette `pytest` WRITE) ; aucune porte ne lit ce champ.

    « run » exige : aucun opérateur de shell (`; & | < > ` $ ( )`, retour ligne), une liste d'outils/sous-commandes
    connue (pytest, python -m pytest|unittest, npm|pnpm|yarn test|run test|run lint|run build, vitest, jest,
    tsc --noEmit, ruff check, ruff format --check, black|prettier --check, mypy, eslint, go test|vet|build, cargo
    test|check|build|clippy, make test|check|lint|build, mvn test, gradle test|build, tox, nox), aucune option qui
    écrit (--fix, --write, -u, -o, --output, rapports, --watch…), et une commande que `classify_command` ne juge pas
    DESTRUCTIVE (le kind ne masque jamais une destruction ; sans l'amont, `classify_command` rend DESTRUCTIVE :
    « other »)."""
    if not isinstance(command, str) or not command.strip() or _KIND_SHELL.search(command):
        return "other"
    try:
        words = shlex.split(command)
    except ValueError:
        return "other"
    if not words or not _kind_is_run(words):
        return "other"
    return "other" if classify_command(command) is Effect.DESTRUCTIVE else "run"


_CD_PREFIX = re.compile(r"^\s*cd\s+[\w./~-]+\s*&&\s*")
# Forme fermée d'une commande de tests dont la sortie est coupée : `<tests> [2>&1] | tail -N` ou `| head -N` (N entier de 1
# à 1000, `-N` ou `-n N`). Rien d'autre après le tube : ni autre filtre, ni redirection, ni `;`, ni `&&`.
_PIPED_TESTS = re.compile(r"(?P<left>[^|\n\r]+?)\s*(?:2>&1\s*)?\|\s*(?:tail|head)\s+(?:-(?:n\s+)?)(?P<n>[1-9][0-9]{0,2}|1000)\s*")
_REDIR_ERR = re.compile(r"\s*2>&1\s*$")


def _piped_tests_split(command: str) -> tuple[str, str] | None:
    """`(préfixe « cd x && » ou vide, commande de tests nue)` si `command` est de la forme fermée `<tests> [2>&1] | tail|head
    -N`, sinon None. La commande de tests nue (sans `2>&1` ni tube) doit elle-même être reconnue par `is_test_command`."""
    if not isinstance(command, str) or "\n" in command or "\r" in command:
        return None
    m = _PIPED_TESTS.fullmatch(command.strip())
    if m is None or "||" in command:
        return None
    left = m.group("left")
    prefix = _CD_PREFIX.match(left)
    base = _REDIR_ERR.sub("", left)  # un `2>&1` placé juste avant le tube
    if not is_test_command(base):
        return None
    return (prefix.group(0) if prefix else ""), base[len(prefix.group(0)) if prefix else 0:].strip()


def is_test_command(command: str) -> bool:
    """Commande de TESTS reconnue (pytest, python -m pytest|unittest, tox, nox, vitest, jest, npm|pnpm|yarn [run] test, go
    test, cargo test, make test, mvn test, gradle test), de façon fermée : `command_kind` « run » (aucun opérateur de shell,
    aucune option qui écrit), un seul préfixe `cd <dossier> &&` toléré. Le linter, le build et le typage n'en sont pas.
    Forme à sortie coupée aussi reconnue : `<tests> 2>&1 | tail -N` ou `| head -N` (et sans `2>&1`), N de 1 à 1000, rien
    d'autre après le tube ; son code de retour n'est celui des tests que si la commande est exécutée par
    `pipefail_command` (le terminal du bac le fait), sinon c'est celui de tail."""
    command = command or ""
    if _PIPED_TESTS.fullmatch(command.strip()) is not None:
        return _piped_tests_split(command) is not None
    command = _CD_PREFIX.sub("", command)
    if command_kind(command) != "run":
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    head, args = _kind_head(words[0]), words[1:]
    if head in ("pytest", "py.test", "tox", "nox", "vitest", "jest"):
        return True
    if head in ("python", "python3"):
        return len(args) >= 2 and args[0] == "-m" and args[1] in ("pytest", "unittest")
    if head in ("npm", "pnpm", "yarn"):
        return args[:1] == ["test"] or args[:2] == ["run", "test"]
    if head in ("go", "cargo"):
        return args[:1] == ["test"]
    if head in ("make", "mvn", "gradle"):
        return "test" in args
    return False


def pipefail_command(command: str) -> str | None:
    """Pour une commande de tests à sortie coupée reconnue (`<tests> [2>&1] | tail|head -N`), la commande À EXÉCUTER :
    le tube dans un sous-shell bash où `pipefail` est posé (`[cd x && ](set -o pipefail; <commande telle qu'écrite>)`).
    Le code de retour devient celui des tests et non celui de `tail` (toujours 0 : un échec de tests passait pour un
    succès). Rien d'autre ne change : même sortie, même commande vue par la personne et par les portes, aucun état de shell
    laissé (sous-shell : `pipefail` ne sort pas). None pour toute autre commande : exécutée telle quelle."""
    split = _piped_tests_split(command)
    if split is None:
        return None
    prefix, _ = split
    body = command.strip()[len(prefix):] if prefix and command.strip().startswith(prefix) else command.strip()
    return f"{prefix}(set -o pipefail; {body})"


HOST_PATH_HINT = mt.HOST_PATH_HINT["fr"]  # figés ; langue du tour : `model_texts`

SANDBOX_UNAVAILABLE = mt.SANDBOX_UNAVAILABLE["fr"]


class OutsideRoot(PermissionError):
    """Un chemin qui sort du dossier de travail (confinement des outils fichiers). Même texte et même `is_error` pour le
    modèle que tout `PermissionError` ; en plus, le résultat est un REFUS pour l'hôte (`ToolResult.refused`,
    `reason="outside_root"` : `status: "refused"` dans `tool.result` et `tools/call`)."""

    outside_root = True


OUTSIDE_ROOT = "outside_root"


# Fils dont l'OUVERTURE de session de code a été refusée parce que le bac à sable est indisponible (docker absent,
# plafonds non appliqués) : le refus `permissions:no_code_session` le dit au modèle au lieu de demander d'ouvrir une
# session. Vidé à l'ouverture réussie et à la fin de la session. Borné, en mémoire (un redémarrage l'oublie).
_REFUSED_SESSIONS: dict[str, None] = {}
_REFUSED_MAX = 1000


def mark_session_refused(thread_id: str | None) -> None:
    if not thread_id:
        return
    _REFUSED_SESSIONS.pop(thread_id, None)
    _REFUSED_SESSIONS[thread_id] = None
    while len(_REFUSED_SESSIONS) > _REFUSED_MAX:
        _REFUSED_SESSIONS.pop(next(iter(_REFUSED_SESSIONS)), None)


def clear_session_refused(thread_id: str | None) -> None:
    _REFUSED_SESSIONS.pop(thread_id or "", None)


def session_refused(thread_id: str | None) -> bool:
    return (thread_id or "") in _REFUSED_SESSIONS


class SandboxUnavailable(RuntimeError):
    """Backend docker demandé, démon (ou proxy du socket) injoignable : refus net, jamais de repli sur `local`."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(mt.pick(mt.SANDBOX_UNAVAILABLE))
        self.detail = detail  # cause technique, pour les journaux (jamais renvoyée au modèle)


class SandboxLimitsNotApplied(SandboxUnavailable):
    """Plafond mémoire/CPU demandé par l'hôte (`local.code.memory`, `local.code.cpus`) que docker n'a pas posé (cgroups non
    délégués à la machine : l'amont omet alors les plafonds avec un simple avertissement). Le bac est supprimé, jamais de
    bac sans plafond quand l'hôte en a demandé un. Le texte est celui de l'opérateur."""

    def __init__(self, detail: str = "") -> None:
        RuntimeError.__init__(self, mt.pick(mt.SANDBOX_LIMITS_NOT_APPLIED).format(detail=detail))
        self.detail = detail


class SandboxCreateRefused(SandboxUnavailable):
    """Le démon docker a refusé de CRÉER le bac (plage de CPU ou de mémoire invalide pour la machine, image introuvable, tout
    autre refus du démon) : `docker run` a échoué. Session refusée net (503 `sandbox_unavailable`), sans trace au journal. Le texte
    est celui de l'opérateur : il cite ce qui a été demandé et, quand docker la donne, la limite de la machine."""

    def __init__(self, detail: str = "", asked: str = "") -> None:
        RuntimeError.__init__(self, mt.pick(mt.SANDBOX_CREATE_REFUSED).format(detail=detail or "?", asked=asked))
        self.detail = detail




def _docker_refusal(e: BaseException) -> str:
    """La phrase utile du refus de docker (stderr de `docker run`), sans préfixe ni conseil d'aide : « Range of CPUs is from
    0.01 to 2.00, as there are only 2 CPUs available. » Jamais la ligne de commande (arguments, variables)."""
    raw = getattr(e, "stderr", None) or getattr(e, "output", None) or ""
    text = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)).strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("See 'docker")]
    if not lines:
        if isinstance(e, subprocess.TimeoutExpired):
            return "délai dépassé (téléchargement de l'image ?)"
        return f"{type(e).__name__}"
    line = lines[-1]
    for prefix in ("docker: Error response from daemon: ", "Error response from daemon: ", "docker: "):
        line = line.removeprefix(prefix)
    return line[:300]


SANDBOX_LIMITS_NOT_APPLIED = mt.SANDBOX_LIMITS_NOT_APPLIED["fr"]
# Texte vu du MODÈLE (refus d'un appel d'outil après expiration) : sans nom de réglage ni jargon, sans consigne de config.
SANDBOX_LIMITS_FOR_MODEL = mt.SANDBOX_LIMITS_FOR_MODEL["fr"]


def check_sandbox(docker: str = "docker", timeout: float = 5.0) -> None:
    """Le démon docker (DOCKER_HOST : le proxy du socket dans le compose Lokkal) répond-il ? `SandboxUnavailable`
    sinon (exécutable absent, délai dépassé, code de retour non nul). Ne démarre rien et n'exécute rien du modèle."""
    import subprocess

    try:
        r = subprocess.run([docker, "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise SandboxUnavailable(f"{type(e).__name__}: {e}") from e
    if r.returncode != 0:
        raise SandboxUnavailable((r.stderr or "").strip()[:300] or f"docker version : code {r.returncode}")


MIN_INSTANCE_LEN = 6  # une étiquette plus courte est refusée : trop proche d'un mot courant, trop facile à partager


def instance_sandboxes(instance: str, docker: str = "docker") -> list[str]:
    """Identifiants des bacs à sable dont l'étiquette `nokker.instance` est EXACTEMENT `instance`. Le filtre de docker
    sert à réduire la liste ; chaque ligne est re-contrôlée ici sur la valeur exacte de l'étiquette (jamais un préfixe
    ni une sous-chaîne). Id vide ou trop court : rien (liste vide)."""
    import subprocess

    if not isinstance(instance, str) or len(instance) < MIN_INSTANCE_LEN or instance != instance.strip():
        return []
    r = subprocess.run([docker, "ps", "-a", "--filter", f"label=nokker.instance={instance}", "--format",
                        '{{.ID}}\t{{.Label "nokker.instance"}}'], capture_output=True, text=True, timeout=30)
    ids = []
    for line in (r.stdout or "").splitlines():
        cid, sep, label = line.partition("\t")
        if sep and cid.strip() and label == instance:
            ids.append(cid.strip())
    return ids


def reap_sandboxes(instance: str, docker: str = "docker", processes_dir: str | None = None) -> int:
    """Supprime les bacs à sable de CETTE instance de noyau (`nokker.instance=<instance>`) restés d'un arrêt sans
    `close()` (container tué, machine éteinte). À appeler au démarrage du noyau local. Seulement ceux dont l'étiquette
    est exactement la sienne : un autre noyau ou un banc peut tourner sur la même machine (vu le 23.10 : un
    redémarrage a tué les bacs à sable d'un banc en cours). Un id vide ou de moins de `MIN_INSTANCE_LEN` caractères ne
    supprime rien. Rend le nombre supprimé ; 0 si docker est absent. Leurs processus en arrière-plan partent avec eux.

    `processes_dir` (backend local, `<état>/processes`) : arrête aussi les processus en arrière-plan laissés par un
    noyau tué (`reap_processes` : seulement ceux qui portent encore leur marque)."""
    import subprocess

    reaped = reap_processes(processes_dir) if processes_dir else 0
    try:
        ids = instance_sandboxes(instance, docker)
        if ids:
            subprocess.run([docker, "rm", "-f", *ids], capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return reaped
    return reaped + len(ids)


# ─────────────────────────────── schémas ───────────────────────────────

_SPECS: tuple[tuple[str, str, Effect, dict[str, Any]], ...] = (
    ("read_file", "Lit un fichier du dossier de travail (lignes numérotées, par pages).", Effect.READ, {
        "type": "object", "required": ["path"], "properties": {
            "path": {"type": "string", "description": "chemin relatif au dossier de travail"},
            "offset": {"type": "integer", "description": "première ligne (1 = début)", "default": 1},
            "limit": {"type": "integer", "description": "nombre de lignes", "default": 500}}}),
    ("search_files", "Cherche un motif dans le contenu des fichiers, ou des fichiers par nom.", Effect.READ, {
        "type": "object", "required": ["pattern"], "properties": {
            "pattern": {"type": "string", "description": "expression régulière (contenu) ou motif glob (noms)"},
            "target": {"type": "string", "enum": ["content", "files"], "default": "content"},
            "path": {"type": "string", "default": "."},
            "file_glob": {"type": "string", "description": "filtre de fichiers, ex. *.py"},
            "context": {"type": "integer", "description": "lignes de contexte autour d'un résultat", "default": 0},
            "limit": {"type": "integer", "default": 50}}}),
    ("write_file", "Écrit un fichier entier (le crée, ou le remplace).", Effect.WRITE, {
        "type": "object", "required": ["path", "content"], "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}}}),
    ("patch", "Modifie des fichiers : remplacement d'un passage, ou patch V4A sur plusieurs fichiers.", Effect.WRITE, {
        "type": "object", "required": ["mode"], "properties": {
            "mode": {"type": "string", "enum": ["replace", "patch"]},
            "path": {"type": "string", "description": "mode replace : le fichier"},
            "old_string": {"type": "string", "description": "mode replace : passage exact, unique dans le fichier"},
            "new_string": {"type": "string", "description": "mode replace : son remplaçant"},
            "replace_all": {"type": "boolean", "default": False},
            "patch": {"type": "string", "description": "mode patch : *** Begin Patch … *** End Patch (V4A)"}}}),
    ("terminal", "Lance une commande shell dans le dossier de travail (tests, build, git…).", Effect.DESTRUCTIVE, {
        "type": "object", "required": ["command"], "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": "secondes (défaut 120, max 600)", "default": 120}}}),
    ("todo", "Plan de travail : lire (sans argument) ou écrire la liste des étapes.", Effect.READ, {
        "type": "object", "properties": {
            "todos": {"type": "array", "items": {"type": "object", "required": ["id", "content", "status"],
                      "properties": {"id": {"type": "string"}, "content": {"type": "string"},
                                     "status": {"type": "string",
                                                "enum": ["pending", "in_progress", "completed", "cancelled"]}}}},
            "merge": {"type": "boolean", "default": False}}}),
)
MAX_DIR_ENTRIES = 50  # `read_file` d'un dossier : entrées citées au plus
CODE_TOOLS = tuple(name for name, *_ in _SPECS)

# Commandes en arrière-plan (lot X6) : présentes seulement avec `background=True` (drapeau `local.background_processes`).
_PROCESS_SPECS: tuple[tuple[str, str, Effect, dict[str, Any]], ...] = (
    ("process_start", "Lance une commande qui ne rend pas la main (serveur de dev, watch) en arrière-plan, dans le "
                      "dossier de travail ; rend son id. Pour une commande qui se termine (tests, build), terminal.",
     Effect.DESTRUCTIVE, {
        "type": "object", "required": ["command"], "properties": {
            "command": {"type": "string"},
            "name": {"type": "string", "description": "nom court (ex. « vite »)"},
            "port": {"type": "integer", "description": "port HTTP que la commande ouvre : l'utilisateur en reçoit un "
                                                       "aperçu privé"}}}),
    ("process_list", "Liste les processus en arrière-plan de ce fil (état, code de sortie).", Effect.READ, {
        "type": "object", "properties": {}}),
    ("process_logs", "Lit la fin du journal (stdout et stderr) d'un processus en arrière-plan.", Effect.READ, {
        "type": "object", "required": ["id"], "properties": {
            "id": {"type": "string", "description": "bg_…"},
            "tail": {"type": "integer", "description": "dernières lignes (défaut 200, max 2000)", "default": 200}}}),
    ("process_stop", "Arrête un processus en arrière-plan de ce fil (SIGTERM, puis SIGKILL après 5 s).", Effect.READ, {
        "type": "object", "required": ["id"], "properties": {"id": {"type": "string", "description": "bg_…"}}}),
)
PROCESS_TOOLS = tuple(name for name, *_ in _PROCESS_SPECS)
_CLASSIFIED = ("terminal", "process_start")  # effet = celui de la commande (`classify_command`)


# ─────────────────────────────── dossier de travail ───────────────────────────────



def _image_of(r: dict[str, Any], path: str) -> list[Any]:
    """Octets d'une image lue (base64 de l'amont), contrôlés comme une pièce jointe ; [] si ce n'en est pas une ou si
    le contrôle refuse (le modèle garde la ligne de description)."""
    b64 = r.get("base64_content")
    if not r.get("is_image") or not isinstance(b64, str) or not b64:
        return []
    from nokker.images.validate import ImageError, parse_attachments

    try:
        return parse_attachments([{"data": b64, "name": os.path.basename(path)}])
    except (ImageError, ValueError):
        return []

@dataclass
class Workspace:
    """Un environnement d'exécution amont ouvert sur le dossier, ses opérations de fichiers et son plan."""

    env: Any
    ops: Any
    todos: Any
    cwd: str  # le dossier tel que les outils le voient (/workspace en docker, le chemin hôte en local)
    reader: Any = None  # docker : lectures et recherches faites directement dans le dossier monté (voir local_reads)
    procs: Any = None  # nokker.tools.processes.ProcessTable, créée au premier process_start
    lsp: Any = None  # nokker.tools.lsp.ThreadLsp, créé à la première édition suivie (lot 13)

    def close(self) -> None:
        if self.lsp is not None:  # serveurs de langage d'abord : ils tournent dans le bac à sable fermé ci-dessous
            try:
                self.lsp.close()
            except Exception:  # noqa: BLE001 — fermeture au mieux
                pass
        if self.procs is not None:  # fin du fil : rien ne lui survit (docker : le container part aussi, ci-dessous)
            self.procs.stop_all()
        try:
            self.env.cleanup()
        except Exception:  # noqa: BLE001 — fermeture au mieux
            pass


class SharedEnv:
    """L'environnement d'exécution d'un fil parent, prêté à un sous-agent (`CodeToolSource.share_workspace`) : chaque
    commande part dans le dossier courant de l'ENFANT (le sien, suivi d'une commande à l'autre), le dossier courant du
    parent n'est jamais déplacé par elle, et `cleanup` ne ferme rien (le bac à sable appartient au parent)."""

    def __init__(self, inner: Any, cwd: str) -> None:
        self._inner, self.cwd = inner, cwd

    def execute(self, command: str, cwd: str = "", **kwargs: Any) -> dict[str, Any]:
        before = getattr(self._inner, "cwd", None)
        r = self._inner.execute(command, cwd=cwd or self.cwd, **kwargs)
        seen = r.get("cwd") if isinstance(r, dict) and r.get("cwd_observed") else None
        if seen:
            self.cwd = seen
            if before is not None and getattr(self._inner, "cwd", None) == seen != before:
                self._inner.cwd = before  # l'amont range le dossier observé chez lui : celui du parent reste
        return r

    def cleanup(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        if name in ("_inner", "cwd"):
            raise AttributeError(name)
        return getattr(self._inner, name)


def _logs_result(logs: dict[str, Any]) -> str:
    """Journal d'un processus : métadonnées hors clôture, sortie (stdout et stderr) dans la clôture commune."""
    from nokker.qa.untrusted import fenced_result

    meta = {k: v for k, v in logs.items() if k != "output"}
    return fenced_result(meta, "process:output", str(logs.get("output") or ""))


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: limit * 2 // 3], text[-(limit // 3):]
    return f"{head}" + mt.pick(mt.TEXT_CUT).format(n=len(text) - len(head) - len(tail)) + f"{tail}"


_MEMORY_LIMIT = re.compile(r"([1-9][0-9]{0,6})([mMgG])")
_CPUS_LIMIT = re.compile(r"[0-9]{1,3}(\.[0-9]{1,2})?")


def parse_memory_limit(value: Any) -> int:
    """Plafond mémoire du bac (`local.code.memory`, « 2g » / « 512m ») en Mo ; 0 = pas de plafond (valeur absente ou
    vide). Forme stricte : un entier positif et `m` ou `g`, rien d'autre (jamais de texte passé tel quel à docker), au
    moins 64 Mo, au plus 1024 Go. ValueError sinon."""
    if value is None or value is False or (isinstance(value, str) and not value.strip()):
        return 0
    m = _MEMORY_LIMIT.fullmatch(value.strip()) if isinstance(value, str) else None
    if m is None:
        raise ValueError(f"local.code.memory invalide : {value!r} (attendu : entier + m ou g, ex. « 2g », « 512m »)")
    mb = int(m.group(1)) * (1024 if m.group(2) in "gG" else 1)
    if not 64 <= mb <= 1024 * 1024:
        raise ValueError(f"local.code.memory hors bornes : {value!r} (64m à 1024g)")
    return mb


def parse_cpus_limit(value: Any) -> float:
    """Plafond CPU du bac (`local.code.cpus`, ex. 2 ou 1.5) ; 0 = pas de plafond (valeur absente ou 0). Nombre positif
    (au plus deux décimales, 0.1 à 256), entier, flottant ou texte de cette forme ; un booléen, un exposant, « inf » ou
    toute autre chaîne est refusé. ValueError sinon."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return 0.0
    bad = ValueError(f"local.code.cpus invalide : {value!r} (attendu : nombre, ex. 2 ou 1.5)")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise bad
    text = value.strip() if isinstance(value, str) else (f"{value:.2f}".rstrip("0").rstrip(".") if isinstance(value, float)
                                                       else str(value))
    if not _CPUS_LIMIT.fullmatch(text):
        raise bad
    n = float(text)
    if n == 0:
        return 0.0
    if not 0.1 <= n <= 256:
        raise ValueError(f"local.code.cpus hors bornes : {value!r} (0.1 à 256)")
    return n


_VOLUME_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_VOLUME_SUBPATH = re.compile(r"[A-Za-z0-9_./@+=-]{1,512}")


@dataclass
class CodeToolSource:
    """`ToolSource` des outils de code sur le dossier hôte `root`.

    backend « docker » (défaut) : un container jetable par fil, `root` monté sur /workspace, rien d'autre de l'hôte.
    backend « local » : les commandes tournent sur l'hôte, dans `root` ; les outils de fichiers refusent tout chemin
    hors de `root` (les commandes, elles, ne sont bornées que par leurs portes : à réserver aux tests et à un
    utilisateur qui l'a choisi)."""

    root: str
    backend: str = "docker"
    image: str = DEFAULT_SANDBOX_IMAGE
    network: bool = True
    # Plafonds du bac docker (`local.code.memory` / `local.code.cpus`) : mémoire en Mo (`--memory`) et CPU (`--cpus`) ;
    # 0 = pas de plafond (comportement d'avant). Validés par `parse_memory_limit` / `parse_cpus_limit`.
    memory_mb: int = 0
    cpus: float = 0.0
    # Réseau docker des bacs à sable quand `network` est vrai. Compose Lokkal : `nokker-sandbox` (sortie Internet
    # seulement : ni Postgres, ni noyau, ni proxy docker, ni LAN, ni 169.254.169.254 ; le proxy refuse tout autre
    # NetworkMode que lui ou none). None = réseau par défaut du démon (tests, dev).
    network_name: str | None = None
    # Chemin du dossier tel que le VOIT le démon docker. Le noyau tourne lui-même dans un container (compose Lokkal) :
    # un `-v` part de l'hôte, pas du container du noyau. None = `root` (noyau sur l'hôte, ou dossier monté au même
    # chemin des deux côtés).
    host_root: str | None = None
    # Chemin du dossier DANS le bac à sable. /workspace par défaut ; un banc peut monter le dépôt là où l'image de
    # l'environnement l'attend (SWE-bench : /testbed, où l'installation éditable du projet pointe).
    mount: str = WORKSPACE_MOUNT
    # Volume docker NOMMÉ qui porte `root` (hôte embarquant le noyau dans un container : ses dossiers de travail sont dans
    # un volume, pas à un chemin de l'hôte). Le bac monte alors CE volume sur `mount`, restreint au sous-dossier
    # `volume_subpath` (`--mount type=volume,…,volume-subpath=`, docker >= 26) : jamais le volume entier, donc jamais les
    # dossiers des autres sessions. Vide = montage par chemin (`host_root`).
    volume: str | None = None
    volume_subpath: str = ""
    # Identifiant du noyau qui possède ce bac à sable (étiquette `nokker.instance`) : son ménage au démarrage ne
    # touche que les siens. None = pas d'étiquette (banc, `nokker code`), donc jamais ramassé par un noyau.
    instance: str | None = None
    # docker : lire et chercher directement dans le dossier monté (le noyau le voit au même chemin, ou via host_root
    # sur l'hôte). False = tout passe par le bac à sable.
    local_reads: bool = True
    core: bool = True  # une session de code a besoin de ces schémas à chaque tour : dans le préfixe stable
    # Sous-dossier de `root` (relatif, ex. « .nokker/worktrees/ab12 ») où les outils démarrent et qu'ils ne quittent
    # pas en local. En docker, `root` reste monté EN ENTIER sur /workspace (le `.git` d'un worktree pointe vers le
    # dépôt par un chemin relatif : il doit voir les deux) ; seul le dossier de départ change. Voir `scoped()`.
    subdir: str = ""
    # Commandes en arrière-plan (lot X6, `local.background_processes`) : outils process_* présents seulement si vrai.
    background: bool = False
    # backend « kubernetes » (sessions de code multi-utilisateur, docs/LOKKAL.md §7.28) : l'environnement d'exécution
    # vient de `env_factory`, appelée avec la signature du module du bac à sable kubernetes
    # (`KubernetesEnvironment(cfg, cwd, sub_path, labels, timeout)`) ; `k8s` = le bloc `code.kubernetes` tel quel,
    # `sub_path` = la racine relative au volume des espaces de travail, `labels` = étiquettes du pod. Les lectures
    # passent par le volume monté côté noyau (`root`), comme en docker ; écritures et commandes par l'environnement.
    env_factory: Any = None
    k8s: dict[str, Any] = field(default_factory=dict)
    sub_path: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    # Backend local : journaux des processus en arrière-plan sous `<state_dir>/processes/<fil>/` (et fiches de ménage,
    # `reap_processes`). None = dossier temporaire de l'utilisateur.
    state_dir: str | None = None
    # Diagnostics de langage (lot 13, `local.lsp.enabled`) : le `nokker.tools.lsp.LspManager` de l'instance (plafonds
    # partagés entre fils), ou None = fonction absente (outil absent, résultats intacts).
    lsp: Any = None
    # Injection des secrets à la sortie (docs/EGRESS-SECRETS.md) : variables ajoutées à l'environnement du bac à sable
    # (URL du proxy avec le jeton de session, certificat de l'AC, noms disponibles). Vide = environnement d'origine.
    sandbox_env: dict[str, str] = field(default_factory=dict)
    _spaces: dict[str, Workspace] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    last_used: float = field(default=0.0, init=False, repr=False)  # dernier appel d'outil (monotone), pour l'expiration

    def __post_init__(self) -> None:
        self.root = os.path.realpath(self.root)
        if self.backend not in ("docker", "local", "kubernetes"):
            raise ValueError(f"backend inconnu : {self.backend!r} (docker | local | kubernetes)")
        if self.backend == "kubernetes":
            if not callable(self.env_factory):
                raise ValueError("backend kubernetes : fabrique d'environnement absente (nokker/tools/k8s_sandbox.py)")
            if self.mount == WORKSPACE_MOUNT:
                self.mount = str(self.k8s.get("workspace_mount") or WORKSPACE_MOUNT)
        if not os.path.isdir(self.root):
            raise ValueError(f"dossier de travail introuvable : {self.root}")
        if self.volume is not None:
            if not _VOLUME_NAME.fullmatch(self.volume):
                raise ValueError(f"nom de volume invalide : {self.volume!r}")
            self.volume_subpath = self.volume_subpath.strip("/")
            if not _VOLUME_SUBPATH.fullmatch(self.volume_subpath or "x") or ".." in self.volume_subpath.split("/"):
                raise ValueError(f"sous-dossier du volume invalide : {self.volume_subpath!r}")
        self.memory_mb = parse_memory_limit(f"{self.memory_mb}m") if self.memory_mb else 0
        self.cpus = parse_cpus_limit(self.cpus) if self.cpus else 0.0
        self.last_used = time.monotonic()
        self.subdir = self.subdir.strip("/")
        if self.subdir and (os.path.isabs(self.subdir) or ".." in self.subdir.split("/")):
            raise ValueError(f"sous-dossier invalide : {self.subdir!r}")

    @property
    def _base(self) -> str:
        """Dossier de départ côté hôte."""
        return os.path.join(self.root, self.subdir) if self.subdir else self.root

    def scoped(self, subdir: str) -> "CodeToolSource":
        """Une source de même configuration, ouverte sur `subdir` (relatif à `root`) : utilisée pour qu'un sous-agent
        travaille dans son worktree. Ses espaces de travail sont les siens (à fermer par l'appelant)."""
        import dataclasses

        return dataclasses.replace(self, subdir=subdir)

    def share_workspace(self, parent_thread: str, thread_id: str, subdir: str = "") -> Workspace:
        """Espace d'un sous-agent (`thread_id`) dans le MÊME bac à sable que `parent_thread` : l'environnement du
        parent (même container, même pod — jamais un second), vu par un `SharedEnv` qui garde son propre dossier courant
        (`<montage>/<subdir>`, son worktree) et ne ferme jamais le bac à sable. Sessions de code multi-utilisateur
        (docs/LOKKAL.md §7.28) ; idempotent ; `close(thread_id)` le retire sans toucher au parent."""
        from tools.file_operations import ShellFileOperations
        from tools.todo_tool import TodoStore

        subdir = subdir.strip("/")
        if subdir and (os.path.isabs(subdir) or ".." in subdir.split("/")):
            raise ValueError(f"sous-dossier invalide : {subdir!r}")
        with self._lock:
            ws = self._spaces.get(thread_id)
        if ws is not None:
            return ws
        parent = self.workspace(parent_thread)
        cwd = self.mount + ("/" + subdir if subdir else "") if self.backend != "local" else \
            (os.path.join(self.root, subdir) if subdir else self.root)
        env = SharedEnv(parent.env, cwd)
        reader = None
        if parent.reader is not None:
            from tools.environments.local import LocalEnvironment

            base = os.path.join(self.root, subdir) if subdir else self.root
            reader = ShellFileOperations(LocalEnvironment(cwd=base, timeout=60), cwd=base)
        ws = Workspace(env=env, ops=ShellFileOperations(env, cwd=cwd), todos=TodoStore(), cwd=cwd, reader=reader)
        with self._lock:
            return self._spaces.setdefault(thread_id, ws)

    def specs(self) -> list[ToolSpec]:
        specs = (*_SPECS, *_PROCESS_SPECS) if self.background else _SPECS
        out = [ToolSpec(name=n, description=d, parameters=p, effect=e, core=self.core, source="builtin:code",
                        effect_of=(lambda a: classify_command(str(a.get("command", "")))) if n in _CLASSIFIED
                        else None, fresh=n in ("process_list", "process_logs"))
               for n, d, e, p in specs]
        if self.lsp is not None:  # hors préfixe stable (index des outils) : seulement drapeau allumé
            from nokker.tools.lsp import SPEC

            out.append(SPEC)
        return out

    # ── cycle de vie ──
    def workspace(self, thread_id: str) -> Workspace:
        with self._lock:
            ws = self._spaces.get(thread_id)
            if ws is None:
                ws = self._spaces[thread_id] = self._open(thread_id)
                from nokker.qa.secrets import add_known_path

                for known in (ws.cwd, self.root, self.mount, getattr(self, "_base", "")):  # racines connues : pas d'entropie
                    add_known_path(known)
            return ws

    def _allow_writes_in_workspace(self) -> None:
        """L'amont refuse toute écriture hors de HERMES_WRITE_SAFE_ROOT quand la variable est posée (l'image nokker
        la pose à /opt/data pour la gateway). On y ajoute le dossier de travail, tel que les outils le voient ; on
        n'y touche pas si elle est absente (aucune restriction à élargir)."""
        current = os.environ.get("HERMES_WRITE_SAFE_ROOT")
        if not current:
            return
        roots = current.split(os.pathsep)
        wanted = self.root if self.backend == "local" else self.mount
        if wanted not in roots:
            os.environ["HERMES_WRITE_SAFE_ROOT"] = os.pathsep.join([*roots, wanted])

    def _open(self, thread_id: str) -> Workspace:
        from tools.file_operations import ShellFileOperations
        from tools.todo_tool import TodoStore

        self._allow_writes_in_workspace()
        if self.backend == "local":
            from tools.environments.local import LocalEnvironment

            env, cwd = LocalEnvironment(cwd=self._base, timeout=120), self._base
        elif self.backend == "kubernetes":
            cwd = self.mount + ("/" + self.subdir if self.subdir else "")
            extra = {"env": dict(self.sandbox_env)} if self.sandbox_env else {}
            env = self.env_factory(cfg=dict(self.k8s), cwd=cwd, sub_path=self.sub_path, labels=dict(self.labels),
                                   timeout=120, **extra)
        else:
            from tools.environments import docker as upstream_docker
            from tools.environments.docker import DockerEnvironment

            # L'amont monte en lecture seule ses propres dossiers (skills, caches, identifiants de ~/.hermes) dans
            # chaque container. Lokkal n'en veut aucun : ses skills sont les siens (L3), et ces chemins, pris dans le
            # container du noyau, n'existent pas sur l'hôte (le proxy du socket les refuserait).
            upstream_docker._readonly_skill_mount_args = lambda: []
            # /tmp du bac à sable : l'amont le monte sans `exec`, donc noexec et à root. Sous l'uid de l'utilisateur
            # (run_as_host_user), un venv ou npm y échouent (recette du 23.10) : exécutable et ouvert à tous
            # (sticky), et HOME=/tmp ci-dessous pour les caches de pip et npm.
            upstream_docker._BASE_SECURITY_ARGS[:] = [
                "/tmp:rw,exec,nosuid,size=1g,mode=1777" if a.startswith("/tmp:") else a
                for a in upstream_docker._BASE_SECURITY_ARGS]

            safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", thread_id)[:48] or "default"
            # Étiquette `nokker.sandbox` : le proxy du socket docker du compose n'accepte que ces containers-là.
            cwd = self.mount + ("/" + self.subdir if self.subdir else "")
            if self.volume is not None:  # volume nommé, sous-dossier seulement (`--mount` : pas de `:` ni de virgule à échapper)
                mounts = ["--mount", f"type=volume,source={self.volume},target={self.mount}"
                          + (f",volume-subpath={self.volume_subpath}" if self.volume_subpath else "")]
                volumes: list[str] = []
            else:
                mounts, volumes = [], [f"{self.host_root or self.root}:{self.mount}"]
            try:
                env = DockerEnvironment(image=self.image, cwd=cwd, timeout=120, task_id=f"nokker-{safe}",
                                        volumes=volumes, network=self.network,
                                        cpu=self.cpus, memory=self.memory_mb,
                                        persistent_filesystem=False, persist_across_processes=False,
                                        # --user <uid:gid> du processus : les fichiers créés dans le dépôt
                                        # appartiennent à l'utilisateur, pas à root (compose : le noyau tourne sous
                                        # l'uid de l'hôte).
                                        run_as_host_user=True, env={"HOME": "/tmp", **self.sandbox_env},
                                        extra_args=[*mounts, "--label", f"nokker.sandbox={safe}",
                                                    *(["--label", f"nokker.instance={self.instance}"]
                                                      if self.instance else []),
                                                    *(["--network", self.network_name]
                                                      if self.network and self.network_name else [])])
            except Exception as e:  # l'amont dit « bascule sur Local » : jamais au modèle
                if type(e).__name__ == "EnvironmentConnectionError":
                    raise SandboxUnavailable(str(e)) from e
                if isinstance(e, subprocess.SubprocessError | OSError):
                    # `docker run` refusé (plage de CPU ou de mémoire invalide, image, tout refus du démon) : refus net de la
                    # session, jamais une trace Python ni un 502. Rien d'autre n'est attrapé ici (un bug reste un bug).
                    asked = ", ".join([f"image {self.image}", *([f"cpus {self.cpus:g}"] if self.cpus else []),
                                       *([f"mémoire {self.memory_mb} Mo"] if self.memory_mb else [])])
                    raise SandboxCreateRefused(_docker_refusal(e), asked) from None
                raise
        if self.backend == "docker" and (self.memory_mb or self.cpus):
            self._require_limits(env)
        reader = None
        if self.backend in ("docker", "kubernetes") and self.local_reads:
            from tools.environments.local import LocalEnvironment

            reader = ShellFileOperations(LocalEnvironment(cwd=self._base, timeout=60), cwd=self._base)
        return Workspace(env=env, ops=ShellFileOperations(env, cwd=cwd), todos=TodoStore(), cwd=cwd, reader=reader)

    @staticmethod
    def _inspect_limits(container_id: str) -> tuple[int, int]:
        """(HostConfig.Memory, HostConfig.NanoCpus) du conteneur, lus par `docker inspect`."""
        import subprocess

        r = subprocess.run(["docker", "inspect", "--format", "{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}", container_id],
                           capture_output=True, text=True, timeout=30, check=False)
        mem, nano = r.stdout.split()
        return int(mem), int(nano)

    def _require_limits(self, env: Any) -> None:
        """Les plafonds demandés sont-ils POSÉS sur le conteneur ? L'amont ne les pose que si les contrôleurs cgroup sont
        délégués, sinon il les omet avec un avertissement et le bac démarre sans plafond. Vérifié par `docker inspect` ;
        un écart (ou une lecture impossible) supprime le bac et refuse : `SandboxLimitsNotApplied`."""
        want_mem, want_nano = self.memory_mb * 1024 * 1024, round(self.cpus * 1_000_000_000)
        problem = ""
        try:
            cid = getattr(env, "_container_id", None)
            if not cid:
                raise LookupError("conteneur introuvable")
            mem, nano = self._inspect_limits(cid)
            if self.memory_mb and mem != want_mem:
                problem = f"mémoire demandée {self.memory_mb} Mo, posée {mem // (1024 * 1024)} Mo"
            elif self.cpus and abs(nano - want_nano) > 1_000_000:
                problem = f"cpus demandés {self.cpus:g}, posés {nano / 1e9:g}"
        except Exception as e:  # noqa: BLE001 — impossible de vérifier : même refus
            problem = f"vérification impossible ({type(e).__name__})"
        if not problem:
            return
        logger.error("bac à sable refusé : %s", problem)
        try:
            env.cleanup()
            from tools.environments.docker import DockerEnvironment

            DockerEnvironment.wait_for_all_teardowns(20.0)
        except Exception:  # le ménage au démarrage suivant rattrape un reste
            logger.debug("suppression du bac refusé en échec", exc_info=True)
        raise SandboxLimitsNotApplied(problem)

    def _host_roots(self) -> list[str]:
        """Les racines du dossier de session côté hôte, telles que le modèle ou une erreur peut les citer : vue du noyau
        (réelle et telle que donnée) et vue du démon docker (`host_root`)."""
        roots = {os.path.realpath(self.root), self.root.rstrip("/") or "/"}
        if self.host_root:
            roots.update({self.host_root.rstrip("/") or "/", os.path.realpath(self.host_root)})
        return sorted((r for r in roots if r != "/"), key=len, reverse=True)

    def _to_mount_view(self, path: str) -> str:
        """Bac à sable : un chemin ABSOLU de l'hôte situé sous le dossier de la session (ou sous la racine vue par le
        démon) est traduit vers le montage (`/host/ws/proj/a.py` → `/workspace/a.py`). Le modèle voit /workspace ; un
        chemin déjà sous le montage, un chemin relatif, un chemin hors du dossier (voisin, parent, `..` qui en sort) ne
        bougent pas : le bac ne voit pas l'hôte, rien n'en sort. Normalisation lexicale (`..`, `//`, `./`) pour juger de
        l'appartenance, comme le fait déjà `_inside`. Backend local : aucune traduction (les chemins sont ceux de l'hôte)."""
        import posixpath

        if self.backend == "local" or not isinstance(path, str):
            return path
        raw = path.strip()
        if not raw.startswith("/"):
            return path
        norm = posixpath.normpath(raw)
        if norm.startswith("//"):  # POSIX garde « // » initial : le tenir pour « / »
            norm = "/" + norm.lstrip("/")
        if norm == self.mount or norm.startswith(self.mount + "/"):
            return path  # déjà vu du bac : jamais de double traduction
        for root in self._host_roots():
            if norm == root or norm.startswith(root + "/"):
                out = self.mount + norm[len(root):]
                return out + "/" if raw.endswith("/") and not out.endswith("/") else out
        return path

    def _host_path(self, ws: Workspace, path: str) -> str | None:
        """docker : le chemin hôte d'un chemin vu dans le container s'il est sous /workspace (liens symboliques résolus,
        borné au dossier monté), sinon None (lecture laissée au bac à sable)."""
        import posixpath

        p = posixpath.normpath(posixpath.join(ws.cwd, self._to_mount_view((path or ".").strip())))
        if p != self.mount and not p.startswith(self.mount + "/"):
            return None
        root = os.path.realpath(self.root)
        full = os.path.realpath(os.path.join(root, p[len(self.mount):].lstrip("/")))
        if full != root and not full.startswith(root + os.sep):
            raise OutsideRoot(mt.pick(mt.CODE_OUTSIDE_ROOT).format(path=repr(path)))
        return full

    def _to_container(self, text: str) -> str:
        """Le modèle ne voit jamais les chemins de l'hôte : ils redeviennent /workspace/…."""
        for root in {os.path.realpath(self.root), self.root}:
            text = text.replace(root + "/", self.mount + "/").replace(f'"{root}"', f'"{self.mount}"')
        return text

    def close(self, thread_id: str | None = None) -> None:
        with self._lock:
            spaces = list(self._spaces.items()) if thread_id is None else \
                [(thread_id, self._spaces[thread_id])] if thread_id in self._spaces else []
            for tid, _ in spaces:
                self._spaces.pop(tid, None)
        for _, ws in spaces:
            ws.close()

    # ── appels ──
    def _inside(self, ws: Workspace, path: str) -> str:
        """Chemin vu par l'environnement. En local, refuse ce qui sort de `root` (liens symboliques compris)."""
        path = (path or ".").strip()
        if self.backend != "local":
            import posixpath

            full = posixpath.normpath(posixpath.join(ws.cwd, self._to_mount_view(path)))
            if full.startswith("//"):  # POSIX garde « // » initial : le tenir pour « / »
                full = "/" + full.lstrip("/")
            # n°27 : un chemin absolu hors du montage (ou un `..` qui en sort) atterrirait dans le système de fichiers
            # ÉPHÉMÈRE du bac : l'écriture serait perdue en silence. Même refus qu'en local. Limite : un lien symbolique
            # DANS le montage qui pointe hors /workspace n'est jugé que du côté noyau (lectures : `_host_path`) ; les
            # écritures ne sont bornées que lexicalement.
            if full != self.mount and not full.startswith(self.mount + "/"):
                raise OutsideRoot(mt.pick(mt.OUTSIDE_ROOT_DETAIL).format(path=repr(path), base=self.mount))
            return full
        base = os.path.realpath(self._base)
        full = os.path.realpath(os.path.join(base, os.path.expanduser(path)))
        if full != base and not full.startswith(base + os.sep):
            raise OutsideRoot(mt.pick(mt.OUTSIDE_ROOT_DETAIL).format(path=repr(path), base=base))
        return full

    def idle_seconds(self, now: float | None = None) -> float:
        """Secondes depuis le dernier appel d'outil de cette source (ouverture comprise)."""
        return (time.monotonic() if now is None else now) - self.last_used

    def has_live_processes(self) -> bool:
        """Un processus en arrière-plan tourne-t-il dans un de ses espaces ? (l'expiration ne l'abat pas)."""
        with self._lock:
            tables = [ws.procs for ws in self._spaces.values() if ws.procs is not None]
        return any(t.running() for t in tables)

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.last_used = time.monotonic()

        def out(payload: Any, error: bool = False, exit_code: int | None = None) -> ToolResult:
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            return ToolResult(call_id="", name=name, content=text, is_error=error, exit_code=exit_code)

        if name not in CODE_TOOLS and not (self.background and name in PROCESS_TOOLS) \
                and not (self.lsp is not None and name == LSP_TOOL):
            return out(mt.pick(mt.UNKNOWN_TOOL_BARE).format(name=name), True)
        try:
            ws = self.workspace(ctx.thread_id)
            a = arguments
            # Chemin vérifié AVANT tout appel à l'amont : un refus ne touche jamais l'environnement.
            if name in ("read_file", "search_files"):
                # Lectures : directement dans le dossier monté quand c'est possible (0-80 ms au lieu de 0,4-2 s par
                # `docker exec`, mesuré le 23.10) ; elles n'exécutent aucun code du dépôt. Écritures, patchs (et leur
                # lint) et commandes restent dans le bac à sable.
                host = self._host_path(ws, str(a.get("path") or ".")) if ws.reader is not None else None
                ops, path = (ws.reader, host) if host is not None else (ws.ops, self._inside(ws, str(a.get("path") or ".")))
                if name == "read_file":
                    if not a.get("path"):
                        raise KeyError("path")
                    # n°host-3f/banc 6 : `read_file` sur un DOSSIER rend sa liste (bornée, filtrée), pas une erreur brute.
                    if os.path.isdir(path):  # local et lecture directe (docker avec `reader`) : chemin hôte déjà confiné
                        return self._dir_reply(self._scan_dir(path), a, out, self._search_visible(ctx))
                    r = ops.read_file(path, int(a.get("offset") or 1), min(int(a.get("limit") or 500), 2000)).to_dict()
                    if r.get("not_found"):
                        self._missing_hint(ws, str(a.get("path") or ""), r)
                    elif "not a regular file" in str(r.get("error") or ""):  # bac à sable : `ls` fixe, chemin en argument cité
                        names = self._list_dir(ws, path)
                        if names is not None:
                            return self._dir_reply(names, a, out, self._search_visible(ctx))
                else:
                    r = ops.search(str(a["pattern"]), path=path, target=str(a.get("target") or "content"),
                                   file_glob=a.get("file_glob"), limit=min(int(a.get("limit") or 50), 200),
                                   context=min(int(a.get("context") or 0), 10)).to_dict()
                    self._empty_search_hint(ops, path, a, r)
                images = _image_of(r, str(a.get("path") or "")) if name == "read_file" else []
                text = json.dumps(r, ensure_ascii=False)
                text = self._to_container(text) if host is not None else text
                from nokker.tools.result_fence import fence_file_result

                # r44 : contenu d'un fichier, lignes trouvées : clôturés ; chemin et compteurs hors clôture ; une
                # image : une ligne de description, ses octets dans `ToolResult.images` (jamais de base64 en texte).
                if a.get("path"):  # l'en-tête du résultat cite le chemin vu du bac : le modèle voit /workspace
                    a = {**a, "path": self._to_mount_view(str(a["path"]))}
                res = out(fence_file_result(name, a, text), bool(r.get("error")))
                res.images = images
                return res
            if name == "write_file":
                path, content = self._inside(ws, str(a["path"])), str(a["content"])
                r = ws.ops.write_file(path, content).to_dict()
                return self._with_diagnostics(ws, ctx.thread_id, name, a, out(r, bool(r.get("error"))))
            if name == "patch":
                return self._with_diagnostics(ws, ctx.thread_id, name, a, self._patch(ws, a, out))
            if name == LSP_TOOL:
                return self._lsp_tool(ws, ctx.thread_id, a, out)
            if name == "terminal":
                return self._terminal(ws, a, out)
            if name in PROCESS_TOOLS:
                return self._process(ws, ctx.thread_id, name, a, out)
            from tools.todo_tool import todo_tool

            text = todo_tool(a.get("todos"), bool(a.get("merge")), store=ws.todos)
            return out(text, "error" in json.loads(text))
        except SandboxUnavailable as e:
            logger.warning("bac à sable indisponible : %s", e.detail)
            why = (mt.pick(mt.SANDBOX_LIMITS_FOR_MODEL) if isinstance(e, SandboxLimitsNotApplied)
                   else mt.pick(mt.SANDBOX_CREATE_FOR_MODEL) if isinstance(e, SandboxCreateRefused) else e)
            return out(mt.pick(mt.REFUSED_WHY).format(why=why), True)
        except PermissionError as e:
            res = out(mt.pick(mt.REFUSED_WHY).format(why=e), True)
            if getattr(e, "outside_root", False):   # le chemin sort du dossier : refus pour l'hôte, le modèle ne change pas
                res.refused, res.reason = True, OUTSIDE_ROOT
            return res
        except ProcessError as e:
            return out(str(e), True)
        except KeyError as e:
            return out(mt.pick(mt.MISSING_ARG).format(name=e.args[0]), True)
        except Exception as e:  # noqa: BLE001 — l'erreur revient au modèle, la boucle continue
            return out(f"{type(e).__name__}: {e}", True)

    def _patch(self, ws: Workspace, a: dict[str, Any], out: Any) -> ToolResult:
        mode = str(a.get("mode") or "replace")
        if mode == "replace":
            if a.get("old_string") is None or a.get("new_string") is None or not a.get("path"):
                return out(mt.pick(mt.CODE_REPLACE_ARGS), True)
            path = self._inside(ws, str(a["path"]))
            r = ws.ops.patch_replace(path, str(a["old_string"]), str(a["new_string"]),
                                     bool(a.get("replace_all"))).to_dict()
        elif mode == "patch":
            text = str(a.get("patch") or "")
            if not text:
                return out(mt.pick(mt.PATCH_REQUIRED), True)
            # En-têtes réécrits en chemins absolus (vérifiés en local) : l'amont contrôle chaque écriture contre
            # HERMES_WRITE_SAFE_ROOT, et un chemin relatif s'y lirait depuis le dossier du processus, pas d'ici.
            text = re.sub(r"^(\*\*\* (?:Update|Add|Delete) File: )(.+)$",
                          lambda m: m.group(1) + self._inside(ws, m.group(2).strip()), text, flags=re.MULTILINE)
            text = re.sub(r"^(\*\*\* Move File: )(.+?) -> (.+)$",
                          lambda m: f"{m.group(1)}{self._inside(ws, m.group(2).strip())} -> "
                                    f"{self._inside(ws, m.group(3).strip())}", text, flags=re.MULTILINE)
            r = ws.ops.patch_v4a(text).to_dict()
        else:
            return out(mt.pick(mt.CODE_UNKNOWN_MODE).format(mode=repr(mode)), True)
        if r.get("error") and "Could not find" in str(r["error"]):
            r["_hint"] = mt.pick(mt.EDIT_NOT_FOUND)
        elif r.get("error") and mode == "replace" and not self._exists(ws, path):
            self._missing_hint(ws, str(a.get("path") or ""), r)
        return out(r, bool(r.get("error")))

    # ── indice de chemins (p2/v-readfile-hint) ──
    def _exists(self, ws: Workspace, inside: str) -> bool:
        import shlex

        try:
            return int(ws.env.execute(f"test -e {shlex.quote(inside)}", timeout=10).get("returncode", 1)) == 0
        except Exception:  # noqa: BLE001 — sans réponse, on ne dit rien de plus
            return True

    def _empty_search_hint(self, ops: Any, path: str, a: dict[str, Any], r: dict[str, Any]) -> None:
        """Une recherche à 0 résultat ne dit pas « dépôt vide » : si le dossier contient des fichiers, le résultat le
        dit (nombre et premiers noms) et rappelle que `target=files` attend un glob (« * »), pas une expression
        régulière (« .* » n'y retient que les noms cachés). Recette rc.9.30 : un 0 a fait conclure « dépôt vide »."""
        if r.get("error") or r.get("matches") or r.get("files") or r.get("matches_text") or r.get("counts"):
            return
        if int(r.get("total_count") or 0) != 0:
            return
        try:
            names = [n for n in (self._list_dir_env(getattr(ops, "env", None), path) or []) if n != ".git/"]
        except Exception:  # noqa: BLE001 — l'aide ne casse jamais le résultat d'origine
            return
        if not names:
            return
        shown = ", ".join(names[:8]) + (" …" if len(names) > 8 else "")
        text = mt.pick(mt.SEARCH_EMPTY_DIR).format(n=len(names), shown=shown)
        if str(a.get("target") or "content") == "files":
            text += mt.pick(mt.SEARCH_GLOB)
        r["warning"] = (f"{r['warning']} " if r.get("warning") else "") + text + "."

    def _list_dir(self, ws: Workspace, inside: str) -> list[str] | None:
        """Entrées d'un dossier (« / » final pour un dossier) ; None si le dossier n'existe pas ou n'est pas lisible."""
        return self._list_dir_env(ws.env, inside)

    @staticmethod
    def _list_dir_env(env: Any, inside: str) -> list[str] | None:
        import shlex

        if env is None:
            return None
        try:
            q = shlex.quote(inside)
            r = env.execute(f"test -d {q} && ls -1Ap -- {q} 2>/dev/null | head -400", timeout=10)
        except Exception:  # noqa: BLE001
            return None
        if int(r.get("returncode", 1)) != 0:
            return None
        return [x for x in str(r.get("output") or "").splitlines() if x]

    @staticmethod
    def _scan_dir(path: str) -> list[str]:
        """Entrées d'un dossier de l'hôte, au format de `_list_dir_env` (« / » final pour un dossier, lien compris)."""
        try:
            with os.scandir(path) as it:
                return [e.name + ("/" if e.is_dir() else "") for e in it][:400]
        except OSError:
            return []

    def _dir_reply(self, names: list[str], a: dict[str, Any], out: Any, with_tail: bool = True) -> ToolResult:
        """Réponse (non erreur) à `read_file` d'un dossier : ≤ 50 entrées, dossiers puis fichiers, alphabétique. Mêmes filtres
        que l'indice de chemins (`readfile_hint.visible` : cachés, ignorés, secrets) : un nom masqué n'est jamais cité. Le
        dossier demandé lui-même peut être caché (`.sokkan-kernel`) : seules ses ENTRÉES passent le filtre, comme une recherche
        dont la racine est explicite."""
        from nokker.qa.untrusted import inline
        from nokker.tools.readfile_hint import visible

        shown_path = inline(self._to_mount_view(str(a.get("path") or ".")), 120)
        kept = sorted(visible(names), key=lambda n: (not n.endswith("/"), n.lower(), n))
        if not kept:
            return out(mt.pick(mt.DIR_EMPTY).format(path=shown_path))  # ne cite aucun outil
        top = kept[:MAX_DIR_ENTRIES]
        listing = "\n".join(inline(n, 80) for n in top)
        if len(kept) > len(top):
            listing += "\n" + mt.pick(mt.DIR_MORE).format(k=len(kept) - len(top))
            head = mt.pick(mt.DIR_PART).format(path=shown_path, n=len(top), total=len(kept))
        else:
            head = mt.pick(mt.DIR_ALL).format(path=shown_path, n=len(top))
        tail = f"\n{mt.pick(mt.DIR_TAIL)}" if with_tail else ""  # search_files retiré du fil : on ne le nomme pas
        return out(f"{head}\n{listing}{tail}")

    @staticmethod
    def _search_visible(ctx: ToolContext) -> bool:
        """`search_files` est-il dans le catalogue de ce fil ? Faux seulement s'il est retiré (`deny_tools`, registre par fil,
        trouvé depuis le noyau de l'appel comme le fait `loop._note_tool_removed`) ; sans noyau (appel direct) ou registre
        illisible : vrai, comme avant."""
        reg, seen = getattr(getattr(ctx, "kernel", None), "tools", None), 0
        while reg is not None and not callable(getattr(reg, "hidden_for", None)) and seen < 8:
            reg, seen = getattr(reg, "inner", None), seen + 1
        hidden = getattr(reg, "hidden_for", None)
        try:
            return not (callable(hidden) and "search_files" in hidden(ctx.thread_id))
        except Exception:  # noqa: BLE001
            return True

    def _missing_hint(self, ws: Workspace, rel: str, result: dict[str, Any]) -> None:
        """Ajoute à une erreur « fichier absent » la liste bornée des entrées réelles (docs : `tools/readfile_hint.py`)."""
        import posixpath

        from nokker.tools.readfile_hint import hint_text, visible

        try:
            directory = posixpath.dirname((rel or "").strip()) or "."
            names = self._list_dir(ws, self._inside(ws, directory))
            same = names is not None
            where = directory
            if names is None:  # le dossier demandé n'existe pas : la racine du dossier de travail
                names, where = self._list_dir(ws, self._inside(ws, ".")) or [], directory
            text = hint_text(visible(names), same_dir=same, where=where)
        except Exception:  # noqa: BLE001 — l'aide ne casse jamais l'erreur d'origine
            return
        result["hint"] = (str(result["hint"]) + " " + text) if result.get("hint") else text

    # ── diagnostics de langage (lot 13) ──
    def _lsp_thread(self, ws: Workspace, thread_id: str) -> Any:
        with self._lock:
            if ws.lsp is None:
                ws.lsp = self.lsp.thread(thread_id, ws.env, ws.cwd)
            return ws.lsp

    def _lsp_files(self, ws: Workspace, paths: list[str]) -> list[tuple[str, str]]:
        """(chemin vu par le bac à sable, texte) des fichiers d'un langage suivi. Le texte est lu du côté du noyau (le
        dossier monté : aucun `docker exec` de plus) ; un fichier absent, hors du dossier ou trop gros est laissé."""
        from nokker.tools.lsp import MAX_FILE_BYTES

        files = []
        for p in paths:
            inside = self._inside(ws, p)
            if language_of(inside) is None:
                continue
            host = inside if self.backend == "local" else self._host_path(ws, p)
            if host is None or not os.path.isfile(host) or os.path.getsize(host) > MAX_FILE_BYTES:
                continue
            with open(host, encoding="utf-8", errors="replace") as f:
                files.append((inside, f.read()))
        return files

    def _with_diagnostics(self, ws: Workspace, thread_id: str, name: str, a: dict[str, Any],
                          result: ToolResult) -> ToolResult:
        """Après un `write_file`/`patch` réussi, drapeau allumé : le serveur du fil reçoit le texte ; en `inline`, le
        bloc `[Diagnostics]` s'ajoute à la fin (ajout pur). Toute panne laisse le résultat tel quel."""
        if self.lsp is None or result.is_error:
            return result
        try:
            from nokker.tools.checkpoints import _written_paths

            files = self._lsp_files(ws, _written_paths(name, a))
            block = self.lsp.after_edit(self._lsp_thread(ws, thread_id), files) if files else ""
        except Exception:  # noqa: BLE001 — les diagnostics ne font jamais échouer une édition
            logger.debug("diagnostics après %s en échec", name, exc_info=True)
            return result
        return dataclasses.replace(result, content=result.content + block) if block else result

    def _lsp_tool(self, ws: Workspace, thread_id: str, a: dict[str, Any], out: Any) -> ToolResult:
        raw = a.get("paths")
        if raw is not None and not (isinstance(raw, list) and all(isinstance(p, str) for p in raw)):
            return out(mt.pick(mt.CODE_PATHS_LIST), True)
        severity = str(a.get("severity") or "error")
        if severity not in ("error", "warning"):
            return out(mt.pick(mt.CODE_SEVERITY_UNKNOWN).format(severity=repr(severity)), True)
        files = self._lsp_files(ws, raw) if raw else None
        return out(self.lsp.diagnostics(self._lsp_thread(ws, thread_id), files, severity))

    @property
    def sandbox_mount(self) -> str | None:
        """Point de montage du dossier de travail DANS l'environnement d'exécution (`/workspace` par défaut, ou
        `mount`) ; None en backend local (pas de bac à sable : le chemin est celui de l'hôte)."""
        return None if self.backend == "local" else self.mount

    def workspace_path(self, thread_id: str, path: str) -> str:
        """Chemin, tel que le bac à sable le voit, d'une destination relative au dossier de travail (`ssh_copy`, lot
        X11) : borné au dossier (liens symboliques compris en local, au dossier monté en docker)."""
        ws = self.workspace(thread_id)
        full = self._inside(ws, path)
        if self.backend != "local" and full != self.mount and not full.startswith(self.mount + "/"):
            raise OutsideRoot(mt.pick(mt.CODE_OUTSIDE_ROOT).format(path=repr(path)))
        return full

    def run_hook(self, thread_id: str, command: str, stdin: str, timeout: int) -> tuple[int | None, str]:
        """Une commande de hook (nokker/tools/hooks.py) dans l'environnement du fil : le bac à sable en docker (jamais
        l'hôte), l'hôte en backend local comme `terminal`. Charge utile sur stdin, sortie bornée. Un sous-agent
        (`<fil>/sa-<id>`) passe par le bac à sable de son fil parent : pas un container de plus pour un hook."""
        ws = self.workspace(thread_id.split("/sa-", 1)[0])
        r = ws.env.execute(command, timeout=max(1, int(timeout)), stdin_data=stdin, bounded_capture=True)
        return r.get("returncode"), _clip(str(r.get("output") or ""))

    def script_command(self, thread_id: str, argv: list[str], env_names: list[str] | None = None) -> list[str]:
        """argv d'un processus INTERACTIF (stdin/stdout tenus ouverts par l'appelant) lancé DANS le bac à sable du fil,
        dans son dossier courant : `execute_code` (nokker/tools/execute_code.py) y fait passer ses appels d'outils.
        docker : `docker exec -i -w <dossier> <container> …` ; kubernetes : `kubectl exec -i <pod> -- sh -c 'cd … &&
        exec "$@"' …`. Backend local : refusé (ce serait l'hôte du noyau). Un sous-agent `<fil>/sa-<id>` sans espace à
        lui prend celui de son fil parent (même bac à sable, comme `run_hook`).

        `env_names` (serveurs MCP stdio, nokker/tools/mcp_stdio.py) : variables passées au processus PAR NOM
        (`docker exec -e NOM`) — la valeur est lue par le client docker dans SON environnement, posé par l'appelant ;
        elle n'est jamais dans la ligne de commande. docker seulement (kubectl exec ne sait pas le faire)."""
        if self.backend == "local":
            raise PermissionError(mt.pick(mt.CODE_EXEC_LOCAL))
        with self._lock:
            ws = self._spaces.get(thread_id)
        if ws is None:
            ws = self.workspace(thread_id.split("/sa-", 1)[0])
        env, cwd = ws.env, str(getattr(ws.env, "cwd", "") or ws.cwd)
        if self.backend == "kubernetes":
            if env_names:
                raise PermissionError(mt.pick(mt.CODE_EXEC_ENV_NAMES))
            before = getattr(env, "_before_execute", None)
            if callable(before):
                before()  # pod recréé s'il a été libéré pour inactivité
            return env.kubectl.exec_argv(env.pod, ["sh", "-c", 'cd "$1" && shift && exec "$@"', "sh", cwd, *argv],
                                         stdin=True)
        container = getattr(env, "_container_id", None)
        if not container:
            raise RuntimeError("bac à sable docker sans container")
        by_name = [x for n in env_names or [] for x in ("-e", n)]
        return [getattr(env, "_docker_exe", None) or "docker", "exec", "-i", *by_name, "-w", cwd, container, *argv]

    def _terminal(self, ws: Workspace, a: dict[str, Any], out: Any) -> ToolResult:
        command = str(a.get("command") or "").strip()
        if not command:
            return out(mt.pick(mt.EMPTY_COMMAND), True)
        why = hardline_reason(command)
        if why:
            return out(mt.pick(mt.HARDLINE_REFUSED).format(why=why), True)
        timeout = max(1, min(int(a.get("timeout") or 120), 600))
        # Tests à sortie coupée (`… 2>&1 | tail -N`) : le code de retour doit être celui des tests (règle de fin
        # `conclude_on_green`). Les portes et la personne ont vu la commande d'origine.
        r = ws.env.execute(pipefail_command(command) or command, timeout=timeout, bounded_capture=True)
        from nokker.qa.untrusted import fenced_result

        # La sortie d'une commande est un contenu de TIERS (fichiers du dépôt, réponses réseau, journaux) : clôturée.
        text = fenced_result({"returncode": r.get("returncode")}, "terminal:output", _clip(str(r.get("output") or "")))
        if isinstance(self, CodeToolSource) and self._host_path_failed(command, r):  # (tests appellent `_terminal(None, …)`)
            text += "\n" + mt.pick(mt.HOST_PATH_HINT)  # hors clôture : une note du noyau, pas la sortie de la commande
        return out(text, False, exit_code=r.get("returncode") if isinstance(r.get("returncode"), int) else None)

    def _host_path_failed(self, command: str, result: dict[str, Any]) -> bool:
        """Bac docker : la commande cite un chemin ABSOLU de l'hôte sous le dossier de session et a échoué sur un fichier
        introuvable ou refusé. Seulement un indice : la commande n'est jamais réécrite (le terminal ne traduit pas)."""
        if self.backend == "local" or not isinstance(result.get("returncode"), int) or result["returncode"] == 0:
            return False
        out = str(result.get("output") or "")
        if "No such file or directory" not in out and "Permission denied" not in out:
            return False
        return any(re.search(re.escape(root) + r"(?![\w.-])", command) for root in self._host_roots())

    # ── commandes en arrière-plan (lot X6) ──
    def _process_dir(self, thread_id: str) -> str:
        """Backend local : dossier des journaux du fil, sous l'état du noyau (un nom sûr + une empreinte du fil)."""
        import hashlib
        import tempfile

        base = self.state_dir or os.path.join(tempfile.gettempdir(), f"nokker-bg-{os.getuid()}")
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", thread_id)[:40] or "default"
        return os.path.join(base, "processes", f"{safe}-{hashlib.sha256(thread_id.encode()).hexdigest()[:8]}")

    def processes(self, ws: Workspace, thread_id: str) -> ProcessTable:
        with self._lock:
            if ws.procs is None:
                if self.backend == "local":
                    d = self._process_dir(thread_id)
                    os.makedirs(d, mode=0o700, exist_ok=True)
                    ws.procs = ProcessTable(ws.env, d, host_logs=True, cwd=ws.cwd)
                else:
                    ws.procs = ProcessTable(ws.env, SANDBOX_LOG_DIR, cwd=ws.cwd)
            return ws.procs

    def find_process(self, proc_id: str) -> tuple[str, Any] | None:
        """(fil, processus) d'un id `bg_…` de cette source, ou None (aperçu d'un port, docs/LOKKAL.md §7.7)."""
        with self._lock:
            spaces = list(self._spaces.items())
        for tid, ws in spaces:
            if ws.procs is not None:
                try:
                    return tid, ws.procs.get(proc_id)
                except ProcessError:
                    continue
        return None

    def process_table(self, thread_id: str) -> ProcessTable | None:
        with self._lock:
            ws = self._spaces.get(thread_id)
        return ws.procs if ws is not None else None

    def _process(self, ws: Workspace, thread_id: str, name: str, a: dict[str, Any], out: Any) -> ToolResult:
        if name == "process_start":
            command = str(a.get("command") or "").strip()
            if not command:
                return out(mt.pick(mt.EMPTY_COMMAND), True)
            why = hardline_reason(command)
            if why:
                return out(mt.pick(mt.HARDLINE_REFUSED).format(why=why), True)
            port = a.get("port")
            if port is not None:
                port = int(port)
                if not 1 <= port <= 65535:
                    return out(mt.pick(mt.CODE_PORT_INVALID).format(port=port), True)
            table = self.processes(ws, thread_id)
            proc = table.start(command, str(a.get("name") or ""), port)
            first = table.logs(proc.id, tail=40)  # ce que la commande a déjà dit (un échec immédiat se voit ici)
            hint = mt.pick(mt.CODE_PROCESS_HINT)
            if port:
                hint += mt.pick(mt.CODE_PROCESS_HINT_PORT).format(port=port)
            return out(_logs_result({**first, "hint": hint}))
        table = self.processes(ws, thread_id)
        if name == "process_list":
            return out({"processes": [p.view() for p in table.list()]})
        proc_id = str(a.get("id") or "").strip()
        if not proc_id:
            raise KeyError("id")
        if name == "process_logs":
            return out(_logs_result(table.logs(proc_id, int(a.get("tail") or 200))))
        return out(table.stop(proc_id).view())


__all__ = ["CODE_TOOLS", "PROCESS_TOOLS", "CodeToolSource", "DEFAULT_SANDBOX_IMAGE", "SharedEnv", "Workspace",
           "classify_command", "hardline_reason", "reap_sandboxes", "parse_memory_limit", "parse_cpus_limit", "is_test_command", "pipefail_command"]
