"""Zones de l'app par membre (docs/LOKKAL-PHONE.md §4.2) : modèle, nettoyage, magasins mémoire et Postgres (075).

Le noyau ne connaît d'une zone que `{id, name, on_enter, on_exit}` et sa dernière transition : JAMAIS de coordonnées
(le centre et le rayon restent sur l'appareil). Les événements ne gardent que leur id et le résultat rendu, 7 jours,
pour l'idempotence.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

MAX_ZONES = 18
NAME_MAX = 40
MAX_SCENARIOS = 8  # par liste on_enter / on_exit
EVENT_KEEP = timedelta(days=7)
_ZONE_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_SCENARIO_ID = re.compile(r"^[a-z0-9_]{1,64}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{8,80}$")
EVENTS = ("enter", "exit")
STATE_OF = {"enter": "inside", "exit": "outside"}


class ZoneError(ValueError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code = status, code


@dataclass(frozen=True)
class Zone:
    id: str
    name: str
    on_enter: tuple[str, ...] = ()
    on_exit: tuple[str, ...] = ()
    home: bool = False  # LA zone maison du membre (une seule) : `home` / `not_home` dans HA
    state: str = ""  # "" (jamais vue) | inside | outside
    state_at: datetime | None = None

    def json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "on_enter": list(self.on_enter), "on_exit": list(self.on_exit),
                "home": self.home}


def clean_name(value: Any) -> str:
    """Nom vu par l'app, le foyer, « Qui fait quoi » et parfois HA : une ligne neutralisée (`qa/untrusted.inline`),
    de 1 à 40 caractères."""
    from nokker.qa.untrusted import inline

    if not isinstance(value, str):
        raise ZoneError(400, "bad_zone", "name : texte de 1 à 40 caractères")
    name = " ".join(inline(value).split())
    if not 1 <= len(name) <= NAME_MAX:
        raise ZoneError(400, "bad_zone", "name : texte de 1 à 40 caractères")
    return name


def _scenarios(value: Any, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_SCENARIOS \
            or any(not isinstance(v, str) or not _SCENARIO_ID.match(v) for v in value):
        raise ZoneError(400, "bad_zone", f"{key} : liste d'ids de scénario ({MAX_SCENARIOS} au plus)")
    return tuple(dict.fromkeys(value))


def clean_zones(body: Any) -> list[Zone]:
    """Corps de `PUT /v1/me/zones` → zones validées. Toute clé de position (lat, lon, radius…) est REFUSÉE : le noyau
    ne doit jamais en recevoir, même ignorée."""
    if not isinstance(body, dict) or set(body) != {"zones"} or not isinstance(body["zones"], list):
        raise ZoneError(400, "bad_request", "corps attendu : {zones: [...]}")
    raw = body["zones"]
    if len(raw) > MAX_ZONES:
        raise ZoneError(400, "too_many", f"{MAX_ZONES} zones au plus")
    out, seen = [], set()
    for z in raw:
        if not isinstance(z, dict) or set(z) - {"id", "name", "on_enter", "on_exit", "home"}:
            raise ZoneError(400, "bad_zone", "zone : {id, name, on_enter, on_exit, home} seulement (jamais de coordonnées)")
        if not isinstance(z.get("home", False), bool):
            raise ZoneError(400, "bad_zone", "home : booléen")
        zid = z.get("id")
        if not isinstance(zid, str) or not _ZONE_ID.match(zid):
            raise ZoneError(400, "bad_zone", "id : 1 à 40 caractères [A-Za-z0-9_-]")
        if zid in seen:
            raise ZoneError(400, "bad_zone", f"id en double : {zid}")
        seen.add(zid)
        out.append(Zone(id=zid, name=clean_name(z.get("name")), on_enter=_scenarios(z.get("on_enter"), "on_enter"),
                        on_exit=_scenarios(z.get("on_exit"), "on_exit"), home=bool(z.get("home", False))))
    if sum(z.home for z in out) > 1:
        raise ZoneError(400, "bad_zone", "une seule zone home par membre")
    return out


def clean_event(body: Any, now: datetime) -> tuple[str, datetime, str]:
    """Corps de `POST …/event` → (event, at, event_id). `at` : ISO 8601 avec fuseau, ou secondes epoch ; au plus 5 min
    dans le futur (horloge du téléphone un peu en avance)."""
    if not isinstance(body, dict) or set(body) - {"event", "at", "event_id"}:
        raise ZoneError(400, "bad_request", "corps attendu : {event, at, event_id} (jamais de coordonnées)")
    ev, eid, at = body.get("event"), body.get("event_id"), body.get("at")
    if ev not in EVENTS:
        raise ZoneError(400, "bad_event", "event : enter | exit")
    if not isinstance(eid, str) or not _EVENT_ID.match(eid):
        raise ZoneError(400, "bad_event", "event_id : 8 à 80 caractères [A-Za-z0-9_.:-]")
    try:
        if isinstance(at, (int, float)) and not isinstance(at, bool):
            when = datetime.fromtimestamp(float(at), tz=UTC)
        elif isinstance(at, str):
            when = datetime.fromisoformat(at.replace("Z", "+00:00"))
            if when.tzinfo is None:
                raise ValueError
        else:
            raise ValueError
    except (ValueError, OverflowError, OSError):
        raise ZoneError(400, "bad_event", "at : ISO 8601 avec fuseau, ou secondes epoch") from None
    if when > now + timedelta(minutes=5):
        raise ZoneError(400, "bad_event", "at : dans le futur")
    return str(ev), when.astimezone(UTC), eid


# ── magasins ──
@dataclass
class EventOutcome:
    kind: str  # ok | duplicate | out_of_order | unknown_zone
    zone: Zone | None = None
    result: dict[str, Any] | None = None  # duplicate : le résultat déjà rendu


@dataclass
class MemoryZoneStore:
    """Tests, dev, Lokkal sans Postgres. Clés par (locataire, compte), comme la RLS."""

    _zones: dict[tuple[str, str], dict[str, Zone]] = field(default_factory=dict)
    _events: dict[tuple[str, str], dict[str, tuple[datetime, dict[str, Any]]]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def _key(account: str) -> tuple[str, str]:
        from nokker.tenancy.context import current_tenant

        return current_tenant(), account

    def zones(self, account: str) -> list[Zone]:
        with self._lock:
            return list(self._zones.get(self._key(account), {}).values())

    def replace(self, account: str, zones: list[Zone]) -> list[Zone]:
        with self._lock:
            old = self._zones.get(self._key(account), {})
            new = {z.id: replace(z, state=old[z.id].state, state_at=old[z.id].state_at) if z.id in old else z
                   for z in zones}
            self._zones[self._key(account)] = new
            return list(new.values())

    def event(self, account: str, zone_id: str, event: str, at: datetime, event_id: str,
              now: datetime) -> EventOutcome:
        with self._lock:
            k = self._key(account)
            seen = self._events.setdefault(k, {})
            for eid in [e for e, (when, _) in seen.items() if now - when > EVENT_KEEP]:
                del seen[eid]
            if event_id in seen:
                return EventOutcome("duplicate", result=dict(seen[event_id][1]))
            z = self._zones.get(k, {}).get(zone_id)
            if z is None:
                return EventOutcome("unknown_zone")
            if z.state_at is not None and at < z.state_at:
                return EventOutcome("out_of_order", zone=z)
            z = replace(z, state=STATE_OF[event], state_at=at)
            self._zones[k][zone_id] = z
            seen[event_id] = (now, {})
            return EventOutcome("ok", zone=z)

    def states(self) -> list[tuple[str, Zone]]:
        """(compte, zone) des zones déjà vues du locataire courant (présence ambiante)."""
        from nokker.tenancy.context import current_tenant

        t = current_tenant()
        with self._lock:
            return [(acc, z) for (tn, acc), zs in self._zones.items() if tn == t for z in zs.values() if z.state]

    def set_result(self, account: str, event_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            seen = self._events.get(self._key(account), {})
            if event_id in seen:
                seen[event_id] = (seen[event_id][0], dict(result))


class PgZoneStore:
    """Postgres (migration 075), connexion `nokker_app` sous RLS (locataire courant)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):
        from psycopg.rows import dict_row

        from nokker.store.conn import connect

        return connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _zone(r: dict[str, Any]) -> Zone:
        return Zone(id=r["zone_id"], name=r["name"], on_enter=tuple(r["on_enter"] or ()), on_exit=tuple(r["on_exit"] or ()),
                    home=bool(r["is_home"]), state=r["state"] or "", state_at=r["state_at"])

    def zones(self, account: str) -> list[Zone]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM member_zones WHERE account_id = %s ORDER BY position, zone_id",
                             (account,)).fetchall()
        return [self._zone(r) for r in rows]

    def replace(self, account: str, zones: list[Zone]) -> list[Zone]:
        with self._conn() as c, c.transaction():
            c.execute("DELETE FROM member_zones WHERE account_id = %s AND NOT (zone_id = ANY(%s))",
                      (account, [z.id for z in zones]))
            for i, z in enumerate(zones):
                c.execute("INSERT INTO member_zones (account_id, zone_id, name, on_enter, on_exit, is_home, position) "
                          "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (account_id, zone_id) DO UPDATE SET "
                          "name = EXCLUDED.name, on_enter = EXCLUDED.on_enter, on_exit = EXCLUDED.on_exit, "
                          "is_home = EXCLUDED.is_home, position = EXCLUDED.position, updated_at = now()",
                          (account, z.id, z.name, list(z.on_enter), list(z.on_exit), z.home, i))
            rows = c.execute("SELECT * FROM member_zones WHERE account_id = %s ORDER BY position, zone_id",
                             (account,)).fetchall()
        return [self._zone(r) for r in rows]

    def event(self, account: str, zone_id: str, event: str, at: datetime, event_id: str,
              now: datetime) -> EventOutcome:
        from psycopg.types.json import Jsonb

        with self._conn() as c, c.transaction():
            c.execute("DELETE FROM zone_events WHERE account_id = %s AND received_at < %s", (account, now - EVENT_KEEP))
            dup = c.execute("SELECT result FROM zone_events WHERE account_id = %s AND event_id = %s",
                            (account, event_id)).fetchone()
            if dup is not None:
                return EventOutcome("duplicate", result=dict(dup["result"] or {}))
            r = c.execute("SELECT * FROM member_zones WHERE account_id = %s AND zone_id = %s FOR UPDATE",
                          (account, zone_id)).fetchone()
            if r is None:
                return EventOutcome("unknown_zone")
            z = self._zone(r)
            if z.state_at is not None and at < z.state_at:
                return EventOutcome("out_of_order", zone=z)
            got = c.execute("INSERT INTO zone_events (account_id, event_id, zone_id, event, at, received_at, result) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING event_id",
                            (account, event_id, zone_id, event, at, now, Jsonb({}))).fetchone()
            if got is None:  # même event_id arrivé en parallèle : le premier gagne
                return EventOutcome("duplicate", result={})
            c.execute("UPDATE member_zones SET state = %s, state_at = %s WHERE account_id = %s AND zone_id = %s",
                      (STATE_OF[event], at, account, zone_id))
        return EventOutcome("ok", zone=replace(z, state=STATE_OF[event], state_at=at))

    def states(self) -> list[tuple[str, Zone]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM member_zones WHERE state <> ''").fetchall()
        return [(r["account_id"], self._zone(r)) for r in rows]

    def set_result(self, account: str, event_id: str, result: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        with self._conn() as c:
            c.execute("UPDATE zone_events SET result = %s WHERE account_id = %s AND event_id = %s",
                      (Jsonb(dict(result)), account, event_id))


__all__ = ["EVENTS", "MAX_ZONES", "NAME_MAX", "EventOutcome", "MemoryZoneStore", "PgZoneStore", "Zone", "ZoneError",
           "clean_event", "clean_name", "clean_zones"]
