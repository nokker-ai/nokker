"""Client du noyau vers la passerelle du foyer `home-bridge` (docs/LOKKAL-HOME-BRIDGE.md, docs/AMBIENT.md §4).

Le noyau reste fermé au réseau local : il ne parle qu'à la passerelle, par des OPÉRATIONS NOMMÉES
(`POST {url}/v1/op/<nom>`, JSON), jamais par un proxy générique. Jeton `NOKKER_HOME_BRIDGE_TOKEN` en
`Authorization: Bearer` ; l'adresse `NOKKER_HOME_BRIDGE_URL` (réseau interne `homeapi` du compose). Aucun secret
du foyer ne revient au noyau ; aucun secret du noyau ne part à la passerelle.

Opérations de ce chantier :
  - `presence.get`   {}                                  → {rooms: {pièce: {members, occupied, unknown}}, guests}
  - `audio.play`     {room, audio_b64, mime}             → {played, sinks}
  - `eink.publish`   {display_id, token_sha256, png_b64, filename, refresh_s, ttl_s}  → {ok}
  - `eink.revoke`    {display_id}                        → {ok}
  - `eink.retain`    {display_ids}                       → {dropped}
  - `eink.setup_offer` {mac_sha256, token, ttl_s}        → {ok}   (appairage TRMNL sur le réseau du foyer)
"""
from __future__ import annotations

import base64
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib import request as urllib_request

from nokker.ambient.presence import UNKNOWN, Presence, RoomState

logger = logging.getLogger("nokker.ambient.bridge")

URL_ENV = "NOKKER_HOME_BRIDGE_URL"
TOKEN_ENV = "NOKKER_HOME_BRIDGE_TOKEN"
MAX_REPLY = 1024 * 1024


class BridgeError(RuntimeError):
    """Passerelle absente, refus, panne. Le message ne porte jamais le jeton."""


@dataclass
class HomeBridge:
    url: str
    token: Callable[[], str]  # lu à chaque appel (variable d'environnement), jamais gardé dans un message
    timeout_s: float = 10.0
    opener: Callable[..., Any] = urllib_request.urlopen  # remplaçable en test

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> HomeBridge | None:
        env = dict(os.environ if env is None else env)
        url = (env.get(URL_ENV) or "").strip().rstrip("/")
        if not url or not env.get(TOKEN_ENV):
            return None
        return cls(url=url, token=lambda: (os.environ if env is None else env).get(TOKEN_ENV, ""))

    def op(self, name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self.token() or ""
        if not token:
            raise BridgeError("passerelle du foyer : jeton absent")
        req = urllib_request.Request(f"{self.url}/v1/op/{name}", method="POST",
                                     data=json.dumps(payload or {}).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {token}"})
        try:
            with self.opener(req, timeout=self.timeout_s) as resp:
                raw = resp.read(MAX_REPLY + 1)
        except Exception as e:  # noqa: BLE001 — jamais l'exception brute (elle pourrait citer un en-tête)
            code = getattr(e, "code", None)
            detail = ""
            if code is not None and hasattr(e, "read"):  # refus de la passerelle : son motif (jamais un secret)
                try:
                    got = json.loads(e.read(4096) or b"{}")
                    detail = str(got.get("error") or "")[:200] if isinstance(got, dict) else ""
                except Exception:  # noqa: BLE001
                    detail = ""
            raise BridgeError(f"passerelle du foyer : {name} en échec ({code or type(e).__name__})"
                              + (f" — {detail}" if detail else "")) from None
        try:
            out = json.loads(raw or b"{}")
        except ValueError:
            raise BridgeError(f"passerelle du foyer : réponse illisible à {name}") from None
        return out if isinstance(out, dict) else {}

    # ── opérations typées ──
    def presence(self) -> Presence:
        try:
            out = self.op("presence.get")
        except BridgeError as e:
            logger.warning("%s", e)
            return UNKNOWN
        rooms = {}
        for name, st in (out.get("rooms") or {}).items():
            if isinstance(st, dict):
                rooms[str(name)] = RoomState(members=frozenset(str(m) for m in st.get("members") or []),
                                             occupied=bool(st.get("occupied")), unknown=bool(st.get("unknown")))
        return Presence(rooms=rooms, guests=bool(out.get("guests")), ok=bool(out.get("ok", True)))

    def play(self, room: str, audio: bytes, mime: str = "audio/ogg") -> dict[str, Any]:
        return self.op("audio.play", {"room": room, "mime": mime,
                                      "audio_b64": base64.b64encode(audio).decode("ascii")})

    def eink_publish(self, display_id: str, token_sha256: str, png: bytes, filename: str, refresh_s: int,
                     ttl_s: int) -> None:
        self.op("eink.publish", {"display_id": display_id, "token_sha256": token_sha256, "filename": filename,
                                 "png_b64": base64.b64encode(png).decode("ascii"), "refresh_s": refresh_s,
                                 "ttl_s": ttl_s})

    def eink_revoke(self, display_id: str) -> None:
        self.op("eink.revoke", {"display_id": display_id})

    def eink_retain(self, display_ids: list[str]) -> None:
        self.op("eink.retain", {"display_ids": sorted(display_ids)})


@dataclass
class FakeBridge:
    """Passerelle en mémoire (tests, démo) : enregistre les opérations reçues."""

    presence_value: Presence = field(default_factory=Presence)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail: bool = False

    def op(self, name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((name, dict(payload or {})))
        return {"ok": True}

    def presence(self) -> Presence:
        self.calls.append(("presence.get", {}))
        return UNKNOWN if self.fail else self.presence_value

    def play(self, room: str, audio: bytes, mime: str = "audio/ogg") -> dict[str, Any]:
        if self.fail:
            raise BridgeError("passerelle du foyer : audio.play en échec (fausse panne)")
        self.calls.append(("audio.play", {"room": room, "audio": audio, "mime": mime}))
        return {"played": True, "sinks": ["fake"]}

    def eink_publish(self, display_id: str, token_sha256: str, png: bytes, filename: str, refresh_s: int,
                     ttl_s: int) -> None:
        self.calls.append(("eink.publish", {"display_id": display_id, "token_sha256": token_sha256, "png": png,
                                            "filename": filename, "refresh_s": refresh_s, "ttl_s": ttl_s}))

    def eink_revoke(self, display_id: str) -> None:
        self.calls.append(("eink.revoke", {"display_id": display_id}))

    def eink_retain(self, display_ids: list[str]) -> None:
        self.calls.append(("eink.retain", {"display_ids": sorted(display_ids)}))


__all__ = ["TOKEN_ENV", "URL_ENV", "BridgeError", "FakeBridge", "HomeBridge"]
