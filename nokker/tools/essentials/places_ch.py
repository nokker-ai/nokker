"""Lieu suisse → coordonnées WGS84, pour les outils « autour d'un lieu » de la vague 2 (mobilité, recharge, eau, air).

Ordre : points de MétéoSuisse (NPA, localités, stations, sommets — même résolution et même règle d'ambiguïté que
weather_ch), puis arrêts de l'horaire suisse (transport.opendata.ch `/locations`, coordonnées de l'arrêt). Distances
à vol d'oiseau (haversine) ; temps à pied à 4,8 km/h (80 m/min), arrondi à la minute supérieure.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from urllib.parse import urlencode

from nokker.tools.essentials.common import Ambiguous, EssentialsError, fold

WALK_M_PER_MIN = 80.0


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6_371_000.0 * 2 * math.asin(math.sqrt(h))


def walk(distance_m: float) -> str:
    """« 240 m, ≈ 3 min à pied »."""
    return f"{round(distance_m):d} m, ≈ {max(1, math.ceil(distance_m / WALK_M_PER_MIN))} min à pied"


@dataclass(frozen=True)
class Spot:
    label: str
    lat: float
    lon: float


def resolve_ch(place: str, meteo, transit) -> Spot | None:
    """Lieu suisse ; `Ambiguous` remonte tel quel ; None si ni localité ni arrêt. Seule exception à l'ambiguïté :
    des arrêts de même nom à moins de 300 m l'un de l'autre sont un seul lieu (le nom retenu est affiché)."""
    point = meteo.resolve(place)
    if point is not None:
        return Spot(point.label, point.lat, point.lon)
    data = transit.source.get_json(f"https://transport.opendata.ch/v1/locations?{urlencode({'query': place})}")
    stations = [s for s in data.get("stations") or [] if isinstance(s, dict) and s.get("name")
                and (s.get("coordinate") or {}).get("x") is not None]
    if not stations:
        return None
    exact = [s for s in stations if fold(str(s["name"])) == fold(place)]

    def _close(group: list[dict]) -> bool:  # arrêts homonymes au même endroit (« Lausanne, Flon » / « Lausanne-Flon »)
        c0 = group[0]["coordinate"]
        return all(haversine_m((float(c0["x"]), float(c0["y"])), (float(g["coordinate"]["x"]),
                                                                  float(g["coordinate"]["y"]))) <= 300 for g in group)

    if len(exact) == 1 or len(stations) == 1 or (len(exact) > 1 and _close(exact)):
        s = exact[0] if exact else stations[0]
    else:
        raise Ambiguous("lieu", place.strip(), list(dict.fromkeys(str(s["name"]) for s in (exact or stations))))
    try:
        return Spot(str(s["name"]), float(s["coordinate"]["x"]), float(s["coordinate"]["y"]))
    except (TypeError, ValueError, KeyError):
        raise EssentialsError("coordonnées de l'arrêt illisibles.", "source_erreur") from None


__all__ = ["Spot", "WALK_M_PER_MIN", "haversine_m", "resolve_ch", "walk"]
