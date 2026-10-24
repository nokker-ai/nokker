"""nokker/tools/delegate.py — sous-agents de Nokker Lokkal (docs/LOKKAL.md §7.4 points 2 et 6, docs/LOKKAL-CLI.md §3.2).

L'outil `delegate(tasks, isolate)` confie des sous-tâches à des enfants qui travaillent en parallèle, chacun dans
son propre worktree git, et rend un résumé par enfant.

Un enfant = UN SECOND `Kernel.run_turn` dans le même processus, sur le fil `<fil parent>/sa-<id>`, canal `subagent`,
même `sender` que le parent. Il passe par nos portes (effets, approbations, juge) comme n'importe quel tour : rien
n'est contourné. Son `Registry` est celui du parent FILTRÉ (sans `delegate` au-delà de la profondeur max, sans
`clarify`, `escalate_to_human`, `send_*`/`*_send`, `session_send`, `agent_*`, outils d'écriture mémoire, installation de
skills ni points de retour) et son `CodeToolSource` est ouvert sur le worktree (`CodeToolSource.scoped`).

Effet de l'outil (`ToolSpec.effect_of`). L'enfant dispose de `terminal`, qui n'est pas en lecture seule : ce que
l'enfant fera ne se juge pas appel par appel au moment de déléguer. Donc
  - `isolate=true` ET le dossier de travail est un dépôt git (présence de `.git`) : WRITE. L'enfant ne peut toucher
    qu'un worktree jetable sur une branche à lui, et ses propres appels d'outils repassent par les portes ;
  - sinon (`isolate=false`, ou pas de dépôt donc pas d'isolation possible) : DESTRUCTIVE, car l'enfant agit dans le
    dossier de travail réel de l'utilisateur.

Git dans le bac à sable, jamais côté noyau (LOKKAL §7.4.6) : toutes les commandes git passent par un `run(cmd) ->
(rc, sortie)` injectable, qui est `ws.env.execute` du fil parent (donc dans le container en docker). Chaque commande
git du noyau force `-c core.hooksPath=/dev/null` : les hooks du dépôt de l'utilisateur ne tournent pas. Les worktrees
sont sous `<racine>/.nokker/worktrees/<id>` (`/workspace/.nokker/worktrees/<id>` dans le bac), branche
`nokker/sa-<id>`. Avec git >= 2.48 on crée avec `--relative-paths` ; sinon (git 2.47.3 dans l'image actuelle) on crée
sans, puis on réécrit le fichier `.git` du worktree et le `gitdir` de l'administration en chemins relatifs, de sorte
que le worktree reste valide des deux côtés du montage (hôte et container). `.nokker/` est ajouté à
`.git/info/exclude` (jamais au `.gitignore` versionné). La logique est celle de `subagent_worktree.py` de la brique
amont (preuve avant suppression, note de contexte), réécrite en stdlib.

Fin d'un enfant : `rev-list --count base..HEAD` et `status --porcelain` ; le worktree n'est supprimé qu'avec la
preuve de 0 commit ET d'un arbre propre (les deux sondes ont réussi). Sinon il est conservé et le résultat le dit
(`worktree.kept = true`, `commits`, `dirty`) : au parent de relire la branche et de la fusionner (`git merge --no-ff`
via son `terminal`).

Exécution : au plus `max_children` enfants à la fois (le reste attend), threads DAEMON (un `ThreadPoolExecutor` ne
l'est pas : il retiendrait la fin du processus), délai d'inactivité (`idle_timeout_s` sans appel d'outil ni retour
d'outil ; un enfant interrompu est rendu `interrupted`, ses outils refusent tout et son thread s'éteint à son
prochain retour du modèle), profondeur max (`max_depth`, 1 : un enfant ne délègue pas). Un `delegate` bloque le tour
du parent jusqu'au retour des enfants.

Événements (`emit(name, payload)` injectable, jamais bloquant) : `subagent.started {thread_id, parent_thread_id,
parent_turn, child_id, child_thread_id, goal}`, `subagent.progress {parent_thread_id, child_id, tool, ms}` (relayé
depuis `TurnObserver.tool_result` du Kernel enfant), `subagent.finished {parent_thread_id, child_id, status, commits,
dirty, kept}` : `parent_thread_id` permet au client de les afficher aussi dans le fil parent.

Approbations : l'enfant garde nos portes. Si l'une exige une carte, le Kernel enfant la pose sur le fil enfant
(`Approval.thread_id` = `<parent>/sa-<id>`) : le client la reconnaît au préfixe `<fil parent>/sa-` et l'affiche sous
le nom de l'enfant. L'enfant s'arrête alors, le résultat porte `status: interrupted`, `exit_reason:
approval_pending` ; le parent peut relancer une tâche une fois la carte décidée.

Schéma de sortie : `output_schema` (JSON Schema) est ajouté au but de l'enfant ; sa réponse finale est validée, avec
UNE relance au plus (message d'erreur renvoyé sur le même fil) avant `failed / output_schema_invalid`. Les briques
`delegation_output_schema` du paquet amont sont importées si présentes, repli maison sinon.
"""
from __future__ import annotations

import contextvars
import fnmatch
import json
import logging
import os
import posixpath
import re
import shlex
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult, TurnInput

logger = logging.getLogger("nokker.tools.delegate")

SOURCE = "builtin:delegate"
WORKTREES_DIR = ".nokker/worktrees"
BRANCH_PREFIX = "nokker/sa-"
CHILD_CHANNEL = "subagent"
MAX_TASKS = 10
NO_HOOKS = "-c core.hooksPath=/dev/null"
Run = Callable[[str], "tuple[int, str]"]

# Outils que l'enfant n'a jamais (motifs fnmatch). `delegate` est géré à part (profondeur).
DENIED_FOR_CHILD = ("clarify", "escalate_to_human", "send_*", "*_send", "session_send", "agent_*",
                    "memory_save", "memory_write", "memory_propose", "memory_add", "memory_update", "memory_delete",
                    "memory_forget", "memory_remember", "memory_set", "skill_install", "skill_remove",
                    "checkpoint_*")
REVIEWER_DENIED = ("write_file", "patch")

_SPECS_PARAMS: dict[str, Any] = {
    "type": "object", "required": ["tasks"], "properties": {
        "tasks": {"type": "array", "minItems": 1, "maxItems": MAX_TASKS, "items": {
            "type": "object", "required": ["goal"], "properties": {
                "goal": {"type": "string", "description": "ce que l'enfant doit accomplir, complet et autonome"},
                "context": {"type": "string", "description": "ce qu'il doit savoir (fichiers, contraintes)"},
                "role": {"type": "string", "enum": ["worker", "reviewer"], "default": "worker",
                         "description": "reviewer : relit et teste, ne modifie aucun fichier"},
                "output_schema": {"type": "object", "description": "JSON Schema de sa réponse finale"}}}},
        "isolate": {"type": "boolean", "default": True,
                    "description": "true : chaque enfant travaille dans son worktree git (branche nokker/sa-<id>)"}}}


# ─────────────────────────────── schéma de sortie ───────────────────────────────


def _schema_module() -> Any | None:
    try:
        from tools import delegation_output_schema as m  # brique amont, si présente
        return m
    except Exception:  # noqa: BLE001 — absente (CI unitaire) : repli maison
        return None


def coerce_schema(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    if raw in (None, "", {}):
        return None, None
    m = _schema_module()
    if m is not None:
        try:
            return m.coerce_output_schema(raw)
        except Exception:  # noqa: BLE001
            pass
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None, "output_schema n'est pas un JSON valide"
    if not isinstance(raw, dict):
        return None, "output_schema doit être un objet JSON Schema"
    return raw, None


def append_contract(context: str, schema: dict[str, Any]) -> str:
    m = _schema_module()
    if m is not None:
        try:
            return m.append_output_contract(context, schema)
        except Exception:  # noqa: BLE001
            pass
    block = ("[Contrat de sortie] Ta réponse finale doit être UN objet JSON conforme à ce schéma, sans autre texte :\n"
             + json.dumps(schema, ensure_ascii=False))
    return f"{context}\n\n{block}" if context else block


def _json_candidate(text: str) -> str:
    t = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if fence:
        t = fence.group(1).strip()
    if not t.startswith(("{", "[")):
        i, j = t.find("{"), t.rfind("}")
        if i != -1 and j > i:
            t = t[i:j + 1]
    return t


_TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "array": list, "object": dict}


def _check(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    typ = schema.get("type")
    if isinstance(typ, str) and typ in _TYPES:
        ok = isinstance(value, _TYPES[typ]) and not (typ in ("number", "integer") and isinstance(value, bool))
        if not ok:
            errors.append(f"{path or '$'} : type {typ} attendu")
            return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path or '$'} : valeur hors de {schema['enum']}")
    if isinstance(value, dict):
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path or '$'} : champ requis « {key} » absent")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value and isinstance(sub, dict):
                _check(value[key], sub, f"{path}.{key}", errors)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            _check(item, schema["items"], f"{path}[{i}]", errors)


def validate_reply(text: str, schema: dict[str, Any]) -> tuple[bool, list[str]]:
    m = _schema_module()
    if m is not None:
        try:
            ok, errors = m.validate_output(text, schema)
            return bool(ok), [str(e) for e in errors]
        except Exception:  # noqa: BLE001
            pass
    try:
        value = json.loads(_json_candidate(text))
    except ValueError:
        return False, ["la réponse n'est pas un JSON valide"]
    errors: list[str] = []
    _check(value, schema, "", errors)
    return not errors, errors


def retry_message(errors: list[str]) -> str:
    m = _schema_module()
    if m is not None:
        try:
            return m.build_retry_message(errors)
        except Exception:  # noqa: BLE001
            pass
    return ("Ta réponse finale a été refusée par le contrat de sortie : " + " ; ".join(errors[:10])
            + ". Réponds de nouveau par UN objet JSON conforme au schéma, sans autre texte.")


# ─────────────────────────────── git dans le bac à sable ───────────────────────────────


def _git(cwd: str, *args: str) -> str:
    """Commande git, hooks du dépôt désactivés, sans invite."""
    return f"GIT_TERMINAL_PROMPT=0 git {NO_HOOKS} -C {shlex.quote(cwd)} " + " ".join(shlex.quote(a) for a in args)


def resolve_repo_root(run: Run, cwd: str) -> str | None:
    rc, out = run(_git(cwd, "rev-parse", "--show-toplevel"))
    top = out.strip().splitlines()[-1].strip() if rc == 0 and out.strip() else ""
    return top or None


def supports_relative_paths(run: Run) -> bool:
    """git >= 2.48 sait `worktree add --relative-paths`."""
    rc, out = run("git --version")
    m = re.search(r"(\d+)\.(\d+)", out or "")
    return rc == 0 and bool(m) and (int(m.group(1)), int(m.group(2))) >= (2, 48)


def ensure_excluded(run: Run, repo: str) -> bool:
    """Ajoute `.nokker/` à `.git/info/exclude` du dépôt (jamais au `.gitignore` versionné)."""
    rc, out = run(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    gdir = out.strip().splitlines()[-1].strip() if rc == 0 and out.strip() else ""
    if not gdir:
        return False
    f = shlex.quote(posixpath.join(gdir, "info", "exclude"))
    rc, _ = run(f"mkdir -p {shlex.quote(posixpath.join(gdir, 'info'))} && "
                f"{{ grep -qxF '.nokker/' {f} 2>/dev/null || printf '.nokker/\\n' >> {f}; }}")
    return rc == 0


def create_worktree(run: Run, cwd: str, child_id: str, relative_paths: bool | None = None) -> dict[str, Any] | None:
    """Worktree d'un enfant sous `<cwd>/.nokker/worktrees/<id>` (chemins tels que le bac à sable les voit).
    None = pas d'isolation possible (pas de dépôt, pas de commit, échec de git)."""
    repo = resolve_repo_root(run, cwd)
    if not repo:
        return None
    rc, out = run(_git(repo, "rev-parse", "HEAD"))
    base = out.strip().splitlines()[-1].strip() if rc == 0 and out.strip() else ""
    if not base:
        return None
    if not ensure_excluded(run, repo):
        logger.warning("sous-agent : .git/info/exclude non mis à jour")
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", child_id)
    path = posixpath.join(cwd, WORKTREES_DIR, name)
    branch = BRANCH_PREFIX + name
    rel = supports_relative_paths(run) if relative_paths is None else relative_paths
    run(f"mkdir -p {shlex.quote(posixpath.dirname(path))}")
    args = ["worktree", "add"] + (["--relative-paths"] if rel else []) + ["-b", branch, path, base]
    rc, out = run(_git(repo, *args))
    if rc != 0:
        logger.warning("sous-agent : git worktree add a échoué : %s", out.strip()[:300])
        return None
    if not rel and not _relativize(run, repo, path):
        # Un worktree aux chemins absolus serait cassé de l'autre côté du montage : on n'en garde pas.
        run(_git(repo, "worktree", "remove", "--force", path))
        run(_git(repo, "branch", "-D", branch))
        return None
    return {"path": path, "branch": branch, "repo_root": repo, "base_commit": base, "relative_paths": True,
            "subdir": posixpath.relpath(path, cwd)}


def _relativize(run: Run, repo: str, wt: str) -> bool:
    """Repli pour git < 2.48 : réécrit `<wt>/.git` (« gitdir: … ») et `<admin>/gitdir` en chemins relatifs."""
    rc, out = run(_git(wt, "rev-parse", "--absolute-git-dir"))
    admin = out.strip().splitlines()[-1].strip() if rc == 0 and out.strip() else ""
    if not admin:
        return False
    dotgit = posixpath.join(wt, ".git")
    to_admin = posixpath.relpath(admin, wt)
    back = posixpath.relpath(dotgit, admin)
    rc, _ = run(f"printf 'gitdir: %s\\n' {shlex.quote(to_admin)} > {shlex.quote(dotgit)} && "
                f"printf '%s\\n' {shlex.quote(back)} > {shlex.quote(posixpath.join(admin, 'gitdir'))}")
    if rc != 0:
        return False
    rc, _ = run(_git(wt, "rev-parse", "--git-dir"))  # le worktree doit rester lisible
    return rc == 0


def finalize_worktree(run: Run, info: dict[str, Any], prune: bool = True) -> dict[str, Any]:
    """Inspecte le worktree d'un enfant et ne le supprime qu'avec la PREUVE de 0 commit et d'un arbre propre."""
    path, branch, repo, base = info["path"], info["branch"], info["repo_root"], info.get("base_commit", "")
    payload: dict[str, Any] = {"path": path, "branch": branch, "commits": 0, "dirty": False, "kept": True}
    failed: list[str] = []
    rc, out = run(_git(path, "rev-list", "--count", f"{base}..HEAD")) if base else (1, "base inconnue")
    try:
        payload["commits"] = int(out.strip().splitlines()[-1]) if rc == 0 else 0
        if rc != 0:
            failed.append("commits")
    except (ValueError, IndexError):
        failed.append("commits")
    rc, out = run(_git(path, "status", "--porcelain"))
    if rc == 0:
        payload["dirty"] = bool(out.strip())
    else:
        failed.append("dirty")
    if failed:
        payload["inspection_failed"] = True
        payload["note"] = (f"inspection git en échec ({', '.join(failed)} inconnus, non prouvés nuls) : worktree et "
                           f"branche conservés, à relire avant de conclure que l'enfant n'a rien fait.")
        return payload
    if prune and payload["commits"] == 0 and not payload["dirty"]:
        rc, _ = run(_git(repo, "worktree", "remove", "--force", path))
        if rc != 0:  # preuve acquise : repli par suppression directe puis élagage
            run(f"rm -rf {shlex.quote(path)}")
            run(_git(repo, "worktree", "prune"))
        run(_git(repo, "branch", "-D", branch))
        rc, _ = run(f"test -e {shlex.quote(path)}")
        payload["kept"] = rc == 0
    return payload


def worktree_context_note(info: dict[str, Any]) -> str:
    return ("\n\n[ISOLATION WORKTREE] Tu travailles dans un worktree git isolé : "
            f"{info['path']}\nTa branche : {info['branch']}\n"
            "Toutes tes modifications et commandes restent dans ce dossier (ton terminal y démarre). Ne va PAS dans le "
            "dépôt principal. Commite ton travail sur ta branche quand tu as fini ; le parent relira et fusionnera. "
            "Si tu ne commites rien et laisses l'arbre propre, le worktree est supprimé automatiquement.")


# ─────────────────────────────── enfants ───────────────────────────────


@dataclass
class _Child:
    index: int
    id: str
    thread_id: str
    task: dict[str, Any]
    schema: dict[str, Any] | None
    info: dict[str, Any] | None = None
    note: str = ""
    last_activity: float = 0.0
    started: bool = False
    parent_thread_id: str = ""
    code: Any = None  # source de code du fil parent (résolue par for_thread)
    cancelled: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] = field(default_factory=dict)
    tokens: int = 0
    cost: float | None = None

    def touch(self) -> None:
        self.last_activity = time.monotonic()


class _ChildObserver:
    """`TurnObserver` du Kernel enfant : relaie la progression et nourrit l'observateur du parent (BubbleHub) sans le
    remplacer. Toute exception est avalée ici et par la boucle."""

    def __init__(self, child: _Child, emit: Callable[[str, dict[str, Any]], None], parent: Any | None) -> None:
        self._child, self._emit, self._parent = child, emit, parent

    def _fwd(self, method: str, *args: Any) -> Any:
        if self._parent is None:
            return None
        try:
            return getattr(self._parent, method)(*args)
        except Exception:  # noqa: BLE001
            logger.debug("observateur parent %s a levé", method, exc_info=True)
            return None

    def context_for(self, thread_id: str) -> list[str]:
        return self._fwd("context_for", thread_id) or []

    def turn_started(self, turn: Any) -> None:
        self._child.touch()
        self._fwd("turn_started", turn)

    def tool_call(self, turn: Any, call: Any) -> None:
        self._child.touch()
        self._fwd("tool_call", turn, call)

    def tool_result(self, turn: Any, call: Any, result: Any, ms: int) -> None:
        self._child.touch()
        self._emit("subagent.progress", {"parent_thread_id": self._child.parent_thread_id,
                                         "child_id": self._child.id, "tool": getattr(call, "name", ""), "ms": ms})
        self._fwd("tool_result", turn, call, result, ms)

    def tool_executed(self, turn: Any, call: Any, spec: Any, result: Any) -> str | None:
        return self._fwd("tool_executed", turn, call, spec, result)

    def turn_done(self, turn: Any) -> None:
        self._fwd("turn_done", turn)


class _ChildSource:
    """Source du parent vue par l'enfant : sans les outils interdits, et muette une fois l'enfant interrompu.
    Pas de `close` : les ressources appartiennent au parent."""

    def __init__(self, inner: Any, child: _Child, denied: tuple[str, ...]) -> None:
        self._inner, self._child, self._denied = inner, child, denied

    def _is_denied(self, name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, p) for p in self._denied)

    def specs(self) -> list[ToolSpec]:
        return [s for s in self._inner.specs() if not self._is_denied(s.name)]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if self._child.cancelled:
            return ToolResult(call_id="", name=name, content="INTERROMPU : le sous-agent a dépassé son délai "
                              "d'inactivité, aucun outil n'est plus exécuté.", is_error=True)
        if self._is_denied(name):
            return ToolResult(call_id="", name=name, content=f"outil interdit à un sous-agent : {name}", is_error=True)
        self._child.touch()
        return self._inner.call(name, arguments, ctx)

    def turn_done(self, thread_id: str) -> None:
        hook = getattr(self._inner, "turn_done", None)
        if callable(hook):
            hook(thread_id)


class DelegateToolSource:
    """`ToolSource` de l'outil `delegate`.

    `kernel_factory(registry, thread_id, observer) -> Kernel` est fourni par le noyau : il construit un Kernel enfant
    (mêmes portes, approbations, juge, fournisseurs que le parent) autour du `registry` filtré et de l'`observer`.
    L'enfant DOIT recopier du parent `max_tokens` (plafond de sortie : 16384 en session de code, avec reprise sur
    `finish_reason` « length ») et `max_calls`, sinon il retombe sur les défauts du Kernel et ses réponses sont coupées :

        def child(registry, thread_id, observer):
            return dataclasses.replace(parent_kernel, tools=registry, observer=observer)
            # replace() garde max_tokens, max_calls, approvals, gates, judge… du parent

    `code_source` est le `CodeToolSource` du parent, ou une source par fil (`SessionCodeTools`) dont `for_thread(tid)`
    donne celle du fil (un fil `<fil>/sa-<id>` a celle de son parent) ; None : pas de dossier de travail, pas de worktree.
    `bind(registry)` donne le registre du parent, dont les sources sont filtrées pour l'enfant.
    `parent_observer` : le BubbleHub du parent, à envelopper (pas remplacer) si l'enfant doit le nourrir."""

    core = False

    def __init__(self, kernel_factory: Callable[..., Any], code_source: Any | None, max_children: int = 3,
                 max_depth: int = 1, idle_timeout_s: float = 600, emit: Callable[[str, dict[str, Any]], None] | None
                 = None, parent_observer: Any | None = None, poll_s: float = 0.05) -> None:
        self.kernel_factory, self.code_source = kernel_factory, code_source
        self.max_children, self.max_depth, self.idle_timeout_s = max(1, max_children), max_depth, idle_timeout_s
        self._emit_fn, self.parent_observer, self.poll_s = emit, parent_observer, poll_s
        self._registry: Any = None
        self._slots = threading.BoundedSemaphore(self.max_children)

    def bind(self, registry: Any) -> None:
        self._registry = registry

    # ── ToolSource ──
    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(
            name="delegate", core=False, source=SOURCE, effect=Effect.WRITE, effect_of=self._effect_of,
            description="Confie des sous-tâches à des sous-agents qui travaillent en parallèle, chacun dans son "
                        "worktree git (branche nokker/sa-<id>) ; rend un résumé par enfant. Bloque jusqu'à leur retour.",
            parameters=_SPECS_PARAMS)]

    def _code_for(self, thread_id: str) -> Any | None:
        """La source de code du fil : `for_thread` si la source est par fil (None = pas de session de code)."""
        if self.code_source is None:
            return None
        for_thread = getattr(self.code_source, "for_thread", None)
        return for_thread(thread_id) if callable(for_thread) else self.code_source

    def _roots(self) -> list[str]:
        cs = self.code_source
        if cs is None:
            return []
        if hasattr(cs, "root"):
            return [cs.root]
        sessions = getattr(cs, "sessions", None)  # source par fil : les racines de toutes les sessions ouvertes
        return list(sessions().values()) if callable(sessions) else []

    def _effect_of(self, args: dict[str, Any]) -> Effect:
        """WRITE seulement si l'enfant est confiné à un worktree jetable ; DESTRUCTIVE sinon (voir le docstring)."""
        if args.get("isolate", True) is False or self.code_source is None:
            return Effect.DESTRUCTIVE
        roots = self._roots()  # l'effet ne voit pas le fil : avec une source par fil, TOUTES les racines doivent être git
        return (Effect.WRITE if roots and all(os.path.exists(os.path.join(r, ".git")) for r in roots)
                else Effect.DESTRUCTIVE)

    def emit(self, name: str, payload: dict[str, Any]) -> None:
        if self._emit_fn is None:
            return
        try:
            self._emit_fn(name, payload)
        except Exception:  # noqa: BLE001 — un abonné en panne ne casse jamais un tour
            logger.debug("emit %s a levé", name, exc_info=True)

    @staticmethod
    def depth_of(thread_id: str) -> int:
        return thread_id.count("/sa-")

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def out(payload: Any, error: bool = False) -> ToolResult:
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            return ToolResult(call_id="", name=name, content=text, is_error=error)

        if name != "delegate":
            return out(f"outil inconnu : {name}", True)
        if self._registry is None or not hasattr(self._registry, "sources"):
            return out("delegate indisponible : registre du parent non lié (bind)", True)
        if self.depth_of(ctx.thread_id) >= self.max_depth:
            return out(f"profondeur max atteinte ({self.max_depth}) : un sous-agent ne délègue pas", True)
        tasks = arguments.get("tasks")
        if not isinstance(tasks, list) or not tasks or len(tasks) > MAX_TASKS:
            return out(f"tasks : 1 à {MAX_TASKS} tâches requises", True)
        children: list[_Child] = []
        code = self._code_for(ctx.thread_id)
        for i, t in enumerate(tasks):
            if not isinstance(t, dict) or not str(t.get("goal") or "").strip():
                return out(f"tasks[{i}] : goal requis", True)
            if t.get("role", "worker") not in ("worker", "reviewer"):
                return out(f"tasks[{i}] : role doit être worker ou reviewer", True)
            schema, err = coerce_schema(t.get("output_schema"))
            if err:
                return out(f"tasks[{i}] : {err}", True)
            cid = uuid.uuid4().hex[:8]
            children.append(_Child(index=i, id=cid, thread_id=f"{ctx.thread_id}/sa-{cid}", task=t, schema=schema,
                                   parent_thread_id=ctx.thread_id, code=code))

        run, cwd = self._sandbox_run(ctx.thread_id, code)
        isolate = arguments.get("isolate", True) is not False
        for ch in children:  # worktrees créés l'un après l'autre : un seul git à la fois sur le dépôt
            if not isolate:
                ch.note = "isolate=false : l'enfant travaille directement dans le dossier de travail."
            elif run is None:
                ch.note = "pas de dossier de travail : aucune isolation."
            else:
                try:
                    ch.info = create_worktree(run, cwd, ch.id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("worktree de %s : %s", ch.id, e)
                if ch.info is None:
                    ch.note = ("pas de dépôt git (ou aucun commit) : isolation impossible, l'enfant travaille dans "
                               "le dossier de travail.")
        for ch in children:
            self.emit("subagent.started", {"thread_id": ctx.thread_id, "parent_thread_id": ctx.thread_id,
                                           "parent_turn": ctx.turn_index, "child_id": ch.id, "child_thread_id": ch.thread_id,
                                           "goal": str(ch.task["goal"])[:300]})
            ch.touch()
            cx = contextvars.copy_context()
            threading.Thread(target=cx.run, args=(self._worker, ch, ctx), daemon=True,
                             name=f"nokker-sa-{ch.id}").start()

        self._wait(children)
        results = [self._finish(ch, run) for ch in children]
        return out(results)

    # ── attente (délai d'inactivité) ──
    def _wait(self, children: list[_Child]) -> None:
        pending = list(children)
        while pending:
            now = time.monotonic()
            for ch in list(pending):
                if ch.done.is_set():
                    pending.remove(ch)
                elif ch.started and now - ch.last_activity > self.idle_timeout_s:
                    ch.cancelled = True
                    ch.result = {"status": "interrupted", "exit_reason": "idle_timeout",
                                 "summary": f"interrompu : aucune activité depuis {self.idle_timeout_s:g} s"}
                    pending.remove(ch)
            if pending:
                pending[0].done.wait(self.poll_s)

    # ── un enfant ──
    def _worker(self, ch: _Child, ctx: ToolContext) -> None:
        self._slots.acquire()
        try:
            if ch.cancelled:
                return
            ch.started = True
            ch.touch()
            res = self._run_child(ch, ctx)
            if not ch.cancelled:
                ch.result = res
        except Exception as e:  # noqa: BLE001
            logger.exception("sous-agent %s", ch.id)
            if not ch.cancelled:
                ch.result = {"status": "failed", "exit_reason": "exception", "summary": f"{type(e).__name__}: {e}"}
        finally:
            self._slots.release()
            ch.done.set()

    def _child_registry(self, ch: _Child, scoped: Any | None, parent_depth: int) -> Any:
        from nokker.tools.registry import Registry

        denied = DENIED_FOR_CHILD + (REVIEWER_DENIED if ch.task.get("role") == "reviewer" else ())
        sources: list[Any] = []
        for src in self._registry.sources:
            if src is self:
                if parent_depth + 1 < self.max_depth:  # un enfant peut déléguer seulement sous la profondeur max
                    sources.append(src)
                continue
            if self.code_source is not None and (src is self.code_source
                                                 or getattr(src, "inner", None) is self.code_source):
                continue  # remplacé par la source ouverte sur le worktree
            sources.append(_ChildSource(src, ch, denied))
        if self.code_source is not None:
            sources.append(_ChildSource(scoped if scoped is not None else self.code_source, ch, denied))
        return Registry(sources=sources)

    def _prompt(self, ch: _Child) -> str:
        t = ch.task
        text = str(t["goal"]).strip()
        ctx = str(t.get("context") or "").strip()
        if ctx:
            text += "\n\nContexte :\n" + ctx
        if t.get("role") == "reviewer":
            text += ("\n\nRôle : relecteur. Tu lis, lances les tests et rapportes ; tu ne modifies aucun fichier.")
        if ch.schema is not None:
            text = append_contract(text, ch.schema)
        if ch.info is not None:
            text += worktree_context_note(ch.info)
        return text

    def _run_child(self, ch: _Child, ctx: ToolContext) -> dict[str, Any]:
        scoped = None
        if ch.info is not None and ch.code is not None:
            scoped = ch.code.scoped(ch.info["subdir"])
        try:
            registry = self._child_registry(ch, scoped, self.depth_of(ctx.thread_id))
            observer = _ChildObserver(ch, self.emit, self.parent_observer)
            kernel = self.kernel_factory(registry, ch.thread_id, observer)
            turn = kernel.run_turn(TurnInput(thread_id=ch.thread_id, user_text=self._prompt(ch),
                                             channel=CHILD_CHANNEL, sender=ctx.sender))
            self._account(ch, turn)
            if ch.schema is not None and not turn.error and not turn.pending_approvals:
                ok, errors = validate_reply(turn.reply, ch.schema)
                if not ok:
                    turn = kernel.run_turn(TurnInput(thread_id=ch.thread_id, user_text=retry_message(errors),
                                                     channel=CHILD_CHANNEL, sender=ctx.sender))
                    self._account(ch, turn)
                    ok, errors = validate_reply(turn.reply, ch.schema)
                    if not ok and not turn.error and not turn.pending_approvals:
                        return {"status": "failed", "exit_reason": "output_schema_invalid",
                                "summary": turn.reply, "errors": errors[:10]}
            return self._status(ch, turn)
        finally:
            if scoped is not None:
                scoped.close()

    @staticmethod
    def _status(ch: _Child, turn: Any) -> dict[str, Any]:
        if turn.error:
            return {"status": "failed", "exit_reason": "error", "summary": turn.reply or str(turn.error),
                    "error": str(turn.error)}
        if turn.pending_approvals:
            names = ", ".join(c.name for c in turn.pending_approvals)
            return {"status": "interrupted", "exit_reason": "approval_pending",
                    "summary": (turn.reply or "") + f"\n[carte d'approbation en attente sur le fil {ch.thread_id} : "
                                                    f"{names}]"}
        if getattr(turn, "escalated", False):
            return {"status": "interrupted", "exit_reason": "escalated", "summary": turn.reply}
        return {"status": "completed", "exit_reason": "completed", "summary": turn.reply}

    @staticmethod
    def _account(ch: _Child, turn: Any) -> None:
        for call in getattr(turn, "calls", None) or []:
            u = getattr(call, "usage", None)
            if u is not None:
                ch.tokens += int(getattr(u, "prompt", 0) or 0) + int(getattr(u, "completion", 0) or 0)
            chf = getattr(call, "chf", None)
            if chf is not None:
                ch.cost = (ch.cost or 0.0) + float(chf)

    # ── résultat ──
    def _finish(self, ch: _Child, run: Run | None) -> dict[str, Any]:
        res = {"task_index": ch.index, **(ch.result or {"status": "failed", "exit_reason": "no_result",
                                           "summary": ""})}
        res["child_id"] = ch.id
        res["thread_id"] = ch.thread_id
        commits, dirty, kept = 0, False, False
        if ch.info is not None and run is not None:
            try:
                # Un enfant interrompu peut encore écrire : on inspecte, on ne supprime pas.
                wt = finalize_worktree(run, ch.info, prune=not ch.cancelled)
            except Exception as e:  # noqa: BLE001
                wt = {"path": ch.info["path"], "branch": ch.info["branch"], "commits": 0, "dirty": False,
                      "kept": True, "inspection_failed": True, "note": f"inspection en échec : {e}"}
            res["worktree"] = wt
            commits, dirty, kept = wt["commits"], wt["dirty"], wt["kept"]
        elif ch.note:
            res["isolated"] = False
            res["note"] = ch.note
        if ch.tokens:
            res["tokens"] = ch.tokens
        if ch.cost is not None:
            res["cost"] = ch.cost
        self.emit("subagent.finished", {"parent_thread_id": ch.parent_thread_id, "child_id": ch.id,
                                        "status": res["status"], "commits": commits,
                                        "dirty": dirty, "kept": kept})
        return res

    def _sandbox_run(self, thread_id: str, code: Any | None) -> tuple[Run | None, str]:
        """`run(cmd) -> (rc, sortie)` dans l'environnement du fil parent (bac à sable en docker)."""
        if code is None:
            return None, ""
        ws = code.workspace(thread_id)

        def run(cmd: str) -> tuple[int, str]:
            r = ws.env.execute(cmd, timeout=60)
            rc = r.get("returncode")
            return (1 if rc is None else int(rc)), str(r.get("output") or "")

        return run, ws.cwd


__all__ = ["DENIED_FOR_CHILD", "DelegateToolSource", "WORKTREES_DIR", "create_worktree", "ensure_excluded",
           "finalize_worktree", "resolve_repo_root", "supports_relative_paths", "worktree_context_note"]
