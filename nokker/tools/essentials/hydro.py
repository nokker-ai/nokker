"""Rivières et lacs suisses — mesures hydrologiques actuelles de l'OFEV via LINDAS (`https://ld.admin.ch/query`,
graphe `https://lindas.admin.ch/foen/hydro`, jeu « Données de mesure hydrologiques actuelles de l'OFEV »).
Conditions : `https://ld.admin.ch/vocabulary/TermsOfUse/Open-Use` (« Utilisation libre » : usage commercial autorisé,
source recommandée). Rafraîchi toutes les 10 min ; une requête SPARQL (GET, CSV ~43 Ko) donne la dernière mesure de
chaque station (~230). Unités du jeu : niveau en m, débit en m³/s, température en °C.

Pièges : quelques stations ont une mesure ancienne (rendue avec sa date et signalée) ; quelques petites stations
publient une hauteur relative à leur échelle plutôt qu'une altitude (valeur < 190 m : rendue « hauteur à l'échelle »,
déduction signalée dans docs/ESSENTIALS.md). Les lacs n'ont que le niveau.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from nokker.tools.essentials.common import Ambiguous, EssentialsError, RateLimit, Source, TTLCache, fold, num
from nokker.tools.essentials.places_ch import haversine_m, resolve_ch

URL = "https://ld.admin.ch/query"
SOURCE_LINE = "Source : Office fédéral de l'environnement (OFEV), données hydrologiques actuelles (LINDAS), utilisation libre."
QUERY = """PREFIX schema: <http://schema.org/>
PREFIX geo: <http://www.opengis.net/ont/geosparql#>
PREFIX cube: <https://cube.link/>
PREFIX dim: <https://environment.ld.admin.ch/foen/hydro/dimension/>
SELECT ?id ?name ?waterbody ?wbType ?wkt ?time ?waterLevel ?discharge ?waterTemperature
FROM <https://lindas.admin.ch/foen/hydro>
WHERE {
  { SELECT ?station (MAX(?t) AS ?time) WHERE {
      ?o a cube:Observation ; dim:station ?station ; dim:measurementTime ?t } GROUP BY ?station }
  ?obs a cube:Observation ; dim:station ?station ; dim:measurementTime ?time .
  ?station schema:identifier ?id ; schema:name ?name .
  OPTIONAL { ?station geo:hasGeometry/geo:asWKT ?wkt }
  OPTIONAL { ?station schema:containedInPlace ?wb . ?wb schema:name ?waterbody .
             OPTIONAL { ?wb schema:additionalType ?wbType } }
  OPTIONAL { ?obs dim:waterLevel ?waterLevel }
  OPTIONAL { ?obs dim:discharge ?discharge }
  OPTIONAL { ?obs dim:waterTemperature ?waterTemperature }
}
ORDER BY ?id"""
_WKT = re.compile(r"POINT\(\s*([-\d.]+)\s+([-\d.]+)\s*\)")
STALE = timedelta(hours=6)
NEAR_M = 15_000


@dataclass(frozen=True)
class Station:
    sid: str
    name: str
    water: str
    lake: bool
    lat: float
    lon: float
    time: str
    level: float | None
    discharge: float | None
    temperature: float | None


def _f(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _stations(body: bytes) -> list[Station]:
    out = []
    for r in csv.DictReader(io.StringIO(body.decode("utf-8", "replace"))):
        m = _WKT.search(r.get("wkt") or "")
        if not m or not r.get("id"):
            continue
        out.append(Station(sid=r["id"], name=(r.get("name") or "").strip(), water=(r.get("waterbody") or "").strip(),
                           lake=(r.get("wbType") or "").endswith("/lake"), lat=float(m.group(2)),
                           lon=float(m.group(1)), time=(r.get("time") or "").strip(), level=_f(r.get("waterLevel")),
                           discharge=_f(r.get("discharge")), temperature=_f(r.get("waterTemperature"))))
    return out


def _label(s: Station) -> str:
    return f"{s.water} à {s.name}" if s.water else s.name


@dataclass
class Water:
    source: Source = field(default_factory=lambda: Source("OFEV (LINDAS)", RateLimit(per_minute=20)))
    cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=600, max_items=1))
    clock_utc: Any = None

    def stations(self) -> list[Station]:
        cached = self.cache.get("all")
        if cached is not None:
            return cached
        body = self.source.get(f"{URL}?{urlencode({'query': QUERY})}", max_bytes=3_000_000, accept="text/csv")
        out = _stations(body)
        if not out:
            raise EssentialsError("réponse de LINDAS illisible.", "source_erreur")
        self.cache.set("all", out)
        return out

    def _pick(self, place: str, meteo: Any, transit: Any) -> tuple[list[Station], str]:
        stations, q = self.stations(), fold(place)
        by_station = [s for s in stations if fold(s.name) == q or fold(_label(s)) == q]
        if by_station:
            return by_station[:1], ""
        by_water = [s for s in stations if fold(s.water) == q or fold(s.water).replace("lac ", "") == q]
        if len(by_water) == 1:
            return by_water, ""
        if by_water:
            raise Ambiguous("cours d'eau", place.strip(), [_label(s) for s in by_water],
                            f"Redemande avec la station (ex. « {_label(by_water[0])} ») ou une localité.")
        partial = [s for s in stations if q and (f" {q} " in f" {fold(s.name)} " or f" {q} " in f" {fold(_label(s))} ")]
        if len(partial) == 1:
            return partial, ""
        if len(partial) > 1:
            raise Ambiguous("station", place.strip(), [_label(s) for s in partial])
        spot = resolve_ch(place, meteo, transit)
        if spot is None:
            raise EssentialsError(f"« {place} » : ni rivière, ni lac, ni station, ni localité suisse. Cet outil ne "
                                  "couvre que la Suisse ; ailleurs, web_search.", "hors_couverture")
        near = sorted(((haversine_m((spot.lat, spot.lon), (s.lat, s.lon)), s) for s in stations),
                      key=lambda t: t[0])
        near = [(d, s) for d, s in near if d <= NEAR_M]
        if not near:
            raise EssentialsError(f"aucune station de l'OFEV à moins de 15 km de {spot.label}.", "introuvable")
        # Une station par cours d'eau ou lac, les trois plus proches : la plus proche n'est pas forcément l'eau voulue.
        seen, picked = set(), []
        for d, s in near:
            if s.water not in seen:
                seen.add(s.water)
                picked.append(s)
            if len(picked) == 3:
                break
        return picked, f"stations de l'OFEV les plus proches de {spot.label} (une par cours d'eau ou lac)"

    def render(self, place: str, meteo: Any, transit: Any) -> str:
        picked, intro = self._pick(place, meteo, transit)
        now = self.clock_utc() if self.clock_utc else datetime.now(timezone.utc)
        lines = [intro + " :"] if intro else []
        for s in picked:
            parts = []
            if s.level is not None:
                parts.append(f"niveau {num(s.level)} m d'altitude" if s.level >= 190
                             else f"hauteur à l'échelle {num(s.level)} m")
            if s.discharge is not None:
                parts.append(f"débit {num(s.discharge)} m³/s")
            if s.temperature is not None:
                parts.append(f"température de l'eau {num(s.temperature)} °C")
            if not parts:
                parts.append("aucune mesure publiée")
            stale = ""
            try:
                if now - datetime.fromisoformat(s.time) > STALE:
                    stale = " — MESURE ANCIENNE"
            except ValueError:
                stale = " — date de mesure illisible"
            kind = "lac" if s.lake else "rivière"
            lines.append(f"- {_label(s)} ({kind}, station {s.sid}), mesure du {s.time}{stale} : {' ; '.join(parts)}")
        return "\n".join(lines)


__all__ = ["SOURCE_LINE", "Water"]
