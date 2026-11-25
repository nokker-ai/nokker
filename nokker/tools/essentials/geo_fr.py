"""Communes françaises — API Découpage administratif (`https://geo.api.gouv.fr`, Licence Ouverte Etalab 2.0).

Sert à situer un lieu français (weather_alerts_fr, air_quality_fr, school_holidays) : nom ou code postal → commune (INSEE),
département, centre WGS84. Un nom porté par plusieurs communes distinctes = `Ambiguous` avec la liste (département
entre parenthèses), jamais un choix silencieux.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlencode

from nokker.tools.essentials.common import Ambiguous, RateLimit, Source, TTLCache, fold

BASE = "https://geo.api.gouv.fr/communes"
FIELDS = "nom,code,codesPostaux,codeDepartement,departement,centre,population"
SOURCE_NAME = "geo.api.gouv.fr (Etalab)"
_CP = re.compile(r"\b(\d{5})\b")


@dataclass(frozen=True)
class Commune:
    name: str
    insee: str
    department: str
    department_name: str
    postal_codes: tuple[str, ...]
    lat: float
    lon: float

    @property
    def label(self) -> str:
        return f"{self.name} ({self.department_name}, {self.department})"


def _commune(row: dict) -> Commune | None:
    try:
        lon, lat = (row.get("centre") or {}).get("coordinates") or (None, None)
        dep = row.get("departement") or {}
        return Commune(name=str(row["nom"]), insee=str(row["code"]), department=str(row.get("codeDepartement") or ""),
                       department_name=str(dep.get("nom") or row.get("codeDepartement") or ""),
                       postal_codes=tuple(str(c) for c in row.get("codesPostaux") or []),
                       lat=float(lat), lon=float(lon))
    except (KeyError, TypeError, ValueError):
        return None


@dataclass
class FrenchPlaces:
    source: Source = field(default_factory=lambda: Source(SOURCE_NAME, RateLimit(per_minute=40)))
    cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=86_400, max_items=1024))

    def _query(self, params: dict[str, str]) -> list[Commune]:
        url = f"{BASE}?{urlencode({**params, 'fields': FIELDS, 'format': 'json'})}"
        cached = self.cache.get(url)
        if cached is not None:
            return cached
        rows = self.source.get_json(url, max_bytes=1_000_000)
        out = [c for c in (_commune(r) for r in rows if isinstance(r, dict)) if c is not None] \
            if isinstance(rows, list) else []
        self.cache.set(url, out)
        return out

    def resolve(self, place: str) -> Commune | None:
        """Commune française, ou None (aucune) ; `Ambiguous` si plusieurs communes distinctes portent ce nom."""
        m = _CP.search(place)
        if m:
            found = self._query({"codePostal": m.group(1)})
            rest = fold(_CP.sub(" ", place))
            if rest:
                found = [c for c in found if fold(c.name) == rest] or found
            if len(found) == 1:
                return found[0]
            if found:
                raise Ambiguous("lieu", place.strip(), [f"{c.name} ({c.postal_codes[0] if c.postal_codes else ''})"
                                                         for c in found], "Redemande avec le nom de la commune.")
            return None
        q = fold(place)
        if not q:
            return None
        found = self._query({"nom": place.strip(), "boost": "population", "limit": "10"})
        exact = [c for c in found if fold(c.name) == q]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise Ambiguous("lieu", place.strip(), [c.label for c in exact],
                            "Redemande avec le code postal ou le département.")
        return None


__all__ = ["Commune", "FrenchPlaces", "SOURCE_NAME"]
