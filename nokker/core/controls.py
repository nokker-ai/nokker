"""Pause, reprise et steer d'un tour (docs/CLIENT-API.md §Pause et steer).

- **Pause** : un fil (`pause(thread_id)`) ou tout un compte (`pause_account(tenant, account)`, `account="*"` = tout le
  locataire : la machine Lokkal). La boucle (`core/loop.py`) la vérifie à ses points de contrôle — avant chaque appel
  modèle et avant chaque appel d'outil — et y ATTEND, dans le fil d'exécution du tour : rien n'est à moitié fait, rien
  n'est perdu (messages, cache de lectures, compteurs), le tour reprend au même point. Le tour garde la file de son fil
  (`turn_lock.py`) pendant la pause : ce qui arrive ensuite sur le fil attend derrière lui. Un fil en pause sans tour
  en cours le reste : son prochain tour s'arrête à son premier point de contrôle. Un sous-agent `<fil>/sa-…` suit la
  pause de son fil racine. Une annulation réveille le tour en pause, qui finit sur « annulé ».
- **Steer** : un message de l'utilisateur pour le tour EN COURS d'un fil ; rangé ici, il est consommé par la boucle au
  point de contrôle qui précède le prochain appel modèle (après les résultats de tous les outils déjà demandés, jamais
  au milieu d'un appel). Refusé (`None`) quand aucun tour ne tourne sur le fil. Un steer encore là quand le tour finit
  (réponse finale déjà en cours) est rendu par `end()` : la boucle publie `turn.steer_dropped`, le client le renvoie
  en message normal. `steer()` et `end()` passent sous le même verrou : un steer est consommé ou rendu, jamais perdu.

Un seul objet par processus, partagé par les copies du noyau (`dataclasses.replace` : sous-agents, sessions de code,
passages d'agents) — d'où un champ normal de `Kernel` et non un champ `init=False`.
"""
from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

ANY_ACCOUNT = "*"
SUBAGENT_MARK = "/sa-"  # même marque que `nokker/ledger/turn_cost.py::root_thread`
STEER_MAX_CHARS = 8000


@dataclass
class Steer:
    id: str
    thread_id: str
    text: str  # déjà caviardé par les invariants d'entrée
    at: float = field(default_factory=time.time)


class TurnControls:
    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._threads: set[str] = set()
        self._accounts: set[tuple[str, str]] = set()
        self._active: dict[str, int] = {}
        self._steers: dict[str, list[Steer]] = {}

    # ─────────────── pause ───────────────

    def pause(self, thread_id: str) -> None:
        with self._cond:
            self._threads.add(thread_id)

    def resume(self, thread_id: str) -> bool:
        """Rend vrai si le fil était en pause (à son niveau ; une pause du compte reste en vigueur)."""
        with self._cond:
            was = thread_id in self._threads
            self._threads.discard(thread_id)
            self._cond.notify_all()
            return was

    def pause_account(self, tenant: str, account: str) -> None:
        with self._cond:
            self._accounts.add((tenant, account))

    def resume_account(self, tenant: str, account: str) -> bool:
        with self._cond:
            was = (tenant, account) in self._accounts
            self._accounts.discard((tenant, account))
            self._cond.notify_all()
            return was

    def thread_paused(self, thread_id: str) -> bool:
        """Le fil (ou son fil racine) est en pause à SON niveau."""
        with self._cond:
            return self._thread_paused(thread_id)

    def account_paused(self, tenant: str, account: str | None) -> bool:
        with self._cond:
            return self._account_paused(tenant, account)

    def is_paused(self, thread_id: str, tenant: str, account: str | None) -> bool:
        with self._cond:
            return self._thread_paused(thread_id) or self._account_paused(tenant, account)

    def _thread_paused(self, thread_id: str) -> bool:
        return thread_id in self._threads or thread_id.split(SUBAGENT_MARK, 1)[0] in self._threads

    def _account_paused(self, tenant: str, account: str | None) -> bool:
        return (tenant, ANY_ACCOUNT) in self._accounts or (account is not None and (tenant, account) in self._accounts)

    def wait(self, thread_id: str, tenant: str, account: str | None, stop: Callable[[], bool],
             poll_s: float = 1.0) -> None:
        """Attend tant que le tour est en pause et que `stop()` est faux (annulation). Réveillé par `resume*` et
        `poke()` ; `poll_s` borne l'attente entre deux relectures (filet, si un réveil manque)."""
        with self._cond:
            while (self._thread_paused(thread_id) or self._account_paused(tenant, account)) and not stop():
                self._cond.wait(poll_s)

    def poke(self) -> None:
        """Réveille les tours en pause pour qu'ils relisent leur état (annulation)."""
        with self._cond:
            self._cond.notify_all()

    # ─────────────── tours en cours et steer ───────────────

    def begin(self, thread_id: str) -> None:
        with self._cond:
            self._active[thread_id] = self._active.get(thread_id, 0) + 1

    def end(self, thread_id: str) -> list[Steer]:
        """Fin d'un tour : rend les steers non consommés (le dernier tour du fil seulement, un tour réentrant les
        laisse au tour qui l'entoure)."""
        with self._cond:
            n = self._active.get(thread_id, 0) - 1
            if n > 0:
                self._active[thread_id] = n
                return []
            self._active.pop(thread_id, None)
            return self._steers.pop(thread_id, [])

    def running(self, thread_id: str) -> bool:
        with self._cond:
            return self._active.get(thread_id, 0) > 0

    def running_in(self, tenant_of: Callable[[str], str], tenant: str) -> int:
        with self._cond:
            return sum(1 for t in self._active if tenant_of(t) == tenant)

    def steer(self, thread_id: str, text: str) -> Steer | None:
        with self._cond:
            if self._active.get(thread_id, 0) <= 0:
                return None
            s = Steer(id="st_" + secrets.token_hex(8), thread_id=thread_id, text=text)
            self._steers.setdefault(thread_id, []).append(s)
            return s

    def take_steers(self, thread_id: str) -> list[Steer]:
        with self._cond:
            return self._steers.pop(thread_id, [])


__all__ = ["ANY_ACCOUNT", "STEER_MAX_CHARS", "Steer", "TurnControls"]
