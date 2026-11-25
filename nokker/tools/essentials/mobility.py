"""Mobilité partagée en Suisse — sharedmobility.ch (OFEN), API REST `identify`
(`https://api.sharedmobility.ch/v1/sharedmobility/identify`), sans clé. Licence : « Open use. Must provide the source »,
usage commercial autorisé (dépôt SFOE/sharedmobility, « Access the data.md »).

Pièges mesurés le 05.11.2026 : `Tolerance` est une boîte (pas un cercle) et les résultats ne sont pas triés → refiltrés
et triés ici en haversine ; pages fixes de 50 (`offset`) ; aucun horodatage dans la réponse (l'heure rendue est celle
de la lecture) ; `vehicle_type` est une liste avec doublons ; une station disponible peut avoir `renting: false`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from nokker.tools.essentials.common import RateLimit, Source, TTLCache, now_iso, plural
from nokker.tools.essentials.places_ch import Spot, haversine_m, walk

URL = "https://api.sharedmobility.ch/v1/sharedmobility/identify"
SOURCE_LINE = "Source : sharedmobility.ch (Office fédéral de l'énergie), données ouvertes."
KINDS = {
    "velo": ("Bike", "E-Bike", "CargoBike", "E-CargoBike"),
    "trottinette": ("E-Scooter", "Scooter"),
    "voiture": ("Car", "E-Car"),
}
LABELS = {"Bike": "vélo", "E-Bike": "vélo électrique", "CargoBike": "vélo cargo", "E-CargoBike": "vélo cargo électrique",
          "E-Scooter": "trottinette électrique", "Scooter": "trottinette", "Car": "voiture",
          "E-Car": "voiture électrique", "E-Moped": "scooter électrique", "Moped": "scooter"}
MAX_PAGES = 6


@dataclass
class Mobility:
    source: Source = field(default_factory=lambda: Source("sharedmobility.ch", RateLimit(per_minute=30)))
    cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=60, max_items=256))
    clock: Any = now_iso  # () -> heure ISO de la lecture (injectable en test)

    def _features(self, spot: Spot, radius_m: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(MAX_PAGES):
            url = f"{URL}?{urlencode({'Geometry': f'{spot.lon},{spot.lat}', 'Tolerance': str(radius_m), 'offset': str(page * 50), 'geometryFormat': 'geojson'})}"
            data = self.source.get_json(url, max_bytes=3_000_000)
            rows = data if isinstance(data, list) else (data.get("results") or data.get("features") or [])
            rows = [r for r in rows if isinstance(r, dict)]
            out += rows
            if len(rows) < 50:
                break
        return out

    def render(self, spot: Spot, kind: str, radius_m: int) -> str:
        key = f"{spot.lat:.5f}|{spot.lon:.5f}|{kind}|{radius_m}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        wanted = None if kind == "tout" else set(KINDS[kind])
        items: list[tuple[float, str]] = []
        free: dict[tuple[str, str], list[float]] = {}
        for f in self._features(spot, radius_m):
            props = f.get("properties") or {}
            coords = (f.get("geometry") or {}).get("coordinates") or []
            try:
                lon, lat = float(coords[0]), float(coords[1])
            except (IndexError, TypeError, ValueError):
                continue
            types = list(dict.fromkeys(str(t) for t in props.get("vehicle_type") or []))
            if wanted is not None and not wanted.intersection(types):
                continue
            dist = haversine_m((spot.lat, spot.lon), (lat, lon))
            if dist > radius_m:
                continue  # `Tolerance` est une boîte : coins retirés
            provider = (props.get("provider") or {}).get("name") or "?"
            kinds = ", ".join(LABELS.get(t, t) for t in types) or "type inconnu"
            station = props.get("station")
            if isinstance(station, dict):
                status = station.get("status") or {}
                n = status.get("num_vehicle_available")
                renting = status.get("renting")
                avail = ("location fermée" if renting is False else
                         plural(int(n), "véhicule disponible", "véhicules disponibles") if isinstance(n, int)
                         else "disponibilité inconnue")
                items.append((dist, f"station « {station.get('name') or '?'} » ({provider}) : {kinds} ; {avail} — "
                                    f"{walk(dist)}"))
            else:
                veh = (props.get("vehicle") or {}).get("status") or {}
                if veh.get("disabled") or veh.get("reserved"):
                    continue
                free.setdefault((kinds, provider), []).append(dist)
        # Libre-service : un groupe par type et fournisseur (compte, le plus proche), pas une ligne par véhicule.
        for (kinds, provider), dists in free.items():
            n = len(dists)
            items.append((min(dists), f"{plural(n, 'véhicule', 'véhicules')} en libre-service ({kinds}, {provider}), "
                                      f"le plus proche à {walk(min(dists))}"))
        items.sort(key=lambda t: t[0])
        what = {"tout": "véhicules", "velo": "vélos", "trottinette": "trottinettes", "voiture": "voitures"}[kind]
        lines = [f"Mobilité partagée autour de {spot.label}, {what} dans un rayon de {radius_m} m (lu le {self.clock()}, "
                 f"distances à vol d'oiseau) :"]
        lines += [f"- {txt}" for _, txt in items[:12]] or ["- rien de disponible dans ce rayon."]
        if len(items) > 12:
            lines.append(f"(… {plural(len(items) - 12, 'autre offre', 'autres offres')} plus loin)")
        text = "\n".join(lines)
        self.cache.set(key, text)
        return text


__all__ = ["KINDS", "Mobility", "SOURCE_LINE"]
