"""Qualité de l'air — indice ATMO officiel par commune française (Atmo France, agrégateur national des AASQA ;
Auvergne-Rhône-Alpes pour l'Ain et la Haute-Savoie), WFS sans clé :
`https://data.atmo-france.org/geoserver/ind/ows?service=WFS&version=2.0.0&request=GetFeature&typeNames=ind_atmo_2021`.
Licence ODbL 1.0 (notice Atmo Data, oct. 2024) : réutilisation libre, y compris commerciale, en citant « Atmo France
et les Associations agréées de surveillance de la qualité de l'air ». Échéances J à J+2, publiées vers 14 h.

Suisse : écartée dans cette vague. Les mesures NABEL courantes ne sont publiées que par un outil web sans licence
ouverte (conditions admin.ch : reproduction soumise à accord écrit) et Genève ne publie pas de flux ouvert
(docs/ESSENTIALS.md). L'outil répond hors_couverture pour un lieu suisse.

Échelle de l'indice ATMO (arrêté du 10 juillet 2020) : 1 bon, 2 moyen, 3 dégradé, 4 mauvais, 5 très mauvais,
6 extrêmement mauvais. Les sous-indices par polluant sont sur la MÊME échelle d'indice : ce ne sont pas des µg/m³.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from nokker.tools.essentials.common import EssentialsError, RateLimit, Source, TTLCache

URL = "https://data.atmo-france.org/geoserver/ind/ows"
SOURCE_LINE = ("Source : Atmo France et les Associations agréées de surveillance de la qualité de l'air (Atmo Data, "
               "ODbL 1.0) ; communes : geo.api.gouv.fr.")
SCALE = {1: "bon", 2: "moyen", 3: "dégradé", 4: "mauvais", 5: "très mauvais", 6: "extrêmement mauvais"}
POLLUTANTS = (("code_no2", "NO2"), ("code_o3", "O3"), ("code_pm10", "PM10"), ("code_pm25", "PM2.5"),
              ("code_so2", "SO2"))
_INSEE = re.compile(r"^[0-9][0-9AB][0-9]{3}$")


@dataclass
class Air:
    source: Source = field(default_factory=lambda: Source("Atmo France", RateLimit(per_minute=20)))
    cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=3_600, max_items=256))

    def render(self, place: str, meteo: Any, transit: Any, places: Any) -> tuple[str, str]:
        commune = places.resolve(place)
        if commune is None:
            swiss = meteo.exact_point(place)
            where = ("n'est pas une commune française (lieu suisse)" if swiss is not None
                     else ": commune française introuvable")
            raise EssentialsError(f"« {place} » {where}. Cet outil ne couvre que les communes françaises (indice "
                                  "ATMO) ; la Suisse ne publie pas de mesures de l'air en données ouvertes : pour la "
                                  "Suisse ou ailleurs, web_search.", "hors_couverture")
        if not _INSEE.match(commune.insee):
            raise EssentialsError("code commune illisible.", "source_erreur")
        params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": "ind_atmo_2021",
                  "outputFormat": "application/json", "CQL_FILTER": f"code_zone='{commune.insee}'"}
        url = f"{URL}?{urlencode(params)}"
        rows = self.cache.get(url)
        if rows is None:
            data = self.source.get_json(url, max_bytes=500_000)
            rows = [f.get("properties") or {} for f in data.get("features") or [] if isinstance(f, dict)]
            self.cache.set(url, rows)
        rows = sorted((r for r in rows if r.get("date_ech")), key=lambda r: str(r["date_ech"]))
        if not rows:
            raise EssentialsError(f"pas d'indice ATMO publié pour {commune.label}.", "introuvable")
        lines = [f"Indice ATMO pour {commune.label} (échelle de 1 « bon » à 6 « extrêmement mauvais » ; sous-indices "
                 f"par polluant sur la même échelle, pas des µg/m³), calcul du {rows[0].get('date_maj', '?')}, "
                 f"source régionale : {rows[0].get('source') or '?'} :"]
        for r in rows[:3]:
            code = r.get("code_qual")
            label = SCALE.get(code, str(r.get("lib_qual") or "?")) if isinstance(code, int) else "?"
            subs = ", ".join(f"{name} {r[k]}" for k, name in POLLUTANTS if isinstance(r.get(k), int))
            lines.append(f"- {r['date_ech']} : indice ATMO {code if code is not None else '?'} « {label} »"
                         + (f" ; sous-indices {subs}" if subs else ""))
        return "\n".join(lines), SOURCE_LINE


__all__ = ["Air", "SCALE", "SOURCE_LINE"]
