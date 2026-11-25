"""Bornes de recharge en Suisse — ich-tanke-strom.ch (OFEN), fichiers OICP 2.3 de data.geo.admin.ch :
- données (opérateur, adresse, coordonnées, puissance, prises) :
  `https://data.geo.admin.ch/ch.bfe.ladestellen-elektromobilitaet/data/ch.bfe.ladestellen-elektromobilitaet.json`
  (~29 Mo, ~1,1 Mo compressé) ;
- disponibilité : `…/status/ch.bfe.ladestellen-elektromobilitaet.json` (~1,3 Mo), jointure sur `EvseID`.

Conditions : « Open use. Must provide the source. Use for commercial purposes requires permission of the data owner »
→ outil ABSENT tant que `essentials_v2.charging_permitted` n'est pas vrai (accord de l'OFEN, ich-tanke-strom@bfe.admin.ch).
Le fichier de statut n'a pas d'horodatage par borne : l'heure rendue est celle du fichier lu (en-tête Last-Modified
absent du chemin gardé) → « statut lu le … ». Aucun prix n'est rendu (jamais deviné).

La documentation de l'OFEN annonce une API OCPI 2.3 pour décembre 2026 (encore partielle le 05.11.2026) : bascule à
prévoir (docs/ESSENTIALS.md).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from nokker.tools.essentials.common import EssentialsError, RateLimit, Source, TTLCache, now_iso, num, plural
from nokker.tools.essentials.places_ch import Spot, haversine_m, walk

DATA_URL = "https://data.geo.admin.ch/ch.bfe.ladestellen-elektromobilitaet/data/ch.bfe.ladestellen-elektromobilitaet.json"
STATUS_URL = ("https://data.geo.admin.ch/ch.bfe.ladestellen-elektromobilitaet/status/"
              "ch.bfe.ladestellen-elektromobilitaet.json")
SOURCE_LINE = "Source : ich-tanke-strom.ch (Office fédéral de l'énergie), avec l'accord de l'OFEN."
# (singulier, pluriel) ; « hors service » et « disponibilité inconnue » se rendent « libellé : n ».
STATUS_FR = {"Available": ("libre", "libres"), "Occupied": ("occupée", "occupées"),
             "Reserved": ("réservée", "réservées"), "OutOfService": ("hors service", None),
             "Unknown": ("disponibilité inconnue", None), "EvseNotFound": ("disponibilité inconnue", None)}
UNKNOWN = STATUS_FR["Unknown"]


@dataclass(frozen=True)
class Evse:
    evse_id: str
    operator: str
    address: str
    lat: float
    lon: float
    power_kw: float | None
    plugs: tuple[str, ...]


def _evse(record: dict[str, Any], operator: str) -> Evse | None:
    try:
        lat_s, lon_s = str((record.get("GeoCoordinates") or {}).get("Google") or "").split()
        addr = record.get("Address") or {}
        powers = [float(f.get("power")) for f in record.get("ChargingFacilities") or []
                  if isinstance(f, dict) and f.get("power") not in (None, "")]
        street = " ".join(str(addr.get(k) or "").strip() for k in ("Street", "HouseNum")).strip()
        city = " ".join(str(addr.get(k) or "").strip() for k in ("PostalCode", "City")).strip()
        return Evse(evse_id=str(record["EvseID"]), operator=operator.strip() or "?",
                    address=", ".join(x for x in (street, city) if x) or "adresse inconnue",
                    lat=float(lat_s), lon=float(lon_s), power_kw=max(powers) if powers else None,
                    plugs=tuple(str(p) for p in record.get("Plugs") or []))
    except (KeyError, TypeError, ValueError):
        return None


@dataclass
class Charging:
    source: Source = field(default_factory=lambda: Source("ich-tanke-strom.ch", RateLimit(per_minute=10)))
    data_cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=3_600, max_items=1))
    status_cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=120, max_items=1))
    clock: Any = now_iso  # () -> heure ISO de la lecture du statut (injectable en test)

    def _evses(self) -> list[Evse]:
        cached = self.data_cache.get("evses")
        if cached is not None:
            return cached
        data = json.loads(self.source.get(DATA_URL, max_bytes=60_000_000).decode("utf-8", "replace"))
        out = []
        for op in data.get("EVSEData") or []:
            for rec in op.get("EVSEDataRecord") or []:
                e = _evse(rec, str(op.get("OperatorName") or ""))
                if e is not None:
                    out.append(e)
        if not out:
            raise EssentialsError("fichier des bornes illisible.", "source_erreur")
        self.data_cache.set("evses", out)
        return out

    def _status(self) -> tuple[dict[str, str], str]:
        cached = self.status_cache.get("status")
        if cached is not None:
            return cached
        data = json.loads(self.source.get(STATUS_URL, max_bytes=20_000_000).decode("utf-8", "replace"))
        status = {str(r.get("EvseID")): str(r.get("EVSEStatus")) for op in data.get("EVSEStatuses") or []
                  for r in op.get("EVSEStatusRecord") or [] if isinstance(r, dict)}
        value = (status, self.clock())
        self.status_cache.set("status", value)
        return value

    def render(self, spot: Spot, min_power_kw: float, radius_m: int) -> str:
        status, read_at = self._status()
        near = []
        for e in self._evses():
            if min_power_kw and (e.power_kw is None or e.power_kw < min_power_kw):
                continue
            d = haversine_m((spot.lat, spot.lon), (e.lat, e.lon))
            if d <= radius_m:
                near.append((d, e))
        near.sort(key=lambda t: t[0])
        floor = f", {num(min_power_kw)} kW au moins" if min_power_kw else ""
        lines = [f"Bornes de recharge autour de {spot.label}, rayon {radius_m} m{floor} (statut lu le {read_at}, "
                 "distances à vol d'oiseau) :"]
        # Un groupe par emplacement (adresse + opérateur + puissance + prises) : « 4 points de charge : 2 libres… ».
        groups: dict[tuple, list[tuple[float, Evse]]] = {}
        for d, e in near:
            groups.setdefault((e.address, e.operator, e.power_kw, e.plugs), []).append((d, e))
        ordered = sorted(groups.values(), key=lambda g: g[0][0])
        for g in ordered[:8]:
            d, e = g[0]
            counts: dict[tuple[str, str | None], int] = {}
            for _, x in g:
                st = STATUS_FR.get(status.get(x.evse_id, "Unknown"), UNKNOWN)
                counts[st] = counts.get(st, 0) + 1
            states = ", ".join(plural(n, sing, plur) if plur else f"{sing} : {n}" for (sing, plur), n in counts.items())
            power = f"{num(e.power_kw)} kW" if e.power_kw is not None else "puissance inconnue"
            plugs = ", ".join(e.plugs) or "prises inconnues"
            lines.append(f"- {e.address} ({e.operator}) : {plural(len(g), 'point de charge', 'points de charge')}, "
                         f"{power}, {plugs} ; {states} — {walk(d)}")
        if not near:
            lines.append("- aucune borne dans ce rayon.")
        elif len(ordered) > 8:
            lines.append(f"(… {plural(len(ordered) - 8, 'autre emplacement', 'autres emplacements')} dans le rayon)")
        lines.append("Tarifs : non indiqués ici (voir l'opérateur).")
        return "\n".join(lines)


__all__ = ["Charging", "SOURCE_LINE"]
