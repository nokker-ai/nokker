"""Sites connectés — gouverneur : allure, quotas, pauses, un pilote par connecteur (docs/CONNECTED-SITES.md §3.3, §6).

- **Un pilote** : un verrou par connecteur ; un appel qui attend plus de `busy_wait_s` rend `busy`.
- **Allure** : écart tiré entre `gap_s` (6-20 s) entre deux pages d'un connecteur, plus une pause longue toutes les
  8-15 pages. `sleep` est injectable (tests : aucun sommeil réel).
- **Quotas** : par connecteur (= par compte du site, partagés entre fils, agents et bénéficiaires), par heure, par
  jour et par semaine, plafonds de la recette ; l'utilisateur peut baisser, jamais monter.
- **Pauses** : 1er signal → 24 h ; 2e en 7 jours → 7 jours ; 3e → jusqu'à levée manuelle. Relues avant CHAQUE geste.
- **Heures d'activité** : 7 h-23 h (heure de la machine) pour un agent de fond ; un tour en direct passe toujours.
"""
from __future__ import annotations

import random
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field

DAY = 86400.0
WEEK = 7 * DAY
MANUAL = float("inf")


class GovernorError(RuntimeError):
    def __init__(self, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


@dataclass
class Limits:
    hour: int
    day: int
    week: int | None = None


@dataclass
class _Pause:
    until: float
    signal: str


@dataclass
class Governor:
    limits: dict[str, Limits]  # compteur -> plafonds (recette)
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)
    gap_s: tuple[float, float] = (6.0, 20.0)
    long_pause_every: tuple[int, int] = (8, 15)
    long_pause_s: tuple[float, float] = (60.0, 240.0)
    busy_wait_s: float = 60.0
    active_hours: tuple[int, int] = (7, 23)
    _events: dict[tuple[str, str], deque] = field(default_factory=lambda: defaultdict(deque), init=False)
    _lowered: dict[tuple[str, str], Limits] = field(default_factory=dict, init=False)
    _pauses: dict[str, _Pause] = field(default_factory=dict, init=False)
    _signals: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list), init=False)
    _locks: dict[str, threading.Lock] = field(default_factory=lambda: defaultdict(threading.Lock), init=False)
    _last_page: dict[str, float] = field(default_factory=dict, init=False)
    _pages_since_long: dict[str, int] = field(default_factory=lambda: defaultdict(int), init=False)
    _next_long: dict[str, int] = field(default_factory=dict, init=False)
    _mx: threading.Lock = field(default_factory=threading.Lock, init=False)

    # ── un pilote ──
    @contextmanager
    def driver(self, cid: str):
        lock = self._locks[cid]
        if not lock.acquire(timeout=self.busy_wait_s):
            raise GovernorError("busy", "déjà en cours d'utilisation")
        try:
            yield
        finally:
            lock.release()

    # ── pauses ──
    def check_pause(self, cid: str) -> None:
        p = self._pauses.get(cid)
        if p is not None and self.clock() < p.until:
            raise GovernorError("site_paused", "en pause", until=p.until, signal=p.signal)

    def signal(self, cid: str, signal: str) -> float:
        """Un signal du site (challenge, activité inhabituelle, limite…) : pause escaladée. Rend l'échéance."""
        now = self.clock()
        recent = [t for t in self._signals[cid] if now - t < WEEK]
        recent.append(now)
        self._signals[cid] = recent
        duration = DAY if len(recent) == 1 else (WEEK if len(recent) == 2 else MANUAL)
        until = now + duration
        self._pauses[cid] = _Pause(until, signal)
        return until

    def lift_pause(self, cid: str) -> None:  # utilisateur seulement (route), jamais le modèle
        self._pauses.pop(cid, None)

    def paused_until(self, cid: str) -> tuple[float, str] | None:
        p = self._pauses.get(cid)
        return (p.until, p.signal) if p is not None and self.clock() < p.until else None

    # ── quotas ──
    def lower(self, cid: str, counter: str, limits: Limits) -> None:
        base = self.limits[counter]
        self._lowered[(cid, counter)] = Limits(min(limits.hour, base.hour), min(limits.day, base.day),
                                               min(limits.week or base.week or 10**9, base.week or 10**9))

    def _limits(self, cid: str, counter: str) -> Limits:
        return self._lowered.get((cid, counter)) or self.limits[counter]

    def check(self, cid: str, counter: str, n: int = 1) -> None:
        if counter not in self.limits:
            return
        lim = self._limits(cid, counter)
        now = self.clock()
        ev = self._events[(cid, counter)]
        while ev and now - ev[0] >= WEEK:
            ev.popleft()
        for window, cap, per in ((3600.0, lim.hour, "heure"), (DAY, lim.day, "jour"), (WEEK, lim.week, "semaine")):
            if cap is None:
                continue
            inside = [t for t in ev if now - t < window]
            if len(inside) + n > cap:
                retry_at = inside[len(inside) + n - cap - 1] + window if inside else now
                raise GovernorError("quota", "plafond atteint", counter=counter, cap=cap, per=per, retry_at=retry_at)

    def record(self, cid: str, counter: str, n: int = 1) -> None:
        if counter in self.limits:
            self._events[(cid, counter)].extend([self.clock()] * n)

    def used(self, cid: str, counter: str, window: float = DAY) -> int:
        now = self.clock()
        return sum(1 for t in self._events[(cid, counter)] if now - t < window)

    # ── heures, allure ──
    def check_hours(self, live: bool) -> None:
        if live:
            return
        hour = time.localtime(self.clock()).tm_hour
        if not (self.active_hours[0] <= hour < self.active_hours[1]):
            raise GovernorError("quiet_hours", "hors des heures d'activité")

    def page(self, cid: str) -> None:
        """Avant une page : quota `pages`, puis écart humain depuis la page précédente du connecteur."""
        self.check(cid, "pages")
        with self._mx:
            last = self._last_page.get(cid)
            wait = 0.0
            if last is not None:
                gap = self.rng.uniform(*self.gap_s)
                wait = max(0.0, last + gap - self.clock())
            n = self._pages_since_long[cid] + 1
            if n >= self._next_long.setdefault(cid, self.rng.randint(*self.long_pause_every)):
                wait += self.rng.uniform(*self.long_pause_s)
                n = 0
                self._next_long[cid] = self.rng.randint(*self.long_pause_every)
            self._pages_since_long[cid] = n
        if wait > 0:
            self.sleep(wait)
        self.record(cid, "pages")
        self._last_page[cid] = self.clock()


LINKEDIN_LIMITS = {
    "pages": Limits(30, 150),
    "searches": Limits(6, 25),
    "profiles": Limits(10, 40),
    "messages": Limits(5, 20),
    "comments": Limits(4, 12),
    "invitations": Limits(3, 10, 50),
    "reactions": Limits(10, 40),
    "follows": Limits(3, 10),
    "posts": Limits(1, 2),
    "logins": Limits(2, 2),  # reconnexions automatiques : 2 par 24 h au plus (§4.4)
}

__all__ = ["Governor", "GovernorError", "LINKEDIN_LIMITS", "Limits", "MANUAL"]
