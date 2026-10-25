"""Logique commune des backends de fils (`memory.InMemoryThreads`, `pg.PgThreads`) — même découpage que
`nokker/approvals/base.py` : la sous-classe ne fournit que des primitives de stockage.

Boîte de contexte (`push_context`/`drain_context`, docs/BUBBLES.md §3-§4) : blocs système compacts
(« [Bulle reçue de …] », « [Message de la session …] ») mis en attente pour le PROCHAIN tour du fil, vidés
par le noyau au début de ce tour (`Kernel.observer.context_for`). Les blocs consommés sont gardés dans le
tour lui-même (`Turn.injected`, sérialisé par le store) : le fil est permanent, le contexte reçu aussi."""
from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from nokker.threads.types import Thread, ThreadNotFound

TITLE_FROM_TEXT_CHARS = 48


def new_thread_id() -> str:
    return "th_" + secrets.token_hex(6)


def provisional_title(text: str) -> str:
    t = " ".join((text or "").split())
    return t[:TITLE_FROM_TEXT_CHARS] or "Nouvelle session"


@dataclass
class BaseThreads:
    clock: Callable[[], float] = time.time

    # ─────────── primitives de stockage ───────────
    def _load(self, thread_id: str) -> Thread | None:
        raise NotImplementedError

    def _store(self, thread: Thread) -> None:
        raise NotImplementedError

    def _list_all(self) -> list[Thread]:
        raise NotImplementedError

    def _delete(self, thread_id: str) -> None:
        raise NotImplementedError

    def _push(self, thread_id: str, block: str) -> None:
        raise NotImplementedError

    def _drain(self, thread_id: str) -> list[str]:
        raise NotImplementedError

    # ─────────── logique ───────────
    def get(self, thread_id: str) -> Thread | None:
        return self._load(thread_id)

    def require(self, thread_id: str) -> Thread:
        t = self._load(thread_id)
        if t is None:
            raise ThreadNotFound(thread_id)
        return t

    def create(self, title: str, owner: str | None, *, thread_id: str | None = None,
               members: list[str] | None = None, origin: dict | None = None) -> Thread:
        now = self.clock()
        t = Thread(id=thread_id or new_thread_id(), title=(title or "").strip()[:200] or "Nouvelle session",
                   owner=owner, members=[m for m in (members or []) if m and m != owner],
                   created_at=now, updated_at=now, origin=dict(origin) if origin else None)
        self._store(t)
        return t

    def ensure(self, thread_id: str, owner: str | None, first_text: str) -> tuple[Thread, bool]:
        """Fil à la volée pour un `X-Session-Id` inconnu (rétrocompatible passerelle amont) : (fil, créé ?)."""
        t = self._load(thread_id)
        if t is not None:
            return t, False
        return self.create(provisional_title(first_text), owner, thread_id=thread_id), True

    def list_for(self, user: str | None) -> list[Thread]:
        items = [t for t in self._list_all() if t.is_member(user)]
        items.sort(key=lambda t: t.updated_at, reverse=True)
        return items

    def update(self, thread_id: str, *, title: str | None = None, members: list[str] | None = None,
               working: bool | None = None, last_snippet: str | None = None, touch: bool = True) -> Thread:
        t = self.require(thread_id)
        if title is not None:
            t.title = title.strip()[:200] or t.title
        if members is not None:
            t.members = [m for m in dict.fromkeys(members) if m and m != t.owner]
        if working is not None:
            t.working = working
        if last_snippet is not None:
            t.last_snippet = " ".join(last_snippet.split())[:140]
        if touch:
            t.updated_at = self.clock()
        self._store(t)
        return t

    def delete(self, thread_id: str) -> Thread:
        """Retire le fil (et sa boîte de contexte). L'historique des tours (store/ledger) n'est pas touché."""
        t = self.require(thread_id)
        self._delete(thread_id)
        return t

    def push_context(self, thread_id: str, block: str) -> None:
        self._push(thread_id, block)

    def drain_context(self, thread_id: str) -> list[str]:
        return self._drain(thread_id)


__all__ = ["BaseThreads", "new_thread_id", "provisional_title"]
