"""Bulles `home` tenues à jour (docs/HOME-ASSISTANT.md §6) : un fil de fond abonné à `state_changed` met à jour les
bulles ouvertes des entités suivies (`bubble.updated`), se reconnecte avec un délai croissant, et s'arrête quand plus
aucune bulle n'est suivie. Aucun tour, aucun modèle : la bulle est un affichage."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class HomeBubbleWatcher:
    source: Any  # HomeAssistantSource : client, hub, bubble_state, policy
    idle_s: float = 30.0
    backoff_max_s: float = 60.0
    _tracked: dict[str, set[str]] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)

    def track(self, entity_id: str, bubble_id: str) -> None:
        with self._lock:
            self._tracked.setdefault(entity_id, set()).add(bubble_id)
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="nokker-home-bubbles", daemon=True)
                self._thread.start()

    def tracked(self) -> dict[str, set[str]]:
        with self._lock:
            return {k: set(v) for k, v in self._tracked.items()}

    def stop(self) -> None:
        self._stop.set()

    def push(self, entity_id: str, state: dict[str, Any]) -> None:
        """Nouvel état d'une entité suivie → ses bulles ouvertes ; une bulle fermée cesse d'être suivie."""
        hub = self.source.hub
        with self._lock:
            ids = set(self._tracked.get(entity_id, ()))
        for bid in ids:
            b = hub.bubbles.get(bid) if hub is not None else None
            if b is None or not b.is_open:
                self._untrack(entity_id, bid)
                continue
            try:
                hub.update_from_kernel(bid, state=state)
            except Exception:
                logger.exception("bulle maison : mise à jour en échec")

    def _untrack(self, entity_id: str, bubble_id: str) -> None:
        with self._lock:
            ids = self._tracked.get(entity_id)
            if ids is not None:
                ids.discard(bubble_id)
                if not ids:
                    self._tracked.pop(entity_id, None)

    def _event(self, msg: dict[str, Any]) -> None:
        ev = msg.get("event") or {}
        if ev.get("event_type") != "state_changed":
            return
        data = ev.get("data") or {}
        eid = str(data.get("entity_id") or "")
        new = data.get("new_state")
        if not eid or not isinstance(new, dict) or eid not in self.tracked():
            return
        from nokker.home.source import Snapshot

        snap = self.source._snap
        snap = Snapshot(states={eid: new}, area_of=snap.area_of if snap else {}, hidden=frozenset(), at=time.monotonic())
        state = self.source.bubble_state(snap, eid)
        if state is not None:
            self.push(eid, state)

    def _run(self) -> None:
        delay = 2.0
        while not self._stop.is_set() and self.tracked():
            session = None
            try:
                session = self.source.client.session()
                session.subscribe_state_changed()
                delay = 2.0
                while not self._stop.is_set() and self.tracked():
                    got = False
                    for msg in session.events(self.idle_s):
                        got = True
                        self._event(msg)
                        if self._stop.is_set() or not self.tracked():
                            break
                    if not got:
                        self._prune()
            except Exception as e:  # noqa: BLE001 — reconnexion
                logger.info("bulles maison : connexion perdue (%s), reprise dans %.0f s", type(e).__name__, delay)
                self._stop.wait(delay)
                delay = min(self.backoff_max_s, delay * 2)
            finally:
                if session is not None:
                    session.close()

    def _prune(self) -> None:
        hub = self.source.hub
        for eid, ids in self.tracked().items():
            for bid in ids:
                b = hub.bubbles.get(bid) if hub is not None else None
                if b is None or not b.is_open:
                    self._untrack(eid, bid)


__all__ = ["HomeBubbleWatcher"]
