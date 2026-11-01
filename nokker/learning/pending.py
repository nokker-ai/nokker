"""Écritures de mémoire en attente de validation (quarantaine A-3 de docs/PLAN-P1.md, docs/LEARNING.md §3).

Drapeau `memory_pending` (défaut false : rien ne change). Allumé, une écriture de mémoire durable ne va plus droit en
mémoire : elle est rangée `pending` dans le compte, avec sa provenance, et n'entre dans AUCUN contexte (ni noyau
`[Mémoire du projet]`, ni rappel, ni index) avant que l'humain l'approuve. Deux portes d'entrée :

  - `memory_note` du modèle (mémoire de projet, `ProjectMemory.pending`) : mêmes validations qu'avant (portée user =
    les mots de l'utilisateur dans CE tour), puis la note attend ; approuvée, elle est écrite telle quelle, avec son
    origine (fil, tour) ;
  - l'écriture longue (`MemoryPort.propose`, faits extraits en fin d'épisode par la consolidation) : chaque fait
    attend ; approuvé, il suit le chemin d'origine (`commit_fact`).

Refusée, une note ne s'écrit jamais ; elle reste au journal (`memory.rejected`). Un oubli (`memory forget`) ne passe
pas par ici : retirer ne demande pas de validation."""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nokker.learning.store import JournalEntry, PendingNote

log = logging.getLogger("nokker.learning.pending")

MAX_FACT = 1500


class PendingError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class PendingMemory:
    """`store` : `nokker/learning/store.py`. `commit_note(note)` écrit une note `memory_note` approuvée et rend son nom ;
    `commit_fact(note)` fait suivre un fait approuvé de `propose` à sa destination d'origine. `account_of_thread(fil)` :
    le compte d'un fil (Lokkal : le propriétaire)."""

    store: Any
    account_of_thread: Callable[[str], str | None]
    commit_note: Callable[[PendingNote], str | None] | None = None
    commit_fact: Callable[[PendingNote], str | None] | None = None
    emit: Callable[[str, dict[str, Any]], Any] | None = None
    now_fn: Callable[[], str] = field(default=_now)

    def _publish(self, event: str, data: dict[str, Any]) -> None:
        if self.emit is None:
            return
        try:
            self.emit(event, {**data, "_audience": [data["account"]]})  # le compte seul, dans son locataire
        except Exception:
            log.debug("emit %s a levé", event, exc_info=True)

    def _hold(self, account: str, note: PendingNote) -> PendingNote:
        self.store.put_pending(account, note)
        self.store.journal_append(account, JournalEntry(
            kind="memory.pending", subject=note.id, at=note.at, actor="agent", thread_id=note.thread_id,
            turn_index=note.turn_index, detail={"source": note.source, "kind": note.kind, "scope": note.scope,
                                                "title": note.title}))
        self._publish("memory.pending", {"account": account, "id": note.id, "source": note.source, "kind": note.kind,
                                         "scope": note.scope, "title": note.title, "thread_id": note.thread_id,
                                         "turn_index": note.turn_index})
        return note

    def hold_note(self, *, thread_id: str, turn_index: int | None, kind: str, scope: str, title: str, text: str,
                  derived_from: str, project: str, links: list[str]) -> PendingNote:
        """`memory_note` validée par la mémoire de projet : rangée en attente au lieu d'être écrite."""
        account = self.account_of_thread(thread_id)
        if not account:
            raise PendingError(403, "no_account", "pas de compte pour ce fil")
        for held in self.store.list_pending(account):  # même note déjà en attente : pas de doublon
            if held.kind == kind and held.project == project and " ".join(held.text.split()) == " ".join(text.split()):
                return held
        return self._hold(account, PendingNote(
            id=f"mp_{uuid.uuid4().hex[:16]}", source="memory_note", kind=kind, title=title, text=text, scope=scope,
            derived_from=derived_from, project=project, links=list(links), thread_id=thread_id,
            turn_index=turn_index, at=self.now_fn()))

    def hold_facts(self, thread_id: str, facts: list[str]) -> list[PendingNote]:
        """`MemoryPort.propose` : un fait = une note en attente (titre = début du fait)."""
        account = self.account_of_thread(thread_id)
        if not account:
            return []
        out = []
        for fact in facts:
            text = " ".join(str(fact or "").split())[:MAX_FACT]
            if not text:
                continue
            title = text if len(text) <= 120 else text[:117].rstrip() + "…"
            out.append(self._hold(account, PendingNote(
                id=f"mp_{uuid.uuid4().hex[:16]}", source="propose", kind="fact", title=title, text=text,
                scope="project", derived_from="agent", thread_id=thread_id, at=self.now_fn())))
        return out

    # ── côté humain ──
    def list(self, account: str, status: str | None = "pending") -> list[PendingNote]:
        return self.store.list_pending(account, status)

    def decide(self, account: str, note_id: str, approve: bool, by: str) -> PendingNote:
        note = self.store.get_pending(account, note_id)
        if note is None:
            raise PendingError(404, "unknown_note", "note en attente inconnue")
        if note.status != "pending":
            raise PendingError(409, "already_decided", f"note déjà {note.status}")
        result = None
        if approve:
            commit = self.commit_note if note.source == "memory_note" else self.commit_fact
            if commit is None:
                raise PendingError(503, "no_memory", "la mémoire de destination n'est pas disponible ici : la note "
                                                     "reste en attente")
            result = commit(note)
        at = self.now_fn()
        done = self.store.decide_pending(account, note_id, "approved" if approve else "rejected", by, at, result)
        if done is None:  # décidée entre-temps par un autre geste
            raise PendingError(409, "already_decided", "note déjà décidée")
        kind = "memory.approved" if approve else "memory.rejected"
        self.store.journal_append(account, JournalEntry(
            kind=kind, subject=note_id, at=at, actor="owner", thread_id=note.thread_id, turn_index=note.turn_index,
            detail={"source": note.source, "kind": note.kind, "scope": note.scope, "title": note.title,
                    **({"note": result} if result else {})}))
        self._publish(kind, {"account": account, "id": note_id, "kind": note.kind, "scope": note.scope,
                             "title": note.title, "note": result})
        return done


@dataclass
class PendingMemoryPort:
    """`MemoryPort` dont l'écriture longue (`propose`) passe par la quarantaine ; le reste (rappel, outils, état)
    délègue tel quel à la mémoire d'origine."""

    inner: Any
    pending: PendingMemory

    def propose(self, thread_id: str, facts: list[str]) -> None:
        try:
            self.pending.hold_facts(thread_id, facts)
        except Exception:
            log.warning("quarantaine des faits en échec (fil %s)", thread_id, exc_info=True)

    def __getattr__(self, name: str) -> Any:
        if name in ("inner", "pending"):
            raise AttributeError(name)
        return getattr(self.inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Un branchement posé plus tard (`port.internal = …`, `tenant_memory`) va à la mémoire d'origine.
        if name in ("inner", "pending"):
            object.__setattr__(self, name, value)
        else:
            setattr(self.inner, name, value)


__all__ = ["PendingError", "PendingMemory", "PendingMemoryPort"]
