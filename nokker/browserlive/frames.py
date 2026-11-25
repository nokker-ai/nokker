"""Captures de la vue navigateur en direct (docs/BROWSER-LIVE.md §2) — EN MÉMOIRE SEULEMENT.

- Un anneau des `RING` dernières captures par session navigateur ; `purge(session)` à la fin de la session. Jamais en
  base, jamais dans un stockage objet, jamais sur disque.
- URL signée même origine : `/v1/browser/frames/<id>.webp?exp=<epoch>&sig=<hmac>`, sans en-tête Authorization (une
  balise `<img>` ne sait pas en poser). HMAC-SHA256 sur `<id>.<exp>` avec une clé tirée au démarrage du processus (les
  captures ne survivent pas au processus, la clé non plus). Validité `TTL_S` = 60 s ; au-delà, ou capture purgée : 410.
- Le noyau n'accepte qu'un WebP (en-tête RIFF/WEBP vérifié) d'au plus `MAX_WIDTH` px de large : il ne réencode pas
  (pas de dépendance image) ; une capture plus large est jetée, jamais servie.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

RING = 20
TTL_S = 60
MAX_WIDTH = 1280
MAX_BYTES = 2_000_000
PATH_PREFIX = "/v1/browser/frames/"


@dataclass(frozen=True)
class Frame:
    id: str
    session_id: str
    w: int
    h: int
    data: bytes
    at: float


def webp_size(data: bytes) -> tuple[int, int] | None:
    """(largeur, hauteur) d'un WebP (VP8, VP8L ou VP8X), ou None si ce n'en est pas un."""
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        w = 1 + int.from_bytes(data[24:27], "little")
        h = 1 + int.from_bytes(data[27:30], "little")
        return w, h
    if chunk == b"VP8L":
        if data[20] != 0x2F:
            return None
        b = int.from_bytes(data[21:25], "little")
        return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        w, h = struct.unpack("<HH", data[26:30])
        return w & 0x3FFF, h & 0x3FFF
    return None


@dataclass
class FrameStore:
    clock: Callable[[], float] = time.time
    ttl_s: int = TTL_S
    ring: int = RING
    _key: bytes = field(default_factory=lambda: secrets.token_bytes(32), repr=False)
    _by_session: dict[str, deque] = field(default_factory=dict, repr=False)
    _by_id: dict[str, Frame] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def put(self, session_id: str, data: bytes) -> Frame | None:
        """Range une capture (WebP valide, ≤ MAX_WIDTH) ; None si elle est refusée. La plus vieille sort de l'anneau."""
        if not data or len(data) > MAX_BYTES:
            return None
        size = webp_size(data)
        if size is None or size[0] > MAX_WIDTH or size[0] <= 0 or size[1] <= 0:
            return None
        frame = Frame(id="bf_" + secrets.token_urlsafe(12), session_id=session_id, w=size[0], h=size[1], data=data,
                      at=self.clock())
        with self._lock:
            ring = self._by_session.setdefault(session_id, deque())
            ring.append(frame)
            self._by_id[frame.id] = frame
            while len(ring) > self.ring:
                old = ring.popleft()
                self._by_id.pop(old.id, None)
        return frame

    def last(self, session_id: str) -> Frame | None:
        with self._lock:
            ring = self._by_session.get(session_id)
            return ring[-1] if ring else None

    def count(self, session_id: str | None = None) -> int:
        with self._lock:
            if session_id is None:
                return len(self._by_id)
            return len(self._by_session.get(session_id) or ())

    def purge(self, session_id: str) -> None:
        with self._lock:
            for f in self._by_session.pop(session_id, ()) or ():
                self._by_id.pop(f.id, None)

    # ── URL signée ──
    def _sig(self, frame_id: str, exp: int) -> str:
        mac = hmac.new(self._key, f"{frame_id}.{exp}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode().rstrip("=")

    def url(self, frame: Frame) -> str:
        exp = int(self.clock()) + self.ttl_s
        return f"{PATH_PREFIX}{frame.id}.webp?exp={exp}&sig={self._sig(frame.id, exp)}"

    def fetch(self, frame_id: str, exp: str | None, sig: str | None) -> tuple[int, bytes | None]:
        """(statut HTTP, octets) : 200 + image ; 403 signature absente ou fausse ; 410 expirée ou purgée."""
        try:
            exp_i = int(exp or "")
        except ValueError:
            return 403, None
        if not sig or not hmac.compare_digest(self._sig(frame_id, exp_i), sig):
            return 403, None
        if self.clock() > exp_i:
            return 410, None
        with self._lock:
            frame = self._by_id.get(frame_id)
        if frame is None:
            return 410, None
        return 200, frame.data


__all__ = ["MAX_WIDTH", "PATH_PREFIX", "RING", "TTL_S", "Frame", "FrameStore", "webp_size"]
