"""File par fil (docs/LOKKAL.md §7.4.9) : un tour à la fois par fil, dans l'ordre d'arrivée.

Le store attribue l'index d'un tour à son début et l'historique n'y entre qu'à sa fin : deux tours du même fil en
parallèle prennent le même index et le second ne voit pas le premier (essai du client des éditeurs, X12, 25.10 : une
décision envoyée pendant le tour qui avait posé la carte a lancé une suite « sans le contexte »). `TurnLock` est un
verrou RÉENTRANT (un tour lancé depuis le fil d'exécution qui le tient déjà passe) et ÉQUITABLE (premier arrivé, premier
servi, contrairement à `threading.RLock`) ; `reserve()` prend une place dans la file sans attendre, pour qu'un travail de
fond (suite d'une décision) garde le rang de sa demande même si son fil d'exécution démarre plus tard.
"""
from __future__ import annotations

import threading
import time
from collections import deque


class Ticket:
    """Une place dans la file d'un fil, prise par `TurnLock.reserve()` et consommée par `acquire(ticket=…)`."""

    __slots__ = ("immediate",)

    def __init__(self, immediate: bool) -> None:
        self.immediate = immediate  # vrai si rien ne tournait ni n'attendait sur le fil à la réservation


class TurnLock:
    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._owner: int | None = None
        self._count = 0
        self._queue: deque[Ticket] = deque()

    # ─────────────── état ───────────────

    def busy(self) -> bool:
        """Un tour tourne ou attend sur ce fil."""
        with self._cond:
            return self._owner is not None or bool(self._queue)

    def waiting(self) -> int:
        with self._cond:
            return len(self._queue)

    # ─────────────── file ───────────────

    def reserve(self) -> Ticket:
        """Prend une place en fin de file, sans attendre. La place DOIT être consommée (`acquire(ticket=…)`) ou rendue
        (`cancel`) : sinon le fil reste bloqué."""
        with self._cond:
            t = Ticket(immediate=self._owner is None and not self._queue)
            self._queue.append(t)
            return t

    def cancel(self, ticket: Ticket) -> None:
        with self._cond:
            try:
                self._queue.remove(ticket)
            except ValueError:
                return
            self._cond.notify_all()

    def acquire(self, blocking: bool = True, timeout: float = -1, *, ticket: Ticket | None = None) -> bool:
        me = threading.get_ident()
        with self._cond:
            if self._owner == me:  # réentrant : le tour qui tient le fil relance un tour (ou un outil) sur lui
                if ticket is not None:
                    self._remove(ticket)
                self._count += 1
                return True
            if ticket is None:
                if not blocking:  # sans attendre : seulement si personne ne tourne NI n'attend (pas de saut de file)
                    if self._owner is None and not self._queue:
                        self._owner, self._count = me, 1
                        return True
                    return False
                ticket = Ticket(immediate=False)
                self._queue.append(ticket)
            deadline = None if timeout is None or timeout < 0 else time.monotonic() + timeout
            while not (self._owner is None and self._queue and self._queue[0] is ticket):
                if ticket not in self._queue:
                    return False  # place rendue entre-temps (`cancel`)
                if not blocking:
                    self._remove(ticket)
                    return False
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    self._remove(ticket)
                    return False
                self._cond.wait(left)
            self._queue.popleft()
            self._owner, self._count = me, 1
            return True

    def release(self) -> None:
        with self._cond:
            if self._owner != threading.get_ident():
                raise RuntimeError("TurnLock.release() par un fil d'exécution qui ne le tient pas")
            self._count -= 1
            if self._count == 0:
                self._owner = None
                self._cond.notify_all()

    def _remove(self, ticket: Ticket) -> None:
        try:
            self._queue.remove(ticket)
        except ValueError:
            return
        self._cond.notify_all()

    def __enter__(self) -> TurnLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class TurnLocks:
    """Un `TurnLock` par fil (jamais libéré : un objet par fil vu par le processus, comme les verrous d'avant)."""

    def __init__(self) -> None:
        self._locks: dict[str, TurnLock] = {}
        self._guard = threading.Lock()

    def get(self, thread_id: str) -> TurnLock:
        with self._guard:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = self._locks[thread_id] = TurnLock()
            return lock

    def active(self) -> int:
        """Fils dont un tour tourne ou attend."""
        with self._guard:
            locks = list(self._locks.values())
        return sum(1 for lock in locks if lock.busy())


__all__ = ["Ticket", "TurnLock", "TurnLocks"]
