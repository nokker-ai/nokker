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
approval_pending` ; la carte décidée, l'enfant reprend AVEC SON noyau (voir « Carte décidée après la fin »).

Schéma de sortie : `output_schema` (JSON Schema) est ajouté au but de l'enfant ; sa réponse finale est validée, avec
UNE relance au plus (message d'erreur renvoyé sur le même fil) avant `failed / output_schema_invalid`. Les briques
`delegation_output_schema` du paquet amont sont importées si présentes, repli maison sinon.

Arrière-plan (`background=true`, permis par `DelegateToolSource(background=True)` — serveur local ; jamais dans
`nokker code`, dont le processus s'arrête avec le tour) : mêmes validations, worktrees, portes, filtres, plafond
`max_children` partagé avec le premier plan ; l'appel rend TOUT DE SUITE `{background: true, children: [{child_id,
thread_id, task_index, goal, status, worktree?}]}` et un fil de garde attend les enfants (délai d'inactivité compris),
finalise leurs worktrees et range le résultat. Au plus `max_background` enfants de fond non terminés à la fois (refus
au-delà). Chaque enfant a son fil `<parent>/sa-<id>`, donc son verrou de tour : le fil parent reste libre pour les tours
suivants. Lecture : `delegate_status(ids?, wait_s?)` (READ, jamais resservi par le cache du tour) rend l'état ou le
résultat complet, relisible ; `note(fil, ligne)` prévient le parent, ligne ajoutée au contexte de son PROCHAIN tour.
Annulation : `delegate_cancel(ids?)` (READ comme `process_stop` : il n'arrête que ce que le fil a lancé), ou
`cancel_children(fil)` appelé par l'annulation du fil (`POST /v1/threads/{id}/cancel`) — enfants de fond ET de premier
plan du fil, ou un seul enfant si le fil est `<parent>/sa-<id>` ; l'enfant passe `interrupted / cancelled`, son noyau
reçoit `cancel(<fil enfant>)`, ses outils refusent tout, son worktree est inspecté et conservé. Événements :
`subagent.started|finished` portent `background: true` (et `exit_reason` à la fin) pour un enfant de fond.

Carte décidée après la fin de l'enfant (premier plan ET fond) : un enfant qui s'arrête sur `approval_pending` est
« en attente » — son noyau, son registre filtré et sa source de worktree restent ouverts, son worktree n'est pas
supprimé. `kernel_for_card(<fil enfant>)` le rend à l'exécuteur d'approbation (nokker/approvals/executor.py), qui lance
l'outil puis le tour de suite avec CE noyau, jamais celui du parent. À la fin de ce tour (`turn_done` de son
observateur) : nouvelle carte → toujours en attente ; sinon l'enfant est repris (source fermée, worktree finalisé avec
la preuve habituelle, `subagent.finished` + `resumed: true`, ligne au parent, résultat de fond mis à jour). Annulation
du fil parent, `delegate_cancel` ou éviction (au plus `MAX_PARKED`) : plus de noyau, la carte sera refusée.
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
MAX_STATUS_WAIT_S = 120.0  # `delegate_status(wait_s)` : attente bornée dans le tour du parent
KEEP_FINISHED = 50
MAX_PARKED = 100  # enfants en attente d'une carte, sur la machine (le plus ancien est abandonné au-delà)  # résultats d'enfants de fond gardés par fil parent (les plus anciens partent)
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
_BACKGROUND_PARAM = {"type": "boolean", "default": False,
                     "description": "true : rend la main tout de suite avec les ids des enfants ; relire avec "
                                    "delegate_status, arrêter avec delegate_cancel"}
_STATUS_PARAMS: dict[str, Any] = {"type": "object", "properties": {
    "ids": {"type": "array", "items": {"type": "string"}, "description": "child_id à lire (défaut : tous)"},
    "wait_s": {"type": "number", "minimum": 0, "maximum": MAX_STATUS_WAIT_S,
               "description": "attendre au plus N s que ces enfants aient fini (défaut 0)"}}}
_CANCEL_PARAMS: dict[str, Any] = {"type": "object", "properties": {
    "ids": {"type": "array", "items": {"type": "string"}, "description": "child_id à arrêter (défaut : tous)"}}}


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
    background: bool = False
    kernel: Any = None  # le noyau de l'enfant (son `cancel` vise SON fil)
    last_tool: str | None = None
    created: float = field(default_factory=time.monotonic)
    final: dict[str, Any] | None = None  # résultat finalisé (worktree inspecté) ; None tant qu'il tourne
    finalized: threading.Event = field(default_factory=threading.Event)
    parked: bool = False  # arrêté sur une carte : noyau, registre et source gardés pour l'exécution différée
    scoped: Any = None  # source de code ouverte sur son worktree (fermée à la reprise ou à l'abandon)
    run: Any = None  # git dans le bac à sable du parent (finalisation du worktree à la reprise)
    created_at: float = field(default_factory=time.time)  # heure murale (gardée dans l'état local)
    restored: bool = False  # relu de l'état local après un redémarrage : fini, jamais repris
    registry: Any = None  # registre du parent résolu à l'appel (`bind` d'une fonction : multi-locataire)

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def cancel(self, reason: str = "cancelled", summary: str = "annulé à la demande : le sous-agent a été arrêté")\
            -> bool:
        """Arrête l'enfant (une fois) : résultat `interrupted`, outils muets, noyau prévenu. False s'il avait fini."""
        if self.parked and not self.cancelled:  # en attente d'une carte : il ne la reprendra pas
            self.cancelled = True
            self.result = {"status": "interrupted", "exit_reason": reason,
                           "summary": summary + " (sa carte en attente ne sera pas exécutée)"}
            if self.final is not None:
                self.final = {**self.final, **self.result}
            return True
        if self.done.is_set() or self.cancelled or self.final is not None:
            return False
        self.cancelled = True
        self.result = {"status": "interrupted", "exit_reason": reason, "summary": summary}
        self.stop_kernel()
        return True

    def stop_kernel(self) -> None:
        k = self.kernel
        if k is not None and callable(getattr(k, "cancel", None)):
            try:
                k.cancel(self.thread_id)
            except Exception:  # noqa: BLE001 — l'annulation du noyau est un plus : les outils sont déjà muets
                logger.debug("cancel du noyau enfant %s a levé", self.id, exc_info=True)


class _ChildObserver:
    """`TurnObserver` du Kernel enfant : relaie la progression et nourrit l'observateur du parent (BubbleHub) sans le
    remplacer. Toute exception est avalée ici et par la boucle."""

    def __init__(self, child: _Child, emit: Callable[[str, dict[str, Any]], None], parent: Any | None,
                 on_resumed: Callable[[_Child, Any], None] | None = None) -> None:
        self._child, self._emit, self._parent, self._on_resumed = child, emit, parent, on_resumed

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
        self._child.last_tool = getattr(call, "name", None)
        if self._child.cancelled:  # annulé avant que son tour ne démarre (qui efface les annulations d'avant)
            self._child.stop_kernel()
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
        if self._child.parked and self._on_resumed is not None:  # tour de suite d'une carte (exécuteur)
            try:
                self._on_resumed(self._child, turn)
            except Exception:  # noqa: BLE001
                logger.exception("reprise du sous-agent %s", self._child.id)


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
            why = ("a été annulé" if self._child.result.get("exit_reason") == "cancelled"
                   else "a dépassé son délai d'inactivité")
            return ToolResult(call_id="", name=name, content=f"INTERROMPU : le sous-agent {why}, aucun outil n'est "
                              "plus exécuté.", is_error=True)
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
                 = None, parent_observer: Any | None = None, poll_s: float = 0.05, background: bool = False,
                 max_background: int = MAX_TASKS, note: Callable[[str, str], None] | None = None,
                 state_path: str | os.PathLike[str] | None = None, max_live: int | None = None,
                 require_isolation: bool = False) -> None:
        self.kernel_factory, self.code_source = kernel_factory, code_source
        self.max_children, self.max_depth, self.idle_timeout_s = max(1, max_children), max_depth, idle_timeout_s
        self._emit_fn, self.parent_observer, self.poll_s = emit, parent_observer, poll_s
        self.background, self.max_background, self.note = background, max(1, max_background), note
        # Sessions de code multi-utilisateur (docs/LOKKAL.md §7.28) : au plus `max_live` enfants VIVANTS (premier plan,
        # fond, en attente d'une carte) pour cette source, refus au-delà ; `require_isolation` : `isolate=true` sans
        # worktree possible est REFUSÉ (jamais de repli sur le dossier de travail : l'effet annoncé était WRITE).
        self.max_live, self.require_isolation = max_live, require_isolation
        self._registry: Any = None
        self._slots = threading.BoundedSemaphore(self.max_children)
        # Enfants par fil parent : ceux de fond (lus par delegate_status) et ceux de premier plan en cours (pour
        # l'annulation du fil). Un enfant de premier plan sort de la table à la fin de son `delegate`.
        self._by_parent: dict[str, dict[str, _Child]] = {}
        self._parked: dict[str, _Child] = {}  # fil enfant -> enfant en attente d'une carte (ordre d'arrivée)
        self._table_lock = threading.Lock()
        # État local (§7.22.9) : enfants de fond, et enfants en cours ou en attente d'une carte, relus au démarrage.
        self.state_path = state_path
        self._state_lock = threading.Lock()
        self._restore()

    def bind(self, registry: Any) -> None:
        """Le registre du parent ; ou une fonction sans argument qui le rend AU MOMENT de l'appel (noyau multi-locataire :
        le jeu d'outils du locataire courant)."""
        self._registry = registry

    def _parent_registry(self) -> Any:
        reg = self._registry
        if reg is not None and not hasattr(reg, "sources") and callable(reg):
            reg = reg()
        return reg if reg is not None and hasattr(reg, "sources") else None

    def _live_locked(self) -> int:
        kids = [c for table in self._by_parent.values() for c in table.values()]
        kids += [c for c in self._parked.values() if c not in kids]
        return sum(1 for c in kids if not c.restored and (c.parked or not c.done.is_set()
                                                          or (c.background and c.final is None)))

    def live_count(self) -> int:
        """Enfants vivants de cette source : en cours, en file, en attente d'une carte, ou de fond pas finalisés."""
        with self._table_lock:
            return self._live_locked()

    # ── ToolSource ──
    def specs(self) -> list[ToolSpec]:
        if not self.background:
            return [ToolSpec(
                name="delegate", core=False, source=SOURCE, effect=Effect.WRITE, effect_of=self._effect_of,
                description="Confie des sous-tâches à des sous-agents qui travaillent en parallèle, chacun dans son "
                            "worktree git (branche nokker/sa-<id>) ; rend un résumé par enfant. Bloque jusqu'à leur "
                            "retour.",
                parameters=_SPECS_PARAMS)]
        params = {**_SPECS_PARAMS, "properties": {**_SPECS_PARAMS["properties"], "background": _BACKGROUND_PARAM}}
        return [
            ToolSpec(name="delegate", core=False, source=SOURCE, effect=Effect.WRITE, effect_of=self._effect_of,
                     description="Confie des sous-tâches à des sous-agents qui travaillent en parallèle, chacun dans "
                                 "son worktree git (branche nokker/sa-<id>) ; rend un résumé par enfant. Bloque jusqu'à "
                                 "leur retour, sauf background=true (ids tout de suite).",
                     parameters=params),
            ToolSpec(name="delegate_status", core=False, source=SOURCE, effect=Effect.READ, fresh=True,
                     description="État ou résultat des sous-agents en arrière-plan de ce fil (attente bornée wait_s).",
                     parameters=_STATUS_PARAMS),
            ToolSpec(name="delegate_cancel", core=False, source=SOURCE, effect=Effect.READ, fresh=True,
                     description="Arrête des sous-agents en arrière-plan de ce fil (worktree conservé).",
                     parameters=_CANCEL_PARAMS)]

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

        if name in ("delegate_status", "delegate_cancel"):
            if not self.background:
                return out(f"outil inconnu : {name}", True)
            if self.depth_of(ctx.thread_id) >= self.max_depth:
                return out("un sous-agent ne lit ni n'arrête d'autres sous-agents", True)
            return self._status_call(arguments or {}, ctx) if name == "delegate_status" \
                else self._cancel_call(arguments or {}, ctx)
        if name != "delegate":
            return out(f"outil inconnu : {name}", True)
        background = arguments.get("background", False) is True
        if background and not self.background:
            return out("delegate en arrière-plan indisponible ici (le processus s'arrête avec le tour) : relance "
                       "sans background", True)
        registry = self._parent_registry()
        if registry is None:
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
                                   parent_thread_id=ctx.thread_id, code=code, background=background,
                                   registry=registry))
        with self._table_lock:
            if self.max_live is not None:
                live = self._live_locked()
                if live + len(children) > self.max_live:
                    return out(f"trop de sous-agents vivants dans cette session : {live} en cours, au plus "
                               f"{self.max_live} à la fois (delegate_status pour suivre, delegate_cancel pour "
                               "arrêter)", True)
            if background:
                live = sum(1 for kids in self._by_parent.values() for c in kids.values()
                           if c.background and c.final is None)
                if live + len(children) > self.max_background:
                    return out(f"trop de sous-agents en arrière-plan : {live} en cours, au plus "
                               f"{self.max_background} à la fois (delegate_status pour suivre, delegate_cancel pour "
                               "arrêter)", True)
            table = self._by_parent.setdefault(ctx.thread_id, {})
            for ch in children:
                table[ch.id] = ch

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
        if isolate and self.require_isolation and any(ch.info is None for ch in children):
            for ch in children:  # rien ne part : les worktrees déjà créés (vides) sont retirés avec la preuve habituelle
                if ch.info is not None and run is not None:
                    try:
                        finalize_worktree(run, ch.info)
                    except Exception:  # noqa: BLE001 — retrait au mieux, le refus reste
                        logger.warning("worktree de %s non retiré", ch.id, exc_info=True)
            with self._table_lock:
                table = self._by_parent.get(ctx.thread_id, {})
                for ch in children:
                    table.pop(ch.id, None)
                if not table:
                    self._by_parent.pop(ctx.thread_id, None)
            return out("isolation impossible (pas de dépôt git avec au moins un commit dans le dossier de travail) : "
                       "aucun sous-agent lancé. Relance avec isolate=false pour qu'ils travaillent directement dans le "
                       "dossier (carte forte), ou commite d'abord.", True)
        self._persist()  # avant le départ : un redémarrage pendant le travail retrouve enfants et worktrees
        for ch in children:
            started = {"thread_id": ctx.thread_id, "parent_thread_id": ctx.thread_id, "parent_turn": ctx.turn_index,
                       "child_id": ch.id, "child_thread_id": ch.thread_id, "goal": str(ch.task["goal"])[:300]}
            self.emit("subagent.started", {**started, "background": True} if background else started)
            ch.touch()
            cx = contextvars.copy_context()
            threading.Thread(target=cx.run, args=(self._worker, ch, ctx), daemon=True,
                             name=f"nokker-sa-{ch.id}").start()

        if background:
            cx = contextvars.copy_context()  # locataire courant, pour les événements de fin
            threading.Thread(target=cx.run, args=(self._watch, children, run), daemon=True,
                             name=f"nokker-sa-watch-{children[0].id}").start()
            return out({"background": True, "children": [self._view(ch) for ch in children],
                        "note": "les sous-agents tournent en arrière-plan : delegate_status pour l'état ou le résultat "
                                "(wait_s pour attendre), delegate_cancel pour arrêter ; tu seras prévenu au tour "
                                "suivant quand ils ont fini."})
        try:
            self._wait(children)
            results = [self._finish(ch, run) for ch in children]
        finally:
            with self._table_lock:
                table = self._by_parent.get(ctx.thread_id, {})
                for ch in children:
                    table.pop(ch.id, None)
                if not table:
                    self._by_parent.pop(ctx.thread_id, None)
            self._persist()  # premier plan rendu : seuls ses enfants en attente d'une carte restent dans l'état
        return out(results)

    # ── arrière-plan ──
    def _watch(self, children: list[_Child], run: Run | None) -> None:
        """Fil de garde d'un lot de fond : attente (délai d'inactivité), finalisation, avis au parent."""
        try:
            self._wait(children)
        except Exception:  # noqa: BLE001 — la finalisation a lieu quand même
            logger.exception("sous-agents de fond : attente")
        for ch in children:
            try:
                ch.final = self._finish(ch, run)
            except Exception as e:  # noqa: BLE001
                logger.exception("sous-agent de fond %s : finalisation", ch.id)
                ch.final = {"task_index": ch.index, "child_id": ch.id, "thread_id": ch.thread_id, "status": "failed",
                            "exit_reason": "exception", "summary": f"{type(e).__name__}: {e}"}
        self._prune(children[0].parent_thread_id)
        self._persist()  # avant de dire « fini » : un résultat lu est un résultat gardé
        for ch in children:
            ch.finalized.set()
        if self.note is not None:
            done = ", ".join(f"{c.id} ({(c.final or {}).get('status')}, {(c.final or {}).get('exit_reason')})"
                             for c in children)
            try:
                self.note(children[0].parent_thread_id, f"[sous-agents en arrière-plan] terminés : {done} — "
                                                        "delegate_status pour lire leurs résultats.")
            except Exception:  # noqa: BLE001 — l'avis est un plus ; le résultat reste lisible
                logger.debug("note au parent a levé", exc_info=True)

    def _prune(self, parent: str) -> None:
        with self._table_lock:
            table = self._by_parent.get(parent)
            if not table:
                return
            done = [cid for cid, c in table.items() if c.final is not None]
            for cid in done[:max(0, len(done) - KEEP_FINISHED)]:
                table.pop(cid, None)

    def _view(self, ch: _Child) -> dict[str, Any]:
        elapsed = time.time() - ch.created_at if ch.restored else time.monotonic() - ch.created
        base = {"child_id": ch.id, "thread_id": ch.thread_id, "task_index": ch.index,
                "goal": str(ch.task.get("goal") or "")[:300], "elapsed_s": round(max(0.0, elapsed), 1)}
        if ch.final is not None:
            return {**ch.final, **base}
        view = {**base, "status": "running" if ch.started else "queued"}
        if ch.cancelled:
            view["status"] = "stopping"
        if ch.last_tool:
            view["last_tool"] = ch.last_tool
        if ch.info is not None:
            view["worktree"] = {"path": ch.info["path"], "branch": ch.info["branch"]}
        elif ch.note:
            view["isolated"] = False
        return view

    def _mine(self, thread_id: str, ids: Any) -> tuple[list[_Child], list[str]]:
        with self._table_lock:
            kids = [c for c in self._by_parent.get(thread_id, {}).values() if c.background or c.restored]
        if ids in (None, []):
            return kids, []
        if not isinstance(ids, list):
            raise ValueError("ids : liste de child_id attendue")
        wanted = [str(i) for i in ids]
        by_id = {c.id: c for c in kids}
        return [by_id[i] for i in wanted if i in by_id], [i for i in wanted if i not in by_id]

    def _status_call(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def out(payload: Any, error: bool = False) -> ToolResult:
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            return ToolResult(call_id="", name="delegate_status", content=text, is_error=error)

        try:
            wait = float(args.get("wait_s") or 0)
            kids, unknown = self._mine(ctx.thread_id, args.get("ids"))
        except (TypeError, ValueError) as e:
            return out(f"arguments invalides : {e}", True)
        deadline = time.monotonic() + max(0.0, min(wait, MAX_STATUS_WAIT_S))
        for ch in kids:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            ch.finalized.wait(left)
        views = [self._view(ch) for ch in kids]
        payload: dict[str, Any] = {"children": views,
                                   "running": sum(1 for v in views if v["status"] in ("queued", "running",
                                                                                      "stopping")),
                                   "finished": sum(1 for v in views if v["status"] not in ("queued", "running",
                                                                                           "stopping"))}
        if unknown:
            payload["unknown"] = unknown
        return out(payload)

    def _cancel_call(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            kids, unknown = self._mine(ctx.thread_id, args.get("ids"))
        except ValueError as e:
            return ToolResult(call_id="", name="delegate_cancel", content=f"arguments invalides : {e}", is_error=True)
        cancelled = [ch.id for ch in kids if ch.cancel()]
        for ch in kids:
            if ch.parked:
                self._unpark(ch)
        self._persist()
        payload: dict[str, Any] = {"cancelled": cancelled}
        if unknown:
            payload["unknown"] = unknown
        return ToolResult(call_id="", name="delegate_cancel", content=json.dumps(payload, ensure_ascii=False))

    def cancel_children(self, thread_id: str) -> int:
        """Annulation d'un fil (`POST /v1/threads/{id}/cancel`) : ses enfants en cours, de fond et de premier plan ; un
        fil `<parent>/sa-<id>` : cet enfant seulement. Rend le nombre d'enfants arrêtés."""
        with self._table_lock:
            if "/sa-" in thread_id:
                parent = thread_id.rsplit("/sa-", 1)[0]
                kids = [c for c in self._by_parent.get(parent, {}).values() if c.thread_id == thread_id]
                parked = [c for c in self._parked.values() if c.thread_id == thread_id]
            else:
                kids = list(self._by_parent.get(thread_id, {}).values())
                parked = [c for c in self._parked.values() if c.parent_thread_id == thread_id]
        kids += [c for c in parked if c not in kids]
        n = sum(1 for ch in kids if ch.cancel())
        for ch in parked:
            self._unpark(ch)
        self._persist()
        return n

    def cancel_all(self) -> int:
        """Tous les enfants de cette source (arrêt du noyau, fermeture de toutes les sessions)."""
        with self._table_lock:
            parents = {*self._by_parent, *(c.parent_thread_id for c in self._parked.values())}
        return sum(self.cancel_children(p) for p in parents)

    # ── état local (redémarrage, §7.22.9) ──
    def _records(self) -> list[dict[str, Any]]:
        """Ce qui survit à un redémarrage : enfants de fond (résultats compris, `KEEP_FINISHED` par fil), enfants de
        premier plan encore en cours, et tout enfant en attente d'une carte. Jamais de noyau, de source ni de secret :
        la tâche, l'état, le résultat et le worktree."""
        with self._table_lock:
            kids = [c for table in self._by_parent.values() for c in table.values()]
            kids += [c for c in self._parked.values() if c not in kids]
        out = []
        for ch in kids:
            finished = ch.final is not None or (not ch.background and ch.done.is_set() and not ch.parked)
            if ch.restored or ch.background or ch.parked or not finished:
                rec: dict[str, Any] = {
                    "child_id": ch.id, "thread_id": ch.thread_id, "parent_thread_id": ch.parent_thread_id,
                    "task_index": ch.index, "goal": str(ch.task.get("goal") or "")[:300],
                    "role": ch.task.get("role", "worker"), "background": ch.background, "parked": ch.parked,
                    "created_at": ch.created_at}
                if ch.info is not None:
                    rec["worktree"] = {"path": ch.info["path"], "branch": ch.info["branch"]}
                if ch.final is not None and not ch.parked:
                    rec["final"] = ch.final
                out.append(rec)
        return out

    def _persist(self) -> None:
        if not self.state_path:
            return
        from pathlib import Path

        path = Path(self.state_path)
        try:
            with self._state_lock:
                data = json.dumps({"v": 1, "children": self._records()}, ensure_ascii=False, default=str)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, path)
        except Exception:  # noqa: BLE001 — l'état local est un plus : un enfant ne tombe jamais pour lui
            logger.warning("état des sous-agents non écrit (%s)", path, exc_info=True)

    def _restore(self) -> None:
        """Démarrage : relit l'état local. Un enfant non fini (en cours, en file, en attente d'une carte) devient
        `interrupted / restart`, worktree conservé (jamais supprimé ici : rien n'est prouvé) ; il n'entre JAMAIS dans
        la table des enfants en attente, donc sa carte reste refusée (`kernel_for_card` → None, jamais le noyau du
        parent). Reprendre un enfant n'est pas fait."""
        if not self.state_path:
            return
        from pathlib import Path

        try:
            raw = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
            records = raw.get("children") if isinstance(raw, dict) else None
        except (OSError, ValueError):
            records = None
        changed = False
        for rec in records if isinstance(records, list) else []:
            try:
                ch = _Child(index=int(rec.get("task_index") or 0), id=str(rec["child_id"]),
                            thread_id=str(rec["thread_id"]), task={"goal": str(rec.get("goal") or ""),
                                                                   "role": rec.get("role", "worker")},
                            schema=None, parent_thread_id=str(rec["parent_thread_id"]),
                            background=bool(rec.get("background")), restored=True,
                            created_at=float(rec.get("created_at") or time.time()))
            except (KeyError, TypeError, ValueError):
                continue
            wt = rec.get("worktree") if isinstance(rec.get("worktree"), dict) else None
            final = rec.get("final") if isinstance(rec.get("final"), dict) else None
            if final is None:
                changed = True
                why = ("interrompu par un redémarrage du noyau : le sous-agent ne tourne plus et n'est pas repris")
                if rec.get("parked"):
                    why += ("; sa carte en attente est refusée (jamais exécutée avec le noyau du parent)")
                final = {"task_index": ch.index, "child_id": ch.id, "thread_id": ch.thread_id,
                         "status": "interrupted", "exit_reason": "restart",
                         "summary": why + ("; worktree conservé : relis la branche" if wt else "")}
                if wt:
                    final["worktree"] = {"path": wt.get("path"), "branch": wt.get("branch"), "kept": True,
                                         "note": "conservé au redémarrage, non inspecté"}
            ch.final, ch.result, ch.cancelled = final, dict(final), True
            ch.done.set()
            ch.finalized.set()
            with self._table_lock:
                self._by_parent.setdefault(ch.parent_thread_id, {})[ch.id] = ch
        for parent in list(self._by_parent):
            self._prune(parent)
        if changed:
            self._persist()

    # ── cartes décidées après la fin d'un enfant ──
    def kernel_for_card(self, thread_id: str) -> Any | None:
        """Noyau de l'enfant en attente sur `thread_id` (exécuteur d'approbation), ou None : il n'existe plus."""
        with self._table_lock:
            ch = self._parked.get(thread_id)
        return ch.kernel if ch is not None and not ch.cancelled and ch.kernel is not None else None

    def _park(self, ch: _Child, scoped: Any | None) -> None:
        ch.parked, ch.scoped = True, scoped
        with self._table_lock:
            self._parked[ch.thread_id] = ch
            evicted = list(self._parked.values())[:max(0, len(self._parked) - MAX_PARKED)]
        for old in evicted:
            old.cancelled = True
            self._unpark(old)
        self._persist()

    def _unpark(self, ch: _Child) -> bool:
        with self._table_lock:
            was = self._parked.pop(ch.thread_id, None) is not None
        ch.parked = False
        scoped, ch.scoped = ch.scoped, None
        if scoped is not None:
            try:
                scoped.close()
            except Exception:  # noqa: BLE001 — fermeture au mieux
                logger.debug("fermeture de la source du sous-agent %s", ch.id, exc_info=True)
        return was

    def _resumed(self, ch: _Child, turn: Any) -> None:
        """Fin du tour de suite d'une carte, avec le noyau de l'enfant : encore une carte → toujours en attente ; sinon
        l'enfant est repris et son résultat publié."""
        self._account(ch, turn)
        if turn.pending_approvals and not ch.cancelled:
            ch.result = self._status(ch, turn)
            if ch.background:
                ch.final = {**(ch.final or {}), **ch.result}
            self._persist()
            return
        self._unpark(ch)
        ch.result = self._status(ch, turn) if not ch.cancelled else ch.result
        res = self._finish(ch, ch.run, resumed=True)
        if ch.background:
            ch.final = res
        self._persist()
        if self.note is not None:
            summary = str(res.get("summary") or "")
            try:
                self.note(ch.parent_thread_id, f"[sous-agent {ch.id}] repris après la carte : {res.get('status')}, "
                                               f"{res.get('exit_reason')} — {summary[:1500]}")
            except Exception:  # noqa: BLE001
                logger.debug("note au parent a levé", exc_info=True)

    # ── attente (délai d'inactivité) ──
    def _wait(self, children: list[_Child]) -> None:
        pending = list(children)
        while pending:
            now = time.monotonic()
            for ch in list(pending):
                if ch.done.is_set() or ch.cancelled:  # annulé (outil ou fil) : on n'attend plus son retour
                    pending.remove(ch)
                elif ch.started and now - ch.last_activity > self.idle_timeout_s:
                    ch.cancel("idle_timeout", f"interrompu : aucune activité depuis {self.idle_timeout_s:g} s")
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
        for src in (ch.registry if ch.registry is not None else self._registry).sources:
            if src is self or getattr(src, "is_delegate_source", False):
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
        park = False
        try:
            registry = self._child_registry(ch, scoped, self.depth_of(ctx.thread_id))
            observer = _ChildObserver(ch, self.emit, self.parent_observer, on_resumed=self._resumed)
            kernel = self.kernel_factory(registry, ch.thread_id, observer)
            ch.kernel = kernel
            if ch.cancelled:  # annulé pendant la construction : rien ne part
                return dict(ch.result)
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
            res = self._status(ch, turn)
            park = res["exit_reason"] == "approval_pending" and not ch.cancelled
            return res
        finally:
            if park:  # carte posée : noyau, registre et worktree gardés pour son exécution différée
                self._park(ch, scoped)
            elif scoped is not None:
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
    def _finish(self, ch: _Child, run: Run | None, resumed: bool = False) -> dict[str, Any]:
        res = {"task_index": ch.index, **(ch.result or {"status": "failed", "exit_reason": "no_result",
                                           "summary": ""})}
        res["child_id"] = ch.id
        res["thread_id"] = ch.thread_id
        commits, dirty, kept = 0, False, False
        ch.run = run
        if ch.info is not None and run is not None:
            try:
                # Un enfant interrompu peut encore écrire, un enfant en attente reprendra sur sa carte : on inspecte,
                # on ne supprime pas.
                wt = finalize_worktree(run, ch.info, prune=not ch.cancelled and not ch.parked)
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
        finished = {"parent_thread_id": ch.parent_thread_id, "child_id": ch.id, "status": res["status"],
                    "commits": commits, "dirty": dirty, "kept": kept}
        if ch.background:
            finished.update(background=True, exit_reason=res.get("exit_reason"))
        if resumed:
            finished.update(resumed=True, exit_reason=res.get("exit_reason"))
        self.emit("subagent.finished", finished)
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


__all__ = ["DENIED_FOR_CHILD", "DelegateToolSource", "MAX_STATUS_WAIT_S", "WORKTREES_DIR", "create_worktree", "ensure_excluded",
           "finalize_worktree", "resolve_repo_root", "supports_relative_paths", "worktree_context_note"]
