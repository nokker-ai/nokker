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

L'amont n'est importé qu'à la création d'un dossier de travail : sans lui (CI unitaire), le module se charge, les
schémas existent, et `classify_command` retombe sur un classement prudent (jamais READ, DESTRUCTIVE au moindre
doute).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import threading
from dataclasses import dataclass, field
from typing import Any

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.tools.processes import SANDBOX_LOG_DIR, ProcessError, ProcessTable, reap_processes

WORKSPACE_MOUNT = "/workspace"
# Python 3.14 + Node 22 : de quoi lancer les tests des dépôts Python et TypeScript du banc de code. Remplacée en L2
# par notre image publique signée (docs/LOKKAL.md §3).
DEFAULT_SANDBOX_IMAGE = "nikolaik/python-nodejs:python3.14-nodejs22"
MAX_OUTPUT_CHARS = 30_000  # sortie de commande gardée (tête + queue) ; le réservoir range le reste hors fenêtre

# Commandes de lecture pure : aucune écriture, aucun réseau, aucun code du dépôt exécuté. Une commande composée
# n'est READ que si CHAQUE segment l'est et qu'aucune redirection n'écrit.
_READ_ONLY = {"ls", "cat", "head", "tail", "wc", "pwd", "grep", "rg", "file", "stat", "tree", "du", "which",
              "echo", "diff", "sort", "uniq", "cut", "basename", "dirname", "realpath", "env", "printenv"}
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
        elif head not in _READ_ONLY:
            return False
    return True


def classify_command(command: str) -> Effect:
    """Effet d'une commande `terminal`. Sans la détection de l'amont : WRITE si la commande est de lecture pure
    (on ne parie pas sur READ sans la détection), DESTRUCTIVE sinon."""
    det = _upstream_detection()
    if det is None:
        return Effect.WRITE if _read_only(command) else Effect.DESTRUCTIVE
    if det.detect_hardline_command(command)[0] or det.detect_dangerous_command(command)[0]:
        return Effect.DESTRUCTIVE
    return Effect.READ if _read_only(command) else Effect.WRITE


def reap_sandboxes(instance: str, docker: str = "docker", processes_dir: str | None = None) -> int:
    """Supprime les bacs à sable de CETTE instance de noyau (`nokker.instance=<instance>`) restés d'un arrêt sans
    `close()` (container tué, machine éteinte). À appeler au démarrage du noyau local. Seulement les siens : un autre
    noyau ou un banc peut tourner sur la même machine (vu le 23.10 : un redémarrage a tué les bacs à sable d'un banc
    en cours). Rend le nombre supprimé ; 0 si docker est absent. Leurs processus en arrière-plan partent avec eux.

    `processes_dir` (backend local, `<état>/processes`) : arrête aussi les processus en arrière-plan laissés par un
    noyau tué (`reap_processes` : seulement ceux qui portent encore leur marque)."""
    import subprocess

    reaped = reap_processes(processes_dir) if processes_dir else 0
    if not instance:
        return reaped
    try:
        ids = subprocess.run([docker, "ps", "-aq", "--filter", f"label=nokker.instance={instance}"],
                             capture_output=True, text=True, timeout=30).stdout.split()
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


@dataclass
class Workspace:
    """Un environnement d'exécution amont ouvert sur le dossier, ses opérations de fichiers et son plan."""

    env: Any
    ops: Any
    todos: Any
    cwd: str  # le dossier tel que les outils le voient (/workspace en docker, le chemin hôte en local)
    reader: Any = None  # docker : lectures et recherches faites directement dans le dossier monté (voir local_reads)
    procs: Any = None  # nokker.tools.processes.ProcessTable, créée au premier process_start

    def close(self) -> None:
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


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: limit * 2 // 3], text[-(limit // 3):]
    return f"{head}\n[… {len(text) - len(head) - len(tail)} caractères coupés …]\n{tail}"


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
    _spaces: dict[str, Workspace] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

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
        return [ToolSpec(name=n, description=d, parameters=p, effect=e, core=self.core, source="builtin:code",
                         effect_of=(lambda a: classify_command(str(a.get("command", "")))) if n in _CLASSIFIED
                         else None, fresh=n in ("process_list", "process_logs"))
                for n, d, e, p in specs]

    # ── cycle de vie ──
    def workspace(self, thread_id: str) -> Workspace:
        with self._lock:
            ws = self._spaces.get(thread_id)
            if ws is None:
                ws = self._spaces[thread_id] = self._open(thread_id)
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
            env = self.env_factory(cfg=dict(self.k8s), cwd=cwd, sub_path=self.sub_path, labels=dict(self.labels),
                                   timeout=120)
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
            env = DockerEnvironment(image=self.image, cwd=cwd, timeout=120, task_id=f"nokker-{safe}",
                                    volumes=[f"{self.host_root or self.root}:{self.mount}"], network=self.network,
                                    persistent_filesystem=False, persist_across_processes=False,
                                    # --user <uid:gid> du processus : les fichiers créés dans le dépôt appartiennent à
                                    # l'utilisateur, pas à root (compose : le noyau tourne sous l'uid de l'hôte).
                                    run_as_host_user=True, env={"HOME": "/tmp"},
                                    extra_args=["--label", f"nokker.sandbox={safe}",
                                                *(["--label", f"nokker.instance={self.instance}"]
                                                  if self.instance else []),
                                                *(["--network", self.network_name]
                                                  if self.network and self.network_name else [])])
        reader = None
        if self.backend in ("docker", "kubernetes") and self.local_reads:
            from tools.environments.local import LocalEnvironment

            reader = ShellFileOperations(LocalEnvironment(cwd=self._base, timeout=60), cwd=self._base)
        return Workspace(env=env, ops=ShellFileOperations(env, cwd=cwd), todos=TodoStore(), cwd=cwd, reader=reader)

    def _host_path(self, ws: Workspace, path: str) -> str | None:
        """docker : le chemin hôte d'un chemin vu dans le container s'il est sous /workspace (liens symboliques résolus,
        borné au dossier monté), sinon None (lecture laissée au bac à sable)."""
        import posixpath

        p = posixpath.normpath(posixpath.join(ws.cwd, (path or ".").strip()))
        if p != self.mount and not p.startswith(self.mount + "/"):
            return None
        root = os.path.realpath(self.root)
        full = os.path.realpath(os.path.join(root, p[len(self.mount):].lstrip("/")))
        if full != root and not full.startswith(root + os.sep):
            raise PermissionError(f"{path!r} sort du dossier de travail")
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
        if self.backend != "local":  # le container ne voit que /workspace : chemin absolu, pas de borne de plus
            import posixpath

            return posixpath.normpath(posixpath.join(ws.cwd, path))
        base = os.path.realpath(self._base)
        full = os.path.realpath(os.path.join(base, os.path.expanduser(path)))
        if full != base and not full.startswith(base + os.sep):
            raise PermissionError(f"{path!r} est hors du dossier de travail ({base})")
        return full

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def out(payload: Any, error: bool = False) -> ToolResult:
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            return ToolResult(call_id="", name=name, content=text, is_error=error)

        if name not in CODE_TOOLS and not (self.background and name in PROCESS_TOOLS):
            return out(f"outil inconnu : {name}", True)
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
                    r = ops.read_file(path, int(a.get("offset") or 1), min(int(a.get("limit") or 500), 2000)).to_dict()
                else:
                    r = ops.search(str(a["pattern"]), path=path, target=str(a.get("target") or "content"),
                                   file_glob=a.get("file_glob"), limit=min(int(a.get("limit") or 50), 200),
                                   context=min(int(a.get("context") or 0), 10)).to_dict()
                text = json.dumps(r, ensure_ascii=False)
                return out(self._to_container(text) if host is not None else text, bool(r.get("error")))
            if name == "write_file":
                path, content = self._inside(ws, str(a["path"])), str(a["content"])
                r = ws.ops.write_file(path, content).to_dict()
                return out(r, bool(r.get("error")))
            if name == "patch":
                return self._patch(ws, a, out)
            if name == "terminal":
                return self._terminal(ws, a, out)
            if name in PROCESS_TOOLS:
                return self._process(ws, ctx.thread_id, name, a, out)
            from tools.todo_tool import todo_tool

            text = todo_tool(a.get("todos"), bool(a.get("merge")), store=ws.todos)
            return out(text, "error" in json.loads(text))
        except PermissionError as e:
            return out(f"REFUSÉ : {e}", True)
        except ProcessError as e:
            return out(str(e), True)
        except KeyError as e:
            return out(f"argument manquant : {e.args[0]}", True)
        except Exception as e:  # noqa: BLE001 — l'erreur revient au modèle, la boucle continue
            return out(f"{type(e).__name__}: {e}", True)

    def _patch(self, ws: Workspace, a: dict[str, Any], out: Any) -> ToolResult:
        mode = str(a.get("mode") or "replace")
        if mode == "replace":
            if a.get("old_string") is None or a.get("new_string") is None or not a.get("path"):
                return out("mode replace : path, old_string et new_string sont requis", True)
            path = self._inside(ws, str(a["path"]))
            r = ws.ops.patch_replace(path, str(a["old_string"]), str(a["new_string"]),
                                     bool(a.get("replace_all"))).to_dict()
        elif mode == "patch":
            text = str(a.get("patch") or "")
            if not text:
                return out("mode patch : le contenu du patch est requis", True)
            # En-têtes réécrits en chemins absolus (vérifiés en local) : l'amont contrôle chaque écriture contre
            # HERMES_WRITE_SAFE_ROOT, et un chemin relatif s'y lirait depuis le dossier du processus, pas d'ici.
            text = re.sub(r"^(\*\*\* (?:Update|Add|Delete) File: )(.+)$",
                          lambda m: m.group(1) + self._inside(ws, m.group(2).strip()), text, flags=re.MULTILINE)
            text = re.sub(r"^(\*\*\* Move File: )(.+?) -> (.+)$",
                          lambda m: f"{m.group(1)}{self._inside(ws, m.group(2).strip())} -> "
                                    f"{self._inside(ws, m.group(3).strip())}", text, flags=re.MULTILINE)
            r = ws.ops.patch_v4a(text).to_dict()
        else:
            return out(f"mode inconnu : {mode!r} (replace | patch)", True)
        if r.get("error") and "Could not find" in str(r["error"]):
            r["_hint"] = "passage introuvable : relis le fichier (read_file) ou cherche-le (search_files)"
        return out(r, bool(r.get("error")))

    def workspace_path(self, thread_id: str, path: str) -> str:
        """Chemin, tel que le bac à sable le voit, d'une destination relative au dossier de travail (`ssh_copy`, lot
        X11) : borné au dossier (liens symboliques compris en local, au dossier monté en docker)."""
        ws = self.workspace(thread_id)
        full = self._inside(ws, path)
        if self.backend != "local" and full != self.mount and not full.startswith(self.mount + "/"):
            raise PermissionError(f"{path!r} sort du dossier de travail")
        return full

    def run_hook(self, thread_id: str, command: str, stdin: str, timeout: int) -> tuple[int | None, str]:
        """Une commande de hook (nokker/tools/hooks.py) dans l'environnement du fil : le bac à sable en docker (jamais
        l'hôte), l'hôte en backend local comme `terminal`. Charge utile sur stdin, sortie bornée. Un sous-agent
        (`<fil>/sa-<id>`) passe par le bac à sable de son fil parent : pas un container de plus pour un hook."""
        ws = self.workspace(thread_id.split("/sa-", 1)[0])
        r = ws.env.execute(command, timeout=max(1, int(timeout)), stdin_data=stdin, bounded_capture=True)
        return r.get("returncode"), _clip(str(r.get("output") or ""))

    def _terminal(self, ws: Workspace, a: dict[str, Any], out: Any) -> ToolResult:
        command = str(a.get("command") or "").strip()
        if not command:
            return out("commande vide", True)
        why = hardline_reason(command)
        if why:
            return out(f"REFUSÉ, jamais exécuté même approuvé : {why}", True)
        timeout = max(1, min(int(a.get("timeout") or 120), 600))
        r = ws.env.execute(command, timeout=timeout, bounded_capture=True)
        code = r.get("returncode")
        return out({"returncode": code, "output": _clip(str(r.get("output") or ""))}, False)

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
                return out("commande vide", True)
            why = hardline_reason(command)
            if why:
                return out(f"REFUSÉ, jamais exécuté même approuvé : {why}", True)
            port = a.get("port")
            if port is not None:
                port = int(port)
                if not 1 <= port <= 65535:
                    return out(f"port invalide : {port}", True)
            table = self.processes(ws, thread_id)
            proc = table.start(command, str(a.get("name") or ""), port)
            first = table.logs(proc.id, tail=40)  # ce que la commande a déjà dit (un échec immédiat se voit ici)
            hint = "la suite : process_logs ; l'arrêter : process_stop ; il s'arrête aussi à la fin du fil"
            if port:
                hint += f" ; le port {port} est proposé à l'utilisateur en aperçu privé (ne donne pas d'URL)"
            return out({**first, "hint": hint})
        table = self.processes(ws, thread_id)
        if name == "process_list":
            return out({"processes": [p.view() for p in table.list()]})
        proc_id = str(a.get("id") or "").strip()
        if not proc_id:
            raise KeyError("id")
        if name == "process_logs":
            return out(table.logs(proc_id, int(a.get("tail") or 200)))
        return out(table.stop(proc_id).view())


__all__ = ["CODE_TOOLS", "PROCESS_TOOLS", "CodeToolSource", "DEFAULT_SANDBOX_IMAGE", "SharedEnv", "Workspace",
           "classify_command", "hardline_reason", "reap_sandboxes"]
