"""Présence par pièce (docs/AMBIENT.md §2.2) : qui est dans quelle pièce, et y a-t-il des invités.

Interface vers la maison (chantier Home Assistant) : `PresenceSource.presence() -> Presence`. Deux sources :
  - `StatesPresence` : calcule la présence depuis une liste d'états au format Home Assistant (`entity_id`, `state`),
    avec la table des pièces de la config. PUR (aucun réseau) : la passerelle du foyer l'appelle avec les états qu'elle
    lit chez Home Assistant ; le chantier Home Assistant peut l'appeler avec son instantané (`Snapshot.states`).
  - `BridgePresence` (nokker/ambient/bridge.py) : le noyau demande la présence à la passerelle (`presence.get`).

Table des pièces (config `ambient.rooms`, la même côté passerelle) :

    rooms:
      salon:
        occupancy: [binary_sensor.salon_presence]   # « on » = quelqu'un dans la pièce
        speakers: [{sink: music_assistant, target: media_player.salon}]
      chambre_a: {occupancy: [binary_sensor.chambre_a_mmwave], speakers: [{sink: snapcast, target: groupe_chambre}]}
    members:            # localisation d'un membre (capteur de pièce type Bermuda / ESPresense : état = nom de pièce)
      us_alice: {room_sensor: sensor.alice_piece}
    guests: input_boolean.invites      # interrupteur « invités présents » du foyer (facultatif)

Une pièce occupée où aucun membre n'est localisé compte `unknown` occupants : pour la confidentialité, un occupant
inconnu est traité comme un invité possible (jamais de contenu personnel lu devant lui).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

_ROOM = re.compile(r"^[a-z0-9_]{1,40}$")
ON_STATES = frozenset({"on", "home", "detected", "occupied", "true"})


@dataclass(frozen=True)
class RoomState:
    members: frozenset[str] = frozenset()  # comptes localisés dans la pièce
    occupied: bool = False  # un capteur de présence de la pièce est actif
    unknown: bool = False  # occupée sans membre localisé : invité possible


@dataclass(frozen=True)
class Presence:
    rooms: dict[str, RoomState] = field(default_factory=dict)
    guests: bool = False  # mode « invités présents » du foyer (interrupteur, ou réglage du noyau)
    ok: bool = True  # False : présence illisible (source absente, panne) → prudence maximale

    def room_of(self, account_id: str) -> str | None:
        found = [r for r, st in self.rooms.items() if account_id in st.members]
        return found[0] if len(found) == 1 else None

    def alone_with(self, room: str, account_id: str) -> bool:
        """Vrai si la présence ne voit QUE ce membre dans la pièce (ni autre membre, ni occupant inconnu)."""
        st = self.rooms.get(room)
        return bool(self.ok and st is not None and st.members == frozenset({account_id}) and not st.unknown)


UNKNOWN = Presence(ok=False)


class PresenceSource(Protocol):
    def presence(self) -> Presence: ...


def valid_room(name: str) -> bool:
    return bool(_ROOM.match(name or ""))


@dataclass
class RoomsConfig:
    rooms: dict[str, dict[str, Any]] = field(default_factory=dict)
    members: dict[str, dict[str, Any]] = field(default_factory=dict)
    guests_entity: str = ""

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> RoomsConfig:
        raw = raw or {}
        rooms = {str(k): dict(v or {}) for k, v in (raw.get("rooms") or {}).items() if valid_room(str(k))}
        members = {str(k): dict(v or {}) for k, v in (raw.get("members") or {}).items()}
        return cls(rooms=rooms, members=members, guests_entity=str(raw.get("guests") or ""))

    def entities(self) -> set[str]:
        """Entités Home Assistant lues pour la présence (la passerelle ne lit que celles-là)."""
        out = {e for r in self.rooms.values() for e in (r.get("occupancy") or [])}
        out |= {str(m.get("room_sensor")) for m in self.members.values() if m.get("room_sensor")}
        if self.guests_entity:
            out.add(self.guests_entity)
        return out


def _norm_room(value: str) -> str:
    v = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return re.sub(r"[^a-z0-9_]", "", v)


def presence_from_states(states: list[dict[str, Any]], cfg: RoomsConfig) -> Presence:
    """Présence calculée depuis des états Home Assistant. Un état inconnu (`unavailable`…) ne prouve rien."""
    by_id = {str(s.get("entity_id")): str(s.get("state") or "").lower() for s in states if isinstance(s, dict)}
    located: dict[str, set[str]] = {r: set() for r in cfg.rooms}
    for account, m in cfg.members.items():
        room = _norm_room(by_id.get(str(m.get("room_sensor") or ""), ""))
        if room in located:
            located[room].add(account)
    rooms: dict[str, RoomState] = {}
    for name, r in cfg.rooms.items():
        occupied = any(by_id.get(e) in ON_STATES for e in (r.get("occupancy") or []))
        members = frozenset(located[name])
        rooms[name] = RoomState(members=members, occupied=occupied or bool(members),
                                unknown=occupied and not members)
    guests = bool(cfg.guests_entity) and by_id.get(cfg.guests_entity) in ON_STATES
    return Presence(rooms=rooms, guests=guests, ok=True)


@dataclass
class StatesPresence:
    """Source de présence sur une fonction qui rend les états (client Home Assistant, instantané, faux en test)."""

    states: Any  # Callable[[], list[dict]]
    cfg: RoomsConfig

    def presence(self) -> Presence:
        try:
            return presence_from_states(list(self.states() or []), self.cfg)
        except Exception:  # noqa: BLE001 — présence illisible : prudence (UNKNOWN)
            return UNKNOWN


__all__ = [
    "UNKNOWN",
    "Presence",
    "PresenceSource",
    "RoomState",
    "RoomsConfig",
    "StatesPresence",
    "presence_from_states",
    "valid_room",
]
