"""Heurtoir dans home-bridge (docs/KNOCK.md §8) : reçoit les déclencheurs de home-audio (socket Unix) et des satellites
(écoute du foyer), les garde quelques minutes EN MÉMOIRE, et les rend au noyau par l'opération `knock.events`.

Aucun son ne passe ici : un message qui n'est pas un fait connu (`knock`, `state`) est jeté, et seuls les champs
connus sont recopiés. Pas de numpy : ce module tourne dans home-bridge, le détecteur dans home-audio.

    knock:                                    # home.yaml (absent = heurtoir éteint dans la passerelle)
      socket: /run/home/audio.sock            # home-audio → home-bridge (volume home-sock, 0700)
      mute_dir: /run/home                     # knock-mute-<pièce> : mute logiciel lu par home-audio
      satellites:                             # Raspberry Pi qui détectent eux-mêmes et envoient l'événement
        cuisine_pi: {room: cuisine, token_file: secrets/knock_cuisine_pi}
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import socket
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("nokker.homebridge.knock")

KINDS = ("knock", "clap", "deck", "nfc")
STATES = ("listening", "muted", "off")
TTL_S = 120.0
MAX_EVENTS = 200
MAX_LINE = 4096
_ROOM = re.compile(r"^[a-z0-9_]{1,40}$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def clean_event(raw: Any, *, room: str | None = None, source: str | None = None) -> dict[str, Any] | None:
    """Ne garde que les champs connus d'un déclencheur (jamais d'audio, jamais de champ inconnu). `room` / `source`
    imposés par la passerelle pour un satellite : il ne choisit pas sa pièce."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "")
    r = room or str(raw.get("room") or "")
    if kind not in KINDS or not _ROOM.match(r):
        return None
    eid = str(raw.get("event_id") or f"{r}-{uuid.uuid4().hex[:16]}")
    if not _ID.match(eid):
        return None
    try:
        at = float(raw.get("at"))
        conf = raw.get("confidence")
        conf = None if conf is None else round(max(0.0, min(1.0, float(conf))), 3)
    except (TypeError, ValueError):
        return None
    out = {"event_id": eid, "kind": kind, "room": r, "at": at, "confidence": conf,
           "source": (source or str(raw.get("source") or ""))[:64]}
    if raw.get("intent") in ("call", "approve"):
        out["intent"] = raw["intent"]
    return out


@dataclass
class KnockEvents:
    """File d'événements en mémoire, à curseur croissant ; `wait` = attente longue du noyau."""

    clock: Callable[[], float] = time.time
    _items: deque = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    _cursor: int = 0
    _cond: threading.Condition = field(default_factory=threading.Condition)
    states: dict[str, dict[str, Any]] = field(default_factory=dict)

    def push(self, ev: dict[str, Any]) -> int:
        with self._cond:
            self._cursor += 1
            self._items.append((self._cursor, self.clock(), ev))
            self._cond.notify_all()
            return self._cursor

    def set_state(self, room: str, state: str, source: str) -> None:
        if _ROOM.match(room) and state in STATES:
            with self._cond:
                self.states[room] = {"state": state, "source": source[:64], "at": self.clock()}

    def wait(self, after: int, wait_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, min(float(wait_s), 25.0))
        with self._cond:
            while True:
                now = self.clock()
                fresh = [ev for cur, ts, ev in self._items if cur > after and now - ts <= TTL_S]
                if fresh or time.monotonic() >= deadline:
                    return {"cursor": self._cursor, "events": fresh}
                self._cond.wait(timeout=max(0.05, deadline - time.monotonic()))


@dataclass
class KnockConfig:
    socket_path: str = "/run/home/audio.sock"
    mute_dir: str = "/run/home"
    satellites: dict[str, dict[str, str]] = field(default_factory=dict)  # id → {room, token}

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None, secret: Callable[[str], str]) -> KnockConfig | None:
        if not raw:
            return None
        sats = {}
        for sid, s in (raw.get("satellites") or {}).items():
            room = str((s or {}).get("room") or "")
            if not _ID.match(str(sid)) or not _ROOM.match(room):
                raise ValueError(f"knock.satellites.{sid} : identifiant ou pièce invalide")
            token = secret(str(s.get("token_file") or f"secrets/knock_{sid}"))
            if len(token) < 16:
                raise ValueError(f"knock.satellites.{sid} : jeton trop court (16 caractères au moins)")
            sats[str(sid)] = {"room": room, "token": token}
        return cls(socket_path=str(raw.get("socket") or "/run/home/audio.sock"),
                   mute_dir=str(raw.get("mute_dir") or "/run/home"), satellites=sats)


class KnockBridge:
    """La part « heurtoir » de home-bridge : opérations du noyau, socket de home-audio, satellites."""

    def __init__(self, cfg: KnockConfig, events: KnockEvents | None = None):
        self.cfg = cfg
        self.events = events or KnockEvents()

    # ── opérations nommées (noyau → passerelle) ──
    def op_events(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            after, wait_s = int(p.get("after") or 0), float(p.get("wait_s") or 0)
        except (TypeError, ValueError):
            after, wait_s = 0, 0.0
        return self.events.wait(after, wait_s)

    def op_status(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"rooms": dict(self.events.states), "satellites": sorted(self.cfg.satellites)}

    def op_mute(self, p: dict[str, Any]) -> dict[str, Any]:
        room = str(p.get("room") or "")
        if not _ROOM.match(room):
            raise ValueError("room invalide")
        flag = Path(self.cfg.mute_dir) / f"knock-mute-{room}"
        if p.get("muted"):
            flag.touch(mode=0o600, exist_ok=True)
        else:
            flag.unlink(missing_ok=True)
        return {"room": room, "muted": bool(p.get("muted"))}

    # ── home-audio → passerelle ──
    def accept_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line[:MAX_LINE])
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "state":
            self.events.set_state(str(msg.get("room") or ""), str(msg.get("state") or ""),
                                  str(msg.get("source") or "home-audio"))
        elif msg.get("type") == "knock":
            ev = clean_event(msg)
            if ev is not None:
                self.events.push(ev)

    def serve_socket(self) -> threading.Thread:
        path = self.cfg.socket_path
        if os.path.exists(path):
            os.unlink(path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o077)
        try:
            srv.bind(path)
        finally:
            os.umask(old)
        srv.listen(4)

        def client(conn: socket.socket) -> None:
            buf = b""
            with conn:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self.accept_line(line)
                    if len(buf) > MAX_LINE:
                        buf = b""  # ligne démesurée : jetée

        def loop() -> None:
            while True:
                conn, _ = srv.accept()
                threading.Thread(target=client, args=(conn,), daemon=True, name="knock-audio-conn").start()

        t = threading.Thread(target=loop, daemon=True, name="knock-audio-sock")
        t.start()
        return t

    # ── satellite → passerelle (écoute du foyer) ──
    def accept_satellite(self, sat_id: str, token: str, body: Any) -> bool:
        sat = self.cfg.satellites.get(sat_id)
        if sat is None or not token or not hmac.compare_digest(token.encode(), sat["token"].encode()):
            return False
        ev = clean_event(body, room=sat["room"], source=f"sat:{sat_id}")
        if ev is None:
            return False
        self.events.push(ev)
        return True


__all__ = ["KnockBridge", "KnockConfig", "KnockEvents", "clean_event"]
