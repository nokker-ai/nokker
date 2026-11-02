"""Rafraîchissement planifié des écrans vers la passerelle du foyer (Lokkal, docs/AMBIENT.md §3.5).

Les écrans du réseau local ne joignent pas le noyau (fermé au LAN) : le noyau POUSSE l'image de chaque écran actif à la
passerelle (`eink.publish`), qui la sert aux appareils contre leur jeton (elle ne connaît que son empreinte). Un écran
est republié quand son image change (tableau, jour) ou à chaque `refresh_s` ; `eink.retain` fait oublier à la
passerelle les écrans révoqués ou inconnus, et chaque image publiée y expire seule après `ttl_s` (3 × refresh) si le
noyau se tait. La révocation part aussi tout de suite (`Displays.on_revoke` → `eink.revoke`).
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("nokker.ambient.publisher")


@dataclass
class EinkPublisher:
    displays: Any  # Displays
    bridge: Any  # HomeBridge | FakeBridge
    interval_s: float = 60.0
    clock: Callable[[], float] = time.monotonic
    _sent: dict[str, tuple[str, float]] = field(default_factory=dict, repr=False)
    _wake: threading.Event = field(default_factory=threading.Event, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    def tick(self) -> int:
        """Une passe : publie ce qui est dû ; rend le nombre d'images envoyées. Ne lève jamais."""
        sent = 0
        try:
            active = self.displays.store.active()
        except Exception:
            logger.exception("écrans : liste illisible")
            return 0
        now = self.clock()
        for d in active:
            try:
                f = self.displays.frame(d, seen=False)
                last = self._sent.get(d.id)
                if last is not None and last[0] == f.filename and now - last[1] < d.refresh_s:
                    continue
                self.bridge.eink_publish(d.id, d.token_sha256, f.png, f.filename, d.refresh_s, 3 * d.refresh_s)
                self._sent[d.id] = (f.filename, now)
                sent += 1
            except Exception as e:  # noqa: BLE001 — un écran en panne n'arrête pas les autres
                logger.warning("écran %s : publication en échec (%s)", d.id, type(e).__name__)
        ids = [d.id for d in active]
        for gone in set(self._sent) - set(ids):
            self._sent.pop(gone, None)
        try:
            self.bridge.eink_retain(ids)
        except Exception as e:  # noqa: BLE001
            logger.warning("écrans : retain en échec (%s)", type(e).__name__)
        return sent

    def revoked(self, display: Any) -> None:
        self._sent.pop(display.id, None)
        self.bridge.eink_revoke(display.id)

    def wake(self, *_: Any) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="nokker-eink", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._wake.wait(self.interval_s)
            self._wake.clear()


__all__ = ["EinkPublisher"]
