"""Bus d'événements en mémoire pour `GET /v1/events` (SSE, docs/CLIENT-API.md) — un seul processus
serveur : le besoin P1 est « un opérateur voit la carte sur tous ses clients ouverts (CLI, web,
Telegram) en même temps », pas un bus distribué multi-instance (hors scope, limite documentée ici).

`nokker/approvals/base.py::BaseApprovals.on_event` publie ici `approval.requested`/`approval.decided` ;
`nokker/bubbles/hub.py::BubbleHub` publie `turn.*`/`tool.*`/`thread.updated`/`bubble.*`/`session.message`
(docs/BUBBLES.md §6) avec un champ privé `_audience` (identités autorisées) que `api/server.py` utilise pour
filtrer par abonné puis retire avant l'envoi.

Reprise (`Last-Event-ID`, demandé par le chantier clients `nokker-apps`) : chaque événement publié reçoit
un `id` entier croissant et est gardé dans un tampon BORNÉ (`max_buffer` événements, `max_age_s`
d'ancienneté) ; un nouvel abonné qui présente `Last-Event-ID` reçoit d'abord les événements manqués
dans cette fenêtre (rien de plus ancien — fenêtre bornée, pas un journal infini), puis bascule en direct.

Multi-locataire (docs/TENANCY.md) : chaque événement porte `_tenant`, le locataire courant au moment de la
publication (ou celui que l'éditeur a posé) ; `api/server.py` ne le livre qu'aux abonnés du même locataire."""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from nokker.tenancy.context import current_tenant_or_none


@dataclass
class StoredEvent:
    id: int
    event_type: str
    data: dict
    at: float


@dataclass
class EventBus:
    max_buffer: int = 1000
    max_age_s: float = 3600.0
    clock: Callable[[], float] = time.time
    _subscribers: list[queue.Queue] = field(default_factory=list)
    _buffer: list[StoredEvent] = field(default_factory=list)
    _next_id: int = field(default=1)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event_type: str, data: dict) -> StoredEvent:
        with self._lock:
            payload = dict(data)
            payload.setdefault("_tenant", current_tenant_or_none())
            ev = StoredEvent(id=self._next_id, event_type=event_type, data=payload, at=self.clock())
            self._next_id += 1
            self._buffer.append(ev)
            self._trim_locked()
            subs = list(self._subscribers)
        for q in subs:
            q.put(ev)
        return ev

    def _trim_locked(self) -> None:
        cutoff = self.clock() - self.max_age_s
        self._buffer = [e for e in self._buffer if e.at >= cutoff][-self.max_buffer:]

    def replay_since(self, last_event_id: int) -> list[StoredEvent]:
        """Événements d'id STRICTEMENT supérieur à `last_event_id`, dans la fenêtre bornée (au-delà, un
        client qui a manqué trop longtemps doit relire l'état via `GET /v1/approvals` — pas de journal
        infini)."""
        with self._lock:
            self._trim_locked()
            return [e for e in self._buffer if e.id > last_event_id]


__all__ = ["EventBus", "StoredEvent"]
