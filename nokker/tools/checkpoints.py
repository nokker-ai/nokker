"""nokker/tools/checkpoints.py — points de retour de Nokker Lokkal (docs/LOKKAL.md §7.4 points 6 et 7,
docs/LOKKAL-CLI.md §3.3).

Avant qu'un outil modifie le dossier de travail, le noyau en prend une photo ; le modèle (ou l'utilisateur, via la
CLI) peut ensuite lister les points, voir ce qui a changé et revenir en arrière.

Reprise de Hermes au niveau de ses BRIQUES : `tools.checkpoint_manager.CheckpointManager` (un dépôt git « fantôme »
partagé sous `$HERMES_HOME/checkpoints`, `GIT_DIR` dans ce magasin, `GIT_WORK_TREE` = le dossier : rien n'est écrit
dans le dépôt de l'utilisateur, ledger des fichiers écrits par l'agent, `restore(safe=True)` qui laisse en place ce
que l'utilisateur a modifié à la main) et `tools.checkpoint_maintenance` (purge).

Contrat de sécurité (LOKKAL §7.4.6) : le noyau ne lance JAMAIS git avec les hooks du dépôt de l'utilisateur. Ici
le `GIT_DIR` est toujours le nôtre (les hooks lus sont ceux du magasin, jamais ceux de `<dossier>/.git/hooks`), et
on force en plus `core.hooksPath=/dev/null` dans l'environnement de chaque sous-processus git du gestionnaire
(`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`, ce que le gestionnaire ne sait pas faire seul).

Hermes n'est importé qu'à la première utilisation : sans lui (CI unitaire), le module se charge, les schémas
existent, l'enveloppe est transparente et les outils répondent « points de retour indisponibles ».
"""
from __future__ import annotations

import re
import json
import logging
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nokker.core.ports import ToolContext, ToolSource, ToolSpec, call_effect
from nokker.core.turn import Effect, ToolResult

logger = logging.getLogger("nokker.tools.checkpoints")

WORKSPACE_MOUNT = "/workspace"
MAX_DIFF_BYTES = 200 * 1024  # un diff plus long est tronqué, avec mention
UNAVAILABLE = "points de retour indisponibles"
SOURCE = "builtin:checkpoints"
WRITE_TOOLS = ("write_file", "patch")  # après un succès : écriture notée au ledger (restore safe)

_SPECS: tuple[tuple[str, str, Effect, dict[str, Any]], ...] = (
    ("checkpoint_create", "Prend un point de retour du dossier de travail maintenant (photo avant un changement risqué).",
     Effect.WRITE, {"type": "object", "properties": {
         "reason": {"type": "string", "description": "pourquoi (affiché dans la liste)"}}}),
    ("checkpoint_list", "Liste les points de retour du dossier de travail, du plus récent au plus ancien.",
     Effect.READ, {"type": "object", "properties": {}}),
    ("checkpoint_diff", "Montre ce qui a changé depuis un point de retour (sans hash : depuis le premier point de la "
     "session, donc tout ce que l'agent a modifié).", Effect.READ, {
         "type": "object", "properties": {"hash": {"type": "string", "description": "hash d'un point (checkpoint_list)"}}}),
    ("checkpoint_restore", "Revient à un point de retour : tout le dossier, ou un seul fichier. En mode sûr (défaut), "
     "les fichiers que l'utilisateur a modifiés lui-même depuis ne sont pas écrasés.", Effect.DESTRUCTIVE, {
         "type": "object", "required": ["hash"], "properties": {
             "hash": {"type": "string"},
             "file": {"type": "string", "description": "un seul fichier, relatif au dossier de travail"},
             "safe": {"type": "boolean", "default": True}}}),
)
CHECKPOINT_TOOLS = tuple(name for name, *_ in _SPECS)


# ─────────────────────────────── chargement paresseux ───────────────────────────────

_lock = threading.Lock()
_cm_module: Any = None  # tools.checkpoint_manager, ou False si absent


def nokker_home() -> Path:
    return Path(os.environ.get("NOKKER_HOME") or "~/.nokker").expanduser()


def _prepare_home(home: Path | None) -> None:
    """`HERMES_HOME` doit être posé AVANT l'import (CHECKPOINT_BASE en dérive) : `home` explicite, sinon ce qui est
    déjà posé, sinon `$NOKKER_HOME` (défaut ~/.nokker)."""
    if home is not None:
        os.environ["HERMES_HOME"] = str(home)
    elif not os.environ.get("HERMES_HOME"):
        os.environ["HERMES_HOME"] = str(nokker_home())


def _no_user_hooks(original: Callable[..., dict]) -> Callable[..., dict]:
    def _git_env(*args: Any, **kwargs: Any) -> dict:
        env = original(*args, **kwargs)
        try:
            n = int(env.get("GIT_CONFIG_COUNT") or 0)
        except ValueError:
            n = 0
        env[f"GIT_CONFIG_KEY_{n}"] = "core.hooksPath"
        env[f"GIT_CONFIG_VALUE_{n}"] = os.devnull
        env["GIT_CONFIG_COUNT"] = str(n + 1)
        return env

    _git_env._nokker_hooks_off = True  # type: ignore[attr-defined]
    return _git_env


def _system_git_env(base: Any = None) -> dict[str, str]:
    return dict(base if base is not None else os.environ)


def _hermes_checkpoints(home: Path | None = None) -> Any | None:
    """Module `tools.checkpoint_manager` (hooks coupés), ou None si Hermes est absent."""
    global _cm_module
    with _lock:
        _prepare_home(home)
        if _cm_module is None:
            try:
                from tools import checkpoint_manager as cm  # Hermes (dépendance épinglée, docs/REPO.md)
            except Exception:  # noqa: BLE001 — absent (CI unitaire)
                _cm_module = False
            else:
                if not getattr(cm._git_env, "_nokker_hooks_off", False):
                    cm._git_env = _no_user_hooks(cm._git_env)
                    if sys.platform != "win32":
                        # Hermes lance un worker de gestionnaire de paquets (~1,7 s) à CHAQUE appel git pour
                        # constater que, sous POSIX, c'est le git du système qui sert : on saute ce détour.
                        cm.selected_git_env = _system_git_env
                _cm_module = cm
        return _cm_module or None


def make_manager(home: Path | None = None, *, max_snapshots: int = 20, max_total_size_mb: int = 500,
                 max_file_size_mb: int = 10) -> Any | None:
    cm = _hermes_checkpoints(home)
    if cm is None:
        return None
    return cm.CheckpointManager(enabled=True, max_snapshots=max_snapshots, max_total_size_mb=max_total_size_mb,
                                max_file_size_mb=max_file_size_mb)


def auto_prune(home: Path | None = None) -> dict[str, Any]:
    """Purge au démarrage du noyau : points de plus de 7 jours, dossiers disparus, plafond 500 Mo, au plus une fois
    par jour. Ne lève jamais ; `{"skipped": True, "error": …}` sans Hermes."""
    if _hermes_checkpoints(home) is None:
        return {"skipped": True, "error": UNAVAILABLE}
    try:
        from tools.checkpoint_maintenance import maybe_auto_prune_checkpoints

        return maybe_auto_prune_checkpoints(retention_days=7, max_total_size_mb=500)
    except Exception as e:  # noqa: BLE001
        logger.warning("purge des points de retour en échec", exc_info=True)
        return {"skipped": True, "error": f"{type(e).__name__}: {e}"}


def code_root_of(code_source: Any) -> Callable[[str], Path | None]:
    """`root_of` pour un `CodeToolSource` : un seul dossier hôte, le même pour tous les fils."""
    return lambda _thread_id: Path(code_source.root)


def host_path(root: Path, path: str, mount: str = WORKSPACE_MOUNT) -> str:
    """Chemin vu par l'outil → chemin hôte : `/workspace/x` (ou le montage du bac) → `root/x` ; relatif → sous
    `root`."""
    if path == mount or path.startswith(mount + "/"):
        return str(root / path[len(mount):].lstrip("/"))
    p = Path(path)
    return str(p if p.is_absolute() else root / p)


def _written_paths(name: str, args: dict[str, Any]) -> list[str]:
    if name == "write_file":
        return [str(args["path"])] if args.get("path") else []
    if name == "patch":
        if args.get("mode") == "patch":
            import re

            return [m.group(2).strip() for m in re.finditer(
                r"^\*\*\* (Update|Add) File: (.+)$", str(args.get("patch") or ""), flags=re.MULTILINE)]
        return [str(args["path"])] if args.get("path") else []
    return []


# ─────────────────────────────── enveloppe ───────────────────────────────


class CheckpointingToolSource:
    """Enveloppe d'un `ToolSource` (typiquement `CodeToolSource`) : capture AVANT tout appel non-READ.

    Délègue `specs`, `call`, `turn_done` et `close` à `inner`. Autour de `call` : `new_turn()` quand `turn_index`
    change pour ce fil ; `ensure_checkpoint(root, "avant <outil>")` si `call_effect` n'est pas READ ;
    `record_agent_write(chemin hôte)` après un `write_file`/`patch` réussi. Un échec de capture est journalisé et
    n'empêche JAMAIS l'appel. Sans Hermes (`manager` None) : transparente."""

    def __init__(self, inner: ToolSource, root_of: Callable[[str], Path | None], home: Path | None = None,
                 manager: Any | None = None) -> None:
        self.inner = inner
        self.root_of = root_of
        self.manager = manager if manager is not None else make_manager(home)
        self._turns: dict[str, int] = {}
        self._mu = threading.Lock()

    def specs(self) -> list[ToolSpec]:
        return self.inner.specs()

    def _spec(self, name: str) -> ToolSpec | None:
        return next((s for s in self.inner.specs() if s.name == name), None)

    def _root(self, thread_id: str) -> Path | None:
        try:
            r = self.root_of(thread_id)
            return Path(r) if r is not None else None
        except Exception:  # noqa: BLE001
            logger.warning("racine du fil %s introuvable", thread_id, exc_info=True)
            return None

    def _before(self, name: str, args: dict[str, Any], ctx: ToolContext) -> Path | None:
        if self.manager is None:
            return None
        root = self._root(ctx.thread_id)
        if root is None:
            return None
        try:
            with self._mu:
                if self._turns.get(ctx.thread_id) != ctx.turn_index:
                    self._turns[ctx.thread_id] = ctx.turn_index
                    self.manager.new_turn()
            if call_effect(self._spec(name), args) is not Effect.READ:
                self.manager.ensure_checkpoint(str(root), f"avant {name}")
        except Exception:  # noqa: BLE001 — la capture ne fait jamais échouer l'outil
            logger.warning("capture avant %s en échec", name, exc_info=True)
        return root

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = self._before(name, arguments, ctx)
        result = self.inner.call(name, arguments, ctx)
        if root is not None and name in WRITE_TOOLS and not result.is_error:
            try:
                for p in _written_paths(name, arguments):
                    self.manager.record_agent_write(host_path(root, p, getattr(self.inner, "mount", WORKSPACE_MOUNT)))
            except Exception:  # noqa: BLE001
                logger.warning("ledger après %s en échec", name, exc_info=True)
        return result

    def turn_done(self, thread_id: str) -> None:
        hook = getattr(self.inner, "turn_done", None)
        if callable(hook):
            hook(thread_id)

    def close(self, *args: Any, **kwargs: Any) -> None:
        close = getattr(self.inner, "close", None)
        if callable(close):
            close(*args, **kwargs)


# ─────────────────────────────── outils ───────────────────────────────


def _clip_diff(text: str) -> str:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= MAX_DIFF_BYTES:
        return text
    return (raw[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
            + f"\n[… diff tronqué : {len(raw) - MAX_DIFF_BYTES} octets de plus ; précise un fichier ou "
              "un point plus récent …]")


class CheckpointToolSource:
    """`ToolSource` des quatre outils de points de retour, sur le dossier hôte que donne `root_of(thread_id)`."""

    def __init__(self, root_of: Callable[[str], Path | None], home: Path | None = None, manager: Any | None = None,
                 core: bool = False) -> None:
        self.root_of = root_of
        self.manager = manager if manager is not None else make_manager(home)
        self.core = core

    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=n, description=d, parameters=p, effect=e, core=self.core, source=SOURCE)
                for n, d, e, p in _SPECS]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def out(payload: Any, error: bool = False) -> ToolResult:
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            return ToolResult(call_id="", name=name, content=text, is_error=error)

        if name not in CHECKPOINT_TOOLS:
            return out(f"outil inconnu : {name}", True)
        if self.manager is None:
            return out(UNAVAILABLE, True)
        try:
            root = self.root_of(ctx.thread_id)
            if root is None:
                return out("aucun dossier de travail pour ce fil", True)
            root_s, a, m = str(root), arguments, self.manager
            if name == "checkpoint_create":
                reason = str(a.get("reason") or "").strip() or "manuel"
                before = {c["hash"] for c in m.list_checkpoints(root_s)}
                m._checkpointed_dirs.discard(str(Path(root_s).resolve()))  # demande explicite : pas de dédoublonnage de tour
                m.ensure_checkpoint(root_s, reason)
                new = _fr_entries([c for c in m.list_checkpoints(root_s) if c["hash"] not in before])
                if new:
                    return out({"created": True, **new[0]})
                return out({"created": False, "message": "aucun changement depuis le dernier point de retour"})
            if name == "checkpoint_list":
                return out(_fr_entries(m.list_checkpoints(root_s)))
            if name == "checkpoint_diff":
                h = str(a.get("hash") or "").strip()
                r = m.diff(root_s, h) if h else m.session_diff(root_s)
                if not r.get("success"):
                    return out(str(r.get("error") or "diff impossible"), True)
                if r.get("empty"):
                    return out({"stat": "", "diff": "", "message": "aucun changement"})
                return out({"stat": r.get("stat", ""), "diff": _clip_diff(str(r.get("diff", ""))),
                            **({"baseline": r["baseline"]} if r.get("baseline") else {})})
            # checkpoint_restore
            if not a.get("hash"):
                return out("argument manquant : hash", True)
            r = m.restore(root_s, str(a["hash"]), a.get("file") or None, safe=bool(a.get("safe", True)))
            if isinstance(r, dict) and r.get("reason"):
                r = {**r, "reason": fr_reason(str(r["reason"]))}
            return out(r, not r.get("success"))
        except Exception as e:  # noqa: BLE001 — l'erreur revient au modèle
            return out(f"{type(e).__name__}: {e}", True)


_REASON_PRE_ROLLBACK = re.compile(r"^pre-rollback snapshot \(restoring to ([0-9a-f]{7,40})\)$")


def fr_reason(reason: str) -> str:
    """Raisons automatiques du gestionnaire (en anglais) → français, pour la liste affichée au client."""
    r = (reason or "").strip()
    m = _REASON_PRE_ROLLBACK.match(r)
    if m:
        return f"avant retour à {m.group(1)[:7]}"
    return {"auto": "automatique", "manual": "manuel", "session start": "début de session"}.get(r, r)


def _fr_entries(entries: list[dict]) -> list[dict]:
    return [{**c, "reason": fr_reason(str(c.get("reason", "")))} for c in entries]


def checkpoint_sources(inner: ToolSource, root_of: Callable[[str], Path | None], home: Path | None = None
                       ) -> tuple[CheckpointingToolSource, CheckpointToolSource]:
    """Enveloppe + outils, sur le MÊME gestionnaire : `Registry(sources=[wrapper, tools, …])`."""
    manager = make_manager(home)
    return (CheckpointingToolSource(inner, root_of, home, manager), CheckpointToolSource(root_of, home, manager))


__all__ = ["CHECKPOINT_TOOLS", "CheckpointToolSource", "CheckpointingToolSource", "auto_prune",
           "checkpoint_sources", "code_root_of", "host_path", "make_manager", "nokker_home"]
