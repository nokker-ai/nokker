"""Heurtoir — côté noyau de l'API core ↔ home-bridge (docs/KNOCK.md §8, docs/LOKKAL-HOME-BRIDGE.md).

Le noyau ne parle qu'à home-bridge, par le client commun `nokker.ambient.bridge.HomeBridge` (`POST {url}/v1/op/<nom>`,
jeton `NOKKER_HOME_BRIDGE_TOKEN`). Il TIRE les déclencheurs (`knock.events`, attente longue) : home-bridge n'a aucune
route vers le noyau à connaître, et le noyau n'ouvre aucun port sur le réseau `homeapi`. Un événement ne porte que des
faits (pièce, heure, type, source, confiance) : aucun son n'atteint le noyau avant le déclencheur.

Voix et présence viennent du chantier ambiant : `RoomAudio.say` (opération `audio.play`) pour ce qui est dit dans la
pièce, `presence.get` pour qui est où.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import replace
from typing import Any

from nokker.knock.hub import KernelVoice, KnockHub
from nokker.knock.policy import KnockConfig
from nokker.knock.types import (
    INTENTS,
    KINDS,
    NullAmbientOutput,
    PhysicalTrigger,
    PresenceFact,
    SpokenLine,
)

logger = logging.getLogger("nokker.knock.bridge")

WAIT_S = 20.0
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_ROOM = re.compile(r"^[a-z0-9_]{1,40}$")  # = nokker.ambient.presence.valid_room


def parse_event(raw: dict[str, Any]) -> PhysicalTrigger | None:
    """Événement de home-bridge → déclencheur, ou None s'il est mal formé (jamais d'exception vers la boucle). Tout
    champ inconnu (et donc tout « audio » qu'un bridge fautif ajouterait) est ignoré."""
    try:
        eid, kind, room = str(raw["event_id"]), str(raw["kind"]), str(raw["room"])
        at = float(raw["at"])
    except (KeyError, TypeError, ValueError):
        return None
    if not _ID.match(eid) or kind not in KINDS or not _ROOM.match(room):
        return None
    intent = raw.get("intent")
    if intent is not None and intent not in INTENTS:
        return None
    conf = raw.get("confidence")
    try:
        conf = None if conf is None else max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        conf = None
    return PhysicalTrigger(event_id=eid, kind=kind, room=room, at=at, source=str(raw.get("source") or "")[:64],
                           confidence=conf, intent=intent)


class KnockPump:
    """Fil de fond : tire `knock.events` et passe chaque déclencheur au `KnockHub`. Repart après une erreur."""

    def __init__(self, bridge: Any, hub: KnockHub, wait_s: float = WAIT_S):
        self.bridge = bridge
        self.hub = hub
        self.wait_s = wait_s
        self.cursor = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self.run, daemon=True, name="knock-pump")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> int:
        res = self.bridge.op("knock.events", {"after": self.cursor, "wait_s": self.wait_s})
        try:
            self.cursor = int(res.get("cursor", self.cursor))
        except (TypeError, ValueError):
            pass
        n = 0
        for raw in res.get("events") or []:
            trig = parse_event(raw) if isinstance(raw, dict) else None
            if trig is None:
                logger.warning("home-bridge : événement mal formé ignoré")
                continue
            out = self.hub.handle(trig)
            logger.info("heurtoir : %s dans %s → %s %s", trig.kind, trig.room, out.action, out.reason)
            n += 1
        return n

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self.run_once()
                backoff = 1.0
            except Exception as e:  # noqa: BLE001 — BridgeError (sans jeton) ou panne : on réessaie
                logger.warning("heurtoir : %s (nouvel essai dans %.0f s)", e, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)


class BridgePresence:
    """`Presence` du heurtoir sur `presence.get` de home-bridge (Home Assistant : capteurs de pièce des membres).
    Lokkal = un foyer par machine : `tenant` n'est pas un filtre ici, c'est `decider_allowed` du hub qui écarte tout
    membre d'un autre locataire. Présence illisible : exception (le hub refuse, jamais « présent » par défaut)."""

    def __init__(self, bridge: Any, clock: Any = time.time):
        self.bridge = bridge
        self.clock = clock

    def present(self, tenant: str) -> list[PresenceFact]:
        p = self.bridge.presence()
        if not getattr(p, "ok", False):
            raise RuntimeError("présence illisible")
        now = self.clock()
        return [PresenceFact(member=m, source="home_assistant", seen_at=now, room=room)
                for room, st in p.rooms.items() for m in sorted(st.members)]


class RoomAudioOutput:
    """`AmbientOutput` du heurtoir sur `RoomAudio.say` (chantier ambiant). Les phrases du heurtoir (« Approuvé. »,
    refus) sont communes ; la réponse d'une session vocale est personnelle (lue seulement si la présence ne voit que
    le destinataire, sinon l'annonce neutre de l'ambiant). Les carillons n'ont pas d'opération : ignorés."""

    def __init__(self, room_audio: Any, voice_privacy: str = "personal"):
        self.room_audio = room_audio
        self.voice_privacy = voice_privacy

    def say(self, line: SpokenLine) -> None:
        if not line.text or self.room_audio is None:
            return
        privacy = self.voice_privacy if line.private else "common"
        self.room_audio.say(line.room, line.text, recipient=line.recipient, privacy=privacy)


def configure_knock(config: Any, kernel: Any, approvals: Any, *, local: bool, bridge: Any = None,
                    room_audio: Any = None) -> KnockHub | None:
    """Hub du heurtoir, et sa pompe si home-bridge est configuré. Lokkal seulement ; drapeau éteint : None."""
    cfg = KnockConfig.from_raw(config.knock)
    if not cfg.enabled:
        return None
    if not local:
        logger.warning("knock.enabled ignoré : le heurtoir est une fonction de Lokkal (home-bridge), pas du cloud")
        return None
    if bridge is None:
        from nokker.ambient.bridge import HomeBridge

        bridge = HomeBridge.from_env()
    if room_audio is None:
        from nokker.ambient import http as ambient_http

        amb = ambient_http.service()
        room_audio = getattr(amb, "room_audio", None)
    ambient = RoomAudioOutput(room_audio, cfg.voice_privacy) if room_audio is not None else NullAmbientOutput()
    if room_audio is None:
        logger.warning("heurtoir : ambient.room_audio éteint, rien ne sera dit dans la pièce")
    hub = KnockHub(cfg, approvals=approvals, presence=BridgePresence(bridge) if bridge is not None else None,
                   ambient=ambient, voice=KernelVoice(kernel, ambient))
    hub.pump = None
    if bridge is None:
        logger.warning("heurtoir : NOKKER_HOME_BRIDGE_URL / NOKKER_HOME_BRIDGE_TOKEN absents, aucun déclencheur ne "
                       "sera reçu (profil compose home)")
    else:
        # L'attente longue doit tenir dans le délai du client : une copie du client à délai allongé, pour la pompe.
        pump_bridge = replace(bridge, timeout_s=WAIT_S + 10) if hasattr(bridge, "timeout_s") else bridge
        hub.pump = KnockPump(pump_bridge, hub)
    if not cfg.rooms:
        logger.warning("heurtoir : aucune pièce dans knock.rooms, tout déclencheur sera ignoré")
    return hub
