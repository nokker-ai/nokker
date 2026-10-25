"""Sous-agents dans les sessions de code multi-utilisateur (docs/LOKKAL.md §7.28, même outil que §7.22).

`delegate(tasks, isolate, background)`, `delegate_status(ids?, wait_s?)`, `delegate_cancel(ids?)` sur le fil d'une
session vivante du locataire courant, pour son propriétaire seulement. Une `DelegateToolSource` PAR SESSION (ses
enfants, son plafond, son état sous `<state_dir>/<tenant>/<user>/subagents/<session>.json`) :

- l'enfant tourne dans le MÊME bac à sable que la session (l'environnement du fil parent prêté par
  `CodeToolSource.share_workspace` : aucun second pod ni container), dans son worktree `.nokker/worktrees/<id>` (git
  DANS le bac à sable) ; `isolate=true` sans dépôt git est refusé (jamais de repli sur le dossier : l'effet annoncé est
  WRITE) ; `isolate=false` = DESTRUCTIVE (carte forte), l'enfant travaille dans le dossier de la session ;
- son noyau est une copie de celui du parent prise quand il part (mêmes portes — `MinorGates` à l'extérieur,
  portes de la session, juge —, approbations, fournisseurs, plafonds), son registre celui du locataire courant filtré
  (§7.22) ; son fil `<fil>/sa-<id>` a les portes de la session (mode, règles du dépôt et des équipes) ;
- au plus `code.max_subagents` enfants vivants par session (premier plan, fond, en attente d'une carte), refus au-delà ;
- fermer (ou effacer) la session les arrête (`interrupted / cancelled`, noyau annulé, outils muets) et retire leur état ;
- événements `subagent.*` au propriétaire seulement (`_audience`), dans le locataire de la session ; une carte d'enfant
  est décidée par le propriétaire seulement (`BaseApprovals.thread_owner`) et s'exécute avec le noyau de l'enfant
  (`CodeSessions.subagent_kernel`), jamais celui du parent.
"""
from __future__ import annotations

import logging
from typing import Any

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.tenancy.context import NoTenant
from nokker.tenancy.resolve import is_account
from nokker.tools.delegate import SOURCE as DELEGATE_SOURCE
from nokker.tools.delegate import DelegateToolSource

logger = logging.getLogger("nokker.code_sessions.subagents")

TOOLS = ("delegate", "delegate_status", "delegate_cancel")
SUBAGENT_MARK = "/sa-"


def base_thread(thread_id: str) -> str:
    return thread_id.split(SUBAGENT_MARK, 1)[0]


def _delegate_effect(args: dict[str, Any]) -> Effect:
    """WRITE seulement confiné à des worktrees jetables (`isolate`, défaut vrai : refusé à l'appel sans dépôt git) ;
    DESTRUCTIVE quand l'enfant travaille dans le dossier de la session."""
    return Effect.DESTRUCTIVE if args.get("isolate", True) is False else Effect.WRITE


_PROTO = DelegateToolSource(lambda *a: None, None, background=True)
_SPECS = tuple(ToolSpec(name=s.name, description=s.description, parameters=s.parameters, effect=s.effect, core=False,
                        source=DELEGATE_SOURCE, fresh=s.fresh,
                        effect_of=_delegate_effect if s.name == "delegate" else None)
               for s in _PROTO.specs())
del _PROTO


class SessionCode:
    """La source de code d'UNE session vue par `DelegateToolSource` (`for_thread`) : le bac à sable du fil parent
    (`workspace`, pour git), et la source d'un enfant ouverte sur son worktree (`scoped`)."""

    def __init__(self, sessions: Any, thread_id: str) -> None:
        self.sessions, self.thread_id = sessions, base_thread(thread_id)

    def workspace(self, thread_id: str) -> Any:
        found = self.sessions.source_for_thread(base_thread(thread_id))
        if found is None:
            raise LookupError("pas de session de code sur ce fil")
        return found[1].workspace(found[0].thread_id)

    def scoped(self, subdir: str) -> ChildCode:
        return ChildCode(self.sessions, subdir)


class ChildCode:
    """Outils de code d'un enfant isolé : ceux de la session (`TenantCodeTools` : locataire, propriétaire, inactivité),
    dans le MÊME bac à sable, dossier de départ = son worktree. `close` rend son espace, jamais le bac à sable."""

    def __init__(self, sessions: Any, subdir: str) -> None:
        self.sessions, self.subdir = sessions, subdir
        self._threads: set[str] = set()

    def specs(self) -> list[ToolSpec]:
        return self.sessions.tools().specs()

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if SUBAGENT_MARK in ctx.thread_id:
            self._threads.add(ctx.thread_id)
            self.sessions.set_child_dir(ctx.thread_id, self.subdir)
        return self.sessions.tools().call(name, arguments, ctx)

    def close(self) -> None:
        for tid in list(self._threads):
            self.sessions.drop_child(tid)
        self._threads.clear()


class SessionSubagents:
    """`ToolSource` des comptes : `delegate*` routés vers la `DelegateToolSource` de la session du fil appelant."""

    core = False
    is_delegate_source = True  # un enfant ne la reçoit pas (profondeur 1, nokker/tools/delegate.py)

    def __init__(self, sessions: Any) -> None:
        self.sessions = sessions

    def specs(self) -> list[ToolSpec]:
        return list(_SPECS) if self.sessions.cfg.max_subagents > 0 else []

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def err(text: str) -> ToolResult:
            return ToolResult(call_id="", name=name, content=text, is_error=True)

        from nokker.code_sessions.service import NO_SESSION

        if name not in TOOLS or self.sessions.cfg.max_subagents <= 0:
            return err(f"outil inconnu : {name}")
        try:
            s = self.sessions.session_for_thread(ctx.thread_id)
        except NoTenant:
            s = None
        if s is None:
            return err(NO_SESSION)
        if ctx.sender is not None and is_account(ctx.sender) and ctx.sender != s.user_id:
            return err("REFUSÉ : cette session de code appartient à un autre utilisateur")
        if SUBAGENT_MARK in ctx.thread_id:
            return err("un sous-agent ne délègue pas, ne lit ni n'arrête d'autres sous-agents")
        source = self.sessions.delegate_for(s)
        if source is None:
            return err("sous-agents indisponibles sur ce noyau (pas de fabrique de noyau d'enfant)")
        out = source.call(name, arguments or {}, ctx)
        self.sessions.touch(s.id)
        return out


__all__ = ["ChildCode", "SessionCode", "SessionSubagents", "base_thread"]
