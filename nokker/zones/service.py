"""Service des zones de l'app (docs/LOKKAL-PHONE.md §4.2) : même code dans le noyau cloud et dans Lokkal.

- `put` / `get` : les zones du membre (`{id, name, on_enter, on_exit}`), jamais de coordonnées.
- `event` : une transition `enter|exit` du téléphone.
  - idempotent par `event_id` : un doublon rend `200 duplicate` ;
  - plus vieux que la dernière transition de la zone : 409 ; zone inconnue : 404 ;
  - plus de 30 min : présence seulement, scénarios `skipped` (`stale`).
  Sinon : présence (HA par le traceur `nokker_<membre>` sur Lokkal, présence ambiante, « Qui fait quoi »), puis les
  scénarios `on_enter`/`on_exit` au nom du membre (nokker/zones/scenarios.py).
- `scenarios` : `GET /v1/home/scenarios` → `{scenarios, bridge}` ; sans passerelle (`bridge: null`, cloud), présence
  seulement.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from nokker.zones.scenarios import SKIPPED, location_for
from nokker.zones.store import ZoneError, clean_event, clean_zones

STALE_AFTER = timedelta(minutes=30)


def _iso(v: datetime | None) -> str | None:
    return v.astimezone(UTC).isoformat() if v is not None else None


@dataclass
class ZoneService:
    store: Any  # MemoryZoneStore | PgZoneStore
    scenarios: Any = None  # ScenarioSource ; None = pas de maison (cloud) : présence seulement
    publish: Callable[[str, dict[str, Any]], Any] | None = None  # EventBus.publish (`zone.changed`, au membre seul)
    clock: Callable[[], float] = time.time

    @property
    def bridge(self) -> str | None:
        return getattr(self.scenarios, "bridge", None) if self.scenarios is not None else None

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), tz=UTC)

    def get(self, member: str) -> dict[str, Any]:
        return {"zones": [z.json() for z in self.store.zones(member)]}

    def put(self, member: str, body: Any) -> dict[str, Any]:
        zones = clean_zones(body)
        if self.scenarios is not None:  # sur une maison : un scénario inconnu ou d'un autre est refusé tout de suite
            mine = {s["id"] for s in self.scenarios.list(member)}
            bad = sorted({sid for z in zones for sid in (*z.on_enter, *z.on_exit)} - mine)
            if bad:
                raise ZoneError(400, "unknown_scenario", f"scénario inconnu ou pas à toi : {', '.join(bad)[:200]}")
        return {"zones": [z.json() for z in self.store.replace(member, zones)]}

    def list_scenarios(self, member: str) -> dict[str, Any]:
        items = self.scenarios.list(member) if self.scenarios is not None else []
        return {"scenarios": items, "bridge": self.bridge}

    def event(self, member: str, zone_id: str, body: Any) -> tuple[int, dict[str, Any]]:
        now = self._now()
        event, at, event_id = clean_event(body, now)
        got = self.store.event(member, zone_id, event, at, event_id, now)
        if got.kind == "duplicate":
            return 200, {"duplicate": True, **(got.result or {})}
        if got.kind == "unknown_zone":
            raise ZoneError(404, "zone_unknown", "zone inconnue")
        if got.kind == "out_of_order":
            raise ZoneError(409, "out_of_order", "événement plus ancien que la dernière transition de cette zone")
        zone = got.zone
        stale = now - at > STALE_AFTER
        if self.scenarios is not None:  # présence dans HA, même tardive (l'état courant reste vrai)
            self.scenarios.see(member, location_for(zone, event))
        if self.publish is not None:
            try:
                self.publish("zone.changed", {"member": member, "zone_id": zone.id, "name": zone.name,
                                              "state": zone.state, "at": _iso(at), "_audience": [member]})
            except Exception:  # noqa: BLE001 — la transition est écrite ; le flux n'est qu'un signal
                pass
        wanted = zone.on_enter if event == "enter" else zone.on_exit
        if stale:
            results = [{"id": sid, "status": SKIPPED, "reason": "stale"} for sid in wanted]
        elif self.scenarios is None:
            results = [{"id": sid, "status": SKIPPED, "reason": "no_bridge"} for sid in wanted]
        else:
            results = [self.scenarios.run(member, sid, zone=zone.name, event=event) for sid in wanted]
        out = {"queued": True, "scenarios": results}
        self.store.set_result(member, event_id, out)
        return 202, out

    def away(self) -> frozenset[str]:
        """Membres du locataire courant SORTIS de leur zone `home` (dernière transition) : présence ambiante."""
        try:
            rows = self.store.states()
        except Exception:  # noqa: BLE001 — sans les zones, la présence ambiante reste celle de la maison
            return frozenset()
        return frozenset(acc for acc, z in rows if z.home and z.state == "outside")

    def where(self, member: str) -> list[dict[str, Any]]:
        """Dernières transitions du membre (« Qui fait quoi », présence ambiante) : `{id, name, state, at}`."""
        return [{"id": z.id, "name": z.name, "state": z.state, "at": _iso(z.state_at)}
                for z in self.store.zones(member) if z.state]


__all__ = ["STALE_AFTER", "ZoneService"]
