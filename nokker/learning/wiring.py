"""Branchement de l'apprentissage (docs/LEARNING.md §1) : drapeaux, magasins, outils, portes, quarantaine, invitation.

Drapeaux, tous éteints par défaut (rien ne change : ni outil, ni bloc, ni route) :

| Lokkal (`local.*`)            | nokker.ai (racine de la config) | Effet                                              |
|-------------------------------|---------------------------------|----------------------------------------------------|
| `learning`                    | `learning`                      | outils skill_create/update/history/revert, carte, routes /v1/learning/* |
| `learning_nudges`             | `learning_nudges`               | ligne fixe au tour suivant d'un tour réussi riche en outils |
| `memory_pending`              | `memory_pending`                | memory_note et l'écriture longue en quarantaine ; routes /v1/memory/pending* |

Réglages : `learning_nudge_min_tools` (8), `learning_nudge_cooldown` (20) ; Lokkal : `learning_dir` (défaut
`<état>/learning`)."""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nokker.core.ports import ToolContext
from nokker.learning.http import Learning
from nokker.learning.nudge import DEFAULT_COOLDOWN, DEFAULT_MIN_TOOLS, Nudger
from nokker.learning.pending import PendingMemory, PendingMemoryPort
from nokker.learning.skills import LEARNED_FAMILY, LearnedSkills, LearningGates, LearningToolSource

log = logging.getLogger("nokker.learning.wiring")

LOCAL_ACCOUNT = "local-owner"  # = nokker.local.api.LOCAL_OWNER : une machine Lokkal = un propriétaire


def _flags(conf: dict[str, Any]) -> tuple[bool, bool, bool]:
    return bool(conf.get("learning", False)), bool(conf.get("learning_nudges", False)), \
        bool(conf.get("memory_pending", False))


def nudger_from(conf: dict[str, Any], sender_prefix: str | None = None) -> Nudger | None:
    if not (bool(conf.get("learning_nudges", False)) and bool(conf.get("learning", False))):
        return None  # la ligne nomme skill_create : jamais sans l'outil
    return Nudger(min_tools=int(conf.get("learning_nudge_min_tools", DEFAULT_MIN_TOOLS)),
                  cooldown_turns=int(conf.get("learning_nudge_cooldown", DEFAULT_COOLDOWN)), sender_prefix=sender_prefix)


# ─────────────────────────────── Lokkal ───────────────────────────────


def local_learning(local: dict[str, Any], state_dir: Path | None, home: Path | None,
                   project_memory: Any = None) -> Learning | None:
    """Lokkal : magasin en fichiers sous l'état du noyau ; skills actifs écrits sous `<home>/skills/learned`. None si
    ni `learning` ni `memory_pending`."""
    learning, _, pending = _flags(local)
    if not (learning or pending):
        return None
    from nokker.learning.store import FileLearningStore

    base = Path(state_dir or "~/.config/nokker").expanduser()
    store = FileLearningStore(Path(local.get("learning_dir") or base / "learning").expanduser())
    skills_root = Path(home or os.environ.get("NOKKER_HOME") or "~/.nokker").expanduser() / "skills" / LEARNED_FAMILY
    lrn = Learning(store=store)
    if learning:
        lrn.skills = LearnedSkills(store=store, skills_dir=lambda _account: skills_root)
    if pending:
        commit = project_memory.commit_pending if project_memory is not None else None
        lrn.pending = PendingMemory(store=store, account_of_thread=lambda _tid: LOCAL_ACCOUNT, commit_note=commit,
                                    commit_fact=commit)
        if project_memory is not None:
            project_memory.pending = lrn.pending
    return lrn


def local_tool_source(lrn: Learning | None, catalog: Any) -> LearningToolSource | None:
    """Outils des skills appris dans Lokkal ; `catalog` : la `SkillToolSource` des sessions (un nom pris par un skill
    fourni, installé ou du dépôt est refusé)."""
    if lrn is None or lrn.skills is None:
        return None
    learned_root = lrn.skills.skills_dir(LOCAL_ACCOUNT)

    def taken(_account: str, name: str) -> bool:
        entry, hidden = catalog._find(name)
        others = [e for e in ([entry] if entry is not None else []) + list(hidden)
                  if learned_root is None or os.path.realpath(e.path.parent) != os.path.realpath(learned_root)]
        return bool(others)

    lrn.skills.taken = taken
    return LearningToolSource(service=lrn.skills, account_of=lambda _ctx: LOCAL_ACCOUNT)


# ─────────────────────────────── nokker.ai ───────────────────────────────


def cloud_account_of(account_for_tenant: Callable[[str], Any] | None = None) -> Callable[[ToolContext], str | None]:
    """Le compte de l'appel : l'expéditeur `us_…` du tour (ou le décideur de la carte), sinon le compte du locataire
    courant (en bêta, un compte = un locataire) ; le locataire interne n'apprend pas par ici (None)."""
    from nokker.tenancy.context import INTERNAL_TENANT, current_tenant_or_none

    def account_of(ctx: ToolContext) -> str | None:
        sender = str(getattr(ctx, "sender", "") or "")
        if sender.startswith("us_"):
            return sender
        tid = current_tenant_or_none()
        if account_for_tenant is None or not tid or tid == INTERNAL_TENANT:
            return None
        try:
            acc = account_for_tenant(tid)
        except Exception:  # noqa: BLE001 — compte illisible : refus
            return None
        acc = getattr(acc, "account_id", acc)
        return acc if isinstance(acc, str) and acc.startswith("us_") else None

    return account_of


def cloud_learning(config: Any, account_for_tenant: Callable[[str], Any] | None = None) -> Learning | None:
    learning, _, pending = bool(getattr(config, "learning", False)), False, bool(getattr(config, "memory_pending", False))
    if not (learning or pending):
        return None
    from nokker.learning.store import MemoryLearningStore

    dsn = config.pg_dsn()
    if dsn:
        from nokker.learning.pg import PgLearningStore

        store: Any = PgLearningStore(dsn)
    else:
        store = MemoryLearningStore()
    lrn = Learning(store=store)
    if learning:
        lrn.skills = LearnedSkills(store=store)

    if pending:
        from nokker.tenancy.context import tenant_of_thread

        def account_of_thread(thread_id: str) -> str | None:
            if account_for_tenant is None:
                return None
            acc = account_for_tenant(tenant_of_thread(thread_id))
            return getattr(acc, "account_id", acc) if acc else None

        lrn.pending = PendingMemory(store=store, account_of_thread=account_of_thread)
    return lrn


# ─────────────────────────────── noyau ───────────────────────────────


def install(kernel: Any, lrn: Learning | None, *, account_of: Callable[[ToolContext], str | None],
            nudger: Nudger | None = None, emit: Any = None) -> None:
    """Après la construction complète des portes : carte obligatoire (enveloppe la plus externe), caviardage des
    écritures, événements ; quarantaine de l'écriture longue ; invitation de fin de tour."""
    if nudger is not None:
        kernel.learning = nudger
    if lrn is None:
        return
    if lrn.skills is not None:
        lrn.skills.emit = emit
        lrn.skills.scrub = kernel.invariants.scrub_output
        kernel.gates = LearningGates(inner=kernel.gates, service=lrn.skills, account_of=account_of)
    if lrn.pending is not None:
        lrn.pending.emit = emit
        inner = kernel.memory
        if lrn.pending.commit_fact is None:
            lrn.pending.commit_fact = lambda note: (inner.propose(note.thread_id, [note.text]), None)[1]
        kernel.memory = PendingMemoryPort(inner=inner, pending=lrn.pending)


__all__ = ["LOCAL_ACCOUNT", "cloud_account_of", "cloud_learning", "install", "local_learning", "local_tool_source",
           "nudger_from"]
