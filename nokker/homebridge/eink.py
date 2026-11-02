"""Écrans e-ink servis par la passerelle sur le réseau du foyer (docs/AMBIENT.md §3.5).

Le noyau pousse l'image de chaque écran (`eink.publish`) avec l'EMPREINTE de son jeton ; la passerelle ne connaît
jamais un jeton en clair, sauf l'offre d'appairage TRMNL (`eink.setup_offer`, gardée en mémoire 15 minutes au plus et
remise une seule fois à l'appareil qui présente la bonne MAC). Une image expire seule après `ttl_s` ; `eink.revoke`
l'efface tout de suite ; `eink.retain` efface tout écran que le noyau ne cite plus.
"""
from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

MAX_PNG = 4 * 1024 * 1024
MAX_TTL_S = 3 * 86400


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@dataclass
class Entry:
    display_id: str
    token_sha256: str
    png: bytes
    filename: str
    refresh_s: int
    expires: float


@dataclass
class FrameCache:
    clock: Callable[[], float] = time.monotonic
    _by_id: dict[str, Entry] = field(default_factory=dict, repr=False)
    _offers: dict[str, tuple[str, float]] = field(default_factory=dict, repr=False)  # mac_sha → (jeton, fin)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def publish(self, display_id: str, token_sha256: str, png: bytes, filename: str, refresh_s: int,
                ttl_s: int) -> None:
        if not display_id or len(token_sha256 or "") != 64 or not png or len(png) > MAX_PNG:
            raise ValueError("eink.publish : champs invalides")
        with self._lock:
            self._by_id[display_id] = Entry(display_id, token_sha256, png, str(filename)[:80], int(refresh_s),
                                            self.clock() + max(60, min(MAX_TTL_S, int(ttl_s))))

    def revoke(self, display_id: str) -> bool:
        with self._lock:
            return self._by_id.pop(display_id, None) is not None

    def retain(self, display_ids: list[str]) -> int:
        keep = set(display_ids)
        with self._lock:
            gone = [k for k in self._by_id if k not in keep]
            for k in gone:
                self._by_id.pop(k, None)
            return len(gone)

    def lookup(self, token: str) -> Entry | None:
        if not token:
            return None
        h = _sha(token)
        now = self.clock()
        with self._lock:
            for e in list(self._by_id.values()):
                if e.expires < now:
                    self._by_id.pop(e.display_id, None)
                    continue
                if hmac.compare_digest(e.token_sha256, h):
                    return e
        return None

    def setup_offer(self, mac_sha256: str, token: str, ttl_s: int) -> None:
        if len(mac_sha256 or "") != 64 or not token:
            raise ValueError("eink.setup_offer : champs invalides")
        with self._lock:
            self._offers[mac_sha256] = (token, self.clock() + max(30, min(900, int(ttl_s))))

    def take_offer(self, mac: str) -> str | None:
        key = _sha("mac:" + (mac or "").strip().upper())
        with self._lock:
            got = self._offers.pop(key, None)
        if got is None or got[1] < self.clock():
            return None
        return got[0]


__all__ = ["Entry", "FrameCache"]
