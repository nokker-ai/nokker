"""nokker/tools/code.py — outils de code de Nokker Lokkal (docs/LOKKAL.md, phase L0) : lire, chercher, écrire,
patcher, lancer une commande, tenir un plan de travail, dans UN dossier de travail.

Reprise de Hermes (règle Nick 16.10 : on s'appuie sur ce qui marche, on écrit ce qu'il faut) au niveau de ses
BRIQUES, appelées en direct :
  - `tools.file_operations.ShellFileOperations` : lecture paginée et numérotée, écriture atomique avec contrôle de
    syntaxe (JSON/YAML/TOML), remplacement tolérant (`patch_replace`), lint après écriture, recherche (rg, sinon
    grep/find) — chaque opération est une commande passée à l'environnement, donc la même en local et en docker ;
  - `tools.patch_parser` (format V4A, multi-fichiers) via `ShellFileOperations.patch_v4a` ;
  - `tools.environments.docker.DockerEnvironment` (bac à sable : le dossier monté sur /workspace) et
    `tools.environments.local.LocalEnvironment` (sur l'hôte, sur demande explicite seulement) ;
  - `tools.approval_detection` : classement des commandes (dangereuses, et « hardline » jamais exécutées) ;
  - `tools.todo_tool.TodoStore` : le plan de travail.

Ce qui n'est PAS repris : les fonctions-outils de Hermes (`read_file_tool`, `terminal_tool`…). Elles lisent la
config Hermes (~/.hermes/config.yaml), tiennent leur propre registre d'environnements par tâche et leur propre
approbation des commandes ; ici ces trois rôles sont à nous (un `Workspace` par fil, nos portes, notre `Effect`).

Effets (`nokker/tools/effects.py`, PLAN-P1 §2.3 étape 5) : lire, chercher et le plan = READ ; écrire et patcher =
WRITE ; `terminal` = classé PAR APPEL (`ToolSpec.effect_of`) : commande dangereuse → DESTRUCTIVE (carte
d'approbation forte), commande de lecture pure → READ, le reste → WRITE ; déclaré DESTRUCTIVE si le classement
échoue. Une commande « hardline » (rm -rf /, mkfs, fork bomb…) est refusée par la source même après approbation.

Hermes n'est importé qu'à la création d'un dossier de travail : sans lui (CI unitaire), le module se charge, les
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


def _hermes_detection() -> Any | None:
    try:
        from tools import approval_detection  # Hermes (dépendance épinglée, docs/REPO.md)
    except Exception:  # noqa: BLE001 — absent (CI unitaire) : classement prudent
        return None
    return approval_detection


def hardline_reason(command: str) -> str | None:
    """Motif « hardline » de Hermes (jamais exécutable, même approuvée), ou None."""
    det = _hermes_detection()
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
    """Effet d'une commande `terminal`. Sans la détection de Hermes : WRITE si la commande est de lecture pure
    (on ne parie pas sur READ sans la détection), DESTRUCTIVE sinon."""
    det = _hermes_detection()
    if det is None:
        return Effect.WRITE if _read_only(command) else Effect.DESTRUCTIVE
    if det.detect_hardline_command(command)[0] or det.detect_dangerous_command(command)[0]:
        return Effect.DESTRUCTIVE
    return Effect.READ if _read_only(command) else Effect.WRITE


def reap_sandboxes(instance: str, docker: str = "docker") -> int:
    """Supprime les bacs à sable de CETTE instance de noyau (`nokker.instance=<instance>`) restés d'un arrêt sans
    `close()` (container tué, machine éteinte). À appeler au démarrage du noyau local. Seulement les siens : un autre
    noyau ou un banc peut tourner sur la même machine (vu le 23.10 : un redémarrage a tué les bacs à sable d'un banc
    en cours). Rend le nombre supprimé ; 0 si docker est absent."""
    import subprocess

    if not instance:
        return 0
    try:
        ids = subprocess.run([docker, "ps", "-aq", "--filter", f"label=nokker.instance={instance}"],
                             capture_output=True, text=True, timeout=30).stdout.split()
        if ids:
            subprocess.run([docker, "rm", "-f", *ids], capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return 0
    return len(ids)


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


# ─────────────────────────────── dossier de travail ───────────────────────────────


@dataclass
class Workspace:
    """Un environnement d'exécution Hermes ouvert sur le dossier, ses opérations de fichiers et son plan."""

    env: Any
    ops: Any
    todos: Any
    cwd: str  # le dossier tel que les outils le voient (/workspace en docker, le chemin hôte en local)
    reader: Any = None  # docker : lectures et recherches faites directement dans le dossier monté (voir local_reads)

    def close(self) -> None:
        try:
            self.env.cleanup()
        except Exception:  # noqa: BLE001 — fermeture au mieux
            pass


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
    _spaces: dict[str, Workspace] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = os.path.realpath(self.root)
        if self.backend not in ("docker", "local"):
            raise ValueError(f"backend inconnu : {self.backend!r} (docker | local)")
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

    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=n, description=d, parameters=p, effect=e, core=self.core, source="builtin:code",
                         effect_of=(lambda a: classify_command(str(a.get("command", "")))) if n == "terminal"
                         else None)
                for n, d, e, p in _SPECS]

    # ── cycle de vie ──
    def workspace(self, thread_id: str) -> Workspace:
        with self._lock:
            ws = self._spaces.get(thread_id)
            if ws is None:
                ws = self._spaces[thread_id] = self._open(thread_id)
            return ws

    def _allow_writes_in_workspace(self) -> None:
        """Hermes refuse toute écriture hors de HERMES_WRITE_SAFE_ROOT quand la variable est posée (l'image nokker
        la pose à /opt/data pour la gateway). On y ajoute le dossier de travail, tel que les outils le voient ; on
        n'y touche pas si elle est absente (aucune restriction à élargir)."""
        current = os.environ.get("HERMES_WRITE_SAFE_ROOT")
        if not current:
            return
        roots = current.split(os.pathsep)
        wanted = self.root if self.backend == "local" else WORKSPACE_MOUNT
        if wanted not in roots:
            os.environ["HERMES_WRITE_SAFE_ROOT"] = os.pathsep.join([*roots, wanted])

    def _open(self, thread_id: str) -> Workspace:
        from tools.file_operations import ShellFileOperations
        from tools.todo_tool import TodoStore

        self._allow_writes_in_workspace()
        if self.backend == "local":
            from tools.environments.local import LocalEnvironment

            env, cwd = LocalEnvironment(cwd=self._base, timeout=120), self._base
        else:
            from tools.environments import docker as hermes_docker
            from tools.environments.docker import DockerEnvironment

            # Hermes monte en lecture seule ses propres dossiers (skills, caches, identifiants de ~/.hermes) dans
            # chaque container. Lokkal n'en veut aucun : ses skills sont les siens (L3), et ces chemins, pris dans le
            # container du noyau, n'existent pas sur l'hôte (le proxy du socket les refuserait).
            hermes_docker._readonly_skill_mount_args = lambda: []
            # /tmp du bac à sable : Hermes le monte sans `exec`, donc noexec et à root. Sous l'uid de l'utilisateur
            # (run_as_host_user), un venv ou npm y échouent (recette brosim, 23.10) : exécutable et ouvert à tous
            # (sticky), et HOME=/tmp ci-dessous pour les caches de pip et npm.
            hermes_docker._BASE_SECURITY_ARGS[:] = [
                "/tmp:rw,exec,nosuid,size=1g,mode=1777" if a.startswith("/tmp:") else a
                for a in hermes_docker._BASE_SECURITY_ARGS]

            safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", thread_id)[:48] or "default"
            # Étiquette `nokker.sandbox` : le proxy du socket docker du compose n'accepte que ces containers-là.
            cwd = WORKSPACE_MOUNT + ("/" + self.subdir if self.subdir else "")
            env = DockerEnvironment(image=self.image, cwd=cwd, timeout=120, task_id=f"nokker-{safe}",
                                    volumes=[f"{self.host_root or self.root}:{WORKSPACE_MOUNT}"], network=self.network,
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
        if self.backend == "docker" and self.local_reads:
            from tools.environments.local import LocalEnvironment

            reader = ShellFileOperations(LocalEnvironment(cwd=self._base, timeout=60), cwd=self._base)
        return Workspace(env=env, ops=ShellFileOperations(env, cwd=cwd), todos=TodoStore(), cwd=cwd, reader=reader)

    def _host_path(self, ws: Workspace, path: str) -> str | None:
        """docker : le chemin hôte d'un chemin vu dans le container s'il est sous /workspace (liens symboliques résolus,
        borné au dossier monté), sinon None (lecture laissée au bac à sable)."""
        import posixpath

        p = posixpath.normpath(posixpath.join(ws.cwd, (path or ".").strip()))
        if p != WORKSPACE_MOUNT and not p.startswith(WORKSPACE_MOUNT + "/"):
            return None
        root = os.path.realpath(self.root)
        full = os.path.realpath(os.path.join(root, p[len(WORKSPACE_MOUNT):].lstrip("/")))
        if full != root and not full.startswith(root + os.sep):
            raise PermissionError(f"{path!r} sort du dossier de travail")
        return full

    def _to_container(self, text: str) -> str:
        """Le modèle ne voit jamais les chemins de l'hôte : ils redeviennent /workspace/…."""
        for root in {os.path.realpath(self.root), self.root}:
            text = text.replace(root + "/", WORKSPACE_MOUNT + "/").replace(f'"{root}"', f'"{WORKSPACE_MOUNT}"')
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

        if name not in CODE_TOOLS:
            return out(f"outil inconnu : {name}", True)
        try:
            ws = self.workspace(ctx.thread_id)
            a = arguments
            # Chemin vérifié AVANT tout appel à Hermes : un refus ne touche jamais l'environnement.
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
            from tools.todo_tool import todo_tool

            text = todo_tool(a.get("todos"), bool(a.get("merge")), store=ws.todos)
            return out(text, "error" in json.loads(text))
        except PermissionError as e:
            return out(f"REFUSÉ : {e}", True)
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
            # En-têtes réécrits en chemins absolus (vérifiés en local) : Hermes contrôle chaque écriture contre
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


__all__ = ["CODE_TOOLS", "CodeToolSource", "DEFAULT_SANDBOX_IMAGE", "Workspace", "classify_command",
           "hardline_reason", "reap_sandboxes"]
