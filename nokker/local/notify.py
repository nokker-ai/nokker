"""Côté machine : Lokkal prévient nokker.ai, SANS contenu (docs/LOKKAL-PHONE.md §1.2 point 5, contrat C4).

`MachineNotify` observe le bus d'événements de la machine (`EventBus.observers`, comme la présence) et envoie
`POST /v1/gateway/notify {kind, thread_id, title|null}` avec le jeton de machine :
- `turn.done` → `turn_done`, au plus un par fil et par minute (un échange suivi devant l'écran ne sonne pas à
  chaque tour). Ni les fils des passages d'agent (ils ont `agent_done`), ni les tours annulés ou refusés ;
- `approval.requested` → `awaiting_approval` (le fil de la carte) ;
- `agent.run` terminé → `agent_done` (`succeeded`) ou `agent_failed` (`failed`), titre = nom de l'agent.

Règles :
- **Aucun extrait** : seulement le genre, l'id du fil et, sous la règle du miroir, un titre. Le titre n'est envoyé que
  si les fils de la machine sont en miroir (`mirrored()`), sinon `null`.
- **30 par minute au plus** (la limite de nokker.ai) : au-delà, la notification est abandonnée ici, jamais mise en
  file sans fin. Un 429 de nokker.ai suspend l'envoi jusqu'à `Retry-After`.
- L'envoi passe par un fil de fond borné (64 en attente) : le bus n'attend jamais le réseau. Machine non liée (pas de
  jeton) : rien.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("nokker.local.notify")

RATE_MAX = 30
RATE_WINDOW_S = 60.0
TURN_DEBOUNCE_S = 60.0
QUEUE_MAX = 64
_DONE = {"succeeded": "agent_done", "failed": "agent_failed"}


@dataclass
class MachineNotify:
    base_url: str  # https://app.nokker.ai
    token: Callable[[], str | None]  # jeton de machine ; None ou vide : machine non liée, rien n'est envoyé
    title_of: Callable[[str], str | None] = field(default=lambda tid: None)  # titre d'un fil local
    agent_name: Callable[[str], str | None] = field(default=lambda aid: None)  # nom d'un agent local
    mirrored: Callable[[], bool] = field(default=lambda: False)  # règle du miroir : seule condition d'un titre
    is_agent_thread: Callable[[str], bool] = field(default=lambda tid: False)
    client: Any = None  # httpx.Client injectable (tests)
    sync: bool = False  # tests : envoi tout de suite, sans fil de fond
    clock: Callable[[], float] = time.monotonic
    _sent: deque = field(default_factory=deque, init=False, repr=False)
    _last_turn: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _paused_until: float = field(default=0.0, init=False, repr=False)
    _q: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=QUEUE_MAX), init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _worker: threading.Thread | None = field(default=None, init=False, repr=False)

    # ── observateur du bus (hors de son verrou ; ne lève jamais) ──
    def observe(self, event_type: str, data: dict[str, Any]) -> None:
        from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

        try:
            with tenant_scope(data.get("_tenant") or INTERNAL_TENANT):  # titres lus dans le locataire de l'événement
                event = self._event(event_type, data)
            if event is not None:
                self._enqueue(event)
        except Exception:  # noqa: BLE001 — une notification ne casse jamais le bus
            logger.debug("notification : événement %s ignoré", event_type, exc_info=True)

    def _event(self, event_type: str, data: dict[str, Any]) -> dict[str, Any] | None:
        if event_type == "turn.done":
            tid = str(data.get("thread_id") or "")
            if not tid or data.get("cancelled") or data.get("refused") or self.is_agent_thread(tid):
                return None
            now = self.clock()
            with self._lock:
                if now - self._last_turn.get(tid, -1e18) < TURN_DEBOUNCE_S:
                    return None
                self._last_turn[tid] = now
                if len(self._last_turn) > 2000:  # mémoire bornée
                    for k in sorted(self._last_turn, key=self._last_turn.get)[:1000]:
                        del self._last_turn[k]
            return self._with_title({"kind": "turn_done", "thread_id": tid}, lambda: self.title_of(tid))
        if event_type == "approval.requested":
            a = data.get("approval")
            tid = str(getattr(a, "thread_id", "") or "")
            if not tid:
                return None
            return self._with_title({"kind": "awaiting_approval", "thread_id": tid}, lambda: self.title_of(tid))
        if event_type == "agent.run":
            run = data.get("run") or {}
            kind = _DONE.get(str(run.get("status") or ""))
            tid = str(run.get("thread_id") or "")
            if kind is None or not tid:
                return None
            aid = str(data.get("agent_id") or run.get("agent_id") or "")
            return self._with_title({"kind": kind, "thread_id": tid}, lambda: self.agent_name(aid))
        return None

    def _with_title(self, event: dict[str, Any], title: Callable[[], str | None]) -> dict[str, Any]:
        t = None
        if self.mirrored():
            try:
                t = title()
            except Exception:  # noqa: BLE001
                t = None
        event["title"] = " ".join(str(t).split())[:80] or None if t else None
        return event

    # ── envoi ──
    def _enqueue(self, event: dict[str, Any]) -> None:
        if self.sync:
            self.send(event)
            return
        try:
            self._q.put_nowait(event)
        except queue.Full:
            logger.debug("notification abandonnée : file pleine")
            return
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._drain, name="nokker-notify", daemon=True)
                self._worker.start()

    def _drain(self) -> None:
        while True:
            try:
                event = self._q.get(timeout=30)
            except queue.Empty:
                return
            self.send(event)

    def _allowed(self) -> bool:
        now = self.clock()
        with self._lock:
            if now < self._paused_until:
                return False
            while self._sent and now - self._sent[0] > RATE_WINDOW_S:
                self._sent.popleft()
            if len(self._sent) >= RATE_MAX:
                return False
            self._sent.append(now)
            return True

    def send(self, event: dict[str, Any]) -> bool:
        """Un envoi ; True si nokker.ai l'a accepté (202). Jamais d'exception."""
        try:
            token = self.token()
        except Exception:  # noqa: BLE001
            token = None
        if not token or not self._allowed():
            return False
        if self.client is None:
            self.client = httpx.Client(timeout=10.0)
        try:
            r = self.client.post(self.base_url.rstrip("/") + "/v1/gateway/notify", json=event,
                                 headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            logger.debug("notification non remise (%s)", type(e).__name__)
            return False
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("Retry-After") or RATE_WINDOW_S)
            except ValueError:
                wait = RATE_WINDOW_S
            with self._lock:
                self._paused_until = self.clock() + min(max(wait, 1.0), 3600.0)
        return r.status_code == 202


__all__ = ["MachineNotify", "RATE_MAX", "TURN_DEBOUNCE_S"]
