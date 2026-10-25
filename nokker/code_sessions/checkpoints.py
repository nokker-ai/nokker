"""Points de retour des sessions de code multi-utilisateur : UN magasin par session, jamais commun entre locataires.

Le gestionnaire de l'amont (`tools.checkpoint_manager.CheckpointManager`) range tous ses dépôts fantômes dans un seul
magasin, `<home>/checkpoints/store`, où `<home>` est le « home » de l'amont résolu À CHAQUE APPEL (variable de contexte
`hermes_constants.set_hermes_home_override`, sinon HERMES_HOME). En Lokkal (un seul propriétaire), ce magasin commun
est celui de la machine et rien ne change ici. Pour les sessions multi-utilisateur, `SessionCheckpoints` tient un
gestionnaire PAR SESSION et pose, le temps de chaque appel, le home de la session :

    <code.state_dir>/<tenant>/<user>/checkpoints/<session>/checkpoints/store

Hors de toute racine montée dans un bac à sable (le modèle ne peut ni lire ni réécrire le dépôt fantôme), effacé par la
purge de la session et par `nokker erase` (dossier du locataire). Le chemin passé au gestionnaire doit être la racine
d'une session — ou un fichier sous elle — du locataire COURANT : sinon `PermissionError`, rien n'est lu."""
from __future__ import annotations

import importlib
import importlib.util
import os
import threading
from collections.abc import Callable
from typing import Any

from nokker.code_sessions.paths import PathRefused, check_ids
from nokker.tenancy.context import current_tenant


def _upstream_override() -> tuple[Callable[[Any], Any], Callable[[Any], None]] | None:
    try:
        hc = importlib.import_module("hermes_constants")
    except ImportError:
        return None
    s, r = getattr(hc, "set_hermes_home_override", None), getattr(hc, "reset_hermes_home_override", None)
    return (s, r) if callable(s) and callable(r) else None


def _upstream_factory() -> Any:
    from nokker.tools.checkpoints import make_manager

    return make_manager(None)


class _Discard:
    """`manager._checkpointed_dirs.discard(chemin)` (CheckpointToolSource) : vers le gestionnaire de la session."""

    def __init__(self, owner: SessionCheckpoints) -> None:
        self.owner = owner

    def discard(self, path: str) -> None:
        try:
            _, _, _, mgr = self.owner._resolve(path)
        except PermissionError:
            return
        mgr._checkpointed_dirs.discard(path)


class SessionCheckpoints:
    """Le « gestionnaire » vu par `CheckpointingToolSource` et `CheckpointToolSource` : même interface, un magasin par
    session. `factory` : un gestionnaire de l'amont (défaut `make_manager`) ; `override` : (poser, retirer) le home."""

    def __init__(self, sessions: Any, factory: Callable[[], Any] | None = None,
                 override: tuple[Callable[[Any], Any], Callable[[Any], None]] | None = None) -> None:
        self.sessions = sessions
        self._factory = factory
        self._override = override
        self._managers: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._checkpointed_dirs = _Discard(self)

    def available(self) -> bool:
        """Vrai si l'amont sait poser un home par contexte et porte le gestionnaire (sans rien mettre en cache)."""
        if self._factory is not None and self._override is not None:
            return True
        try:
            if importlib.util.find_spec("tools.checkpoint_manager") is None:
                return False
        except (ImportError, ValueError):
            return False
        return _upstream_override() is not None

    # ── routage ──
    def _resolve(self, path: str) -> tuple[str, str, str, Any]:
        root = os.path.realpath(self.sessions.cfg.workspace_root)
        full = os.path.normpath(os.path.abspath(str(path)))  # un lien du dépôt ne déroute jamais vers une autre session
        rel = os.path.relpath(full, root)
        parts = rel.split(os.sep)
        if rel.startswith("..") or len(parts) < 3:
            raise PermissionError("points de retour : chemin hors d'une session de code")
        tenant, user, session = parts[:3]
        try:
            check_ids(tenant, user, session)
        except PathRefused:
            raise PermissionError("points de retour : chemin hors d'une session de code") from None
        if tenant != current_tenant():
            raise PermissionError("points de retour : session d'un autre locataire")
        with self._lock:
            mgr = self._managers.get(session)
            if mgr is None:
                factory = self._factory or _upstream_factory
                mgr = self._managers[session] = factory()
        if mgr is None:
            raise PermissionError("points de retour indisponibles")
        return tenant, user, session, mgr

    def _run(self, path: str, method: str, *args: Any, **kwargs: Any) -> Any:
        tenant, user, session, mgr = self._resolve(path)
        home = self.sessions.checkpoint_home(tenant, user, session)
        setter, reset = self._override or _upstream_override() or (None, None)
        if setter is None:
            raise PermissionError("points de retour indisponibles (amont sans home par contexte)")
        token = setter(str(home))
        try:
            return getattr(mgr, method)(*args, **kwargs)
        finally:
            reset(token)

    def forget(self, session_id: str) -> None:
        with self._lock:
            self._managers.pop(session_id, None)

    # ── interface du gestionnaire de l'amont ──
    def new_turn(self) -> None:
        with self._lock:
            managers = list(self._managers.values())
        for m in managers:
            m.new_turn()

    def ensure_checkpoint(self, working_dir: str, reason: str = "auto") -> bool:
        return self._run(working_dir, "ensure_checkpoint", working_dir, reason)

    def record_agent_write(self, file_path: str) -> None:
        self._run(file_path, "record_agent_write", file_path)

    def list_checkpoints(self, working_dir: str) -> list[dict]:
        return self._run(working_dir, "list_checkpoints", working_dir)

    def diff(self, working_dir: str, commit_hash: str) -> dict:
        return self._run(working_dir, "diff", working_dir, commit_hash)

    def session_diff(self, working_dir: str) -> dict:
        return self._run(working_dir, "session_diff", working_dir)

    def restore(self, working_dir: str, commit_hash: str, file_path: str | None = None, safe: bool = True) -> dict:
        return self._run(working_dir, "restore", working_dir, commit_hash, file_path, safe=safe)


__all__ = ["SessionCheckpoints"]
