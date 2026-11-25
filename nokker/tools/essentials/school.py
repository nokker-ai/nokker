"""Vacances scolaires — `school_holidays`.

France : calendrier scolaire officiel du ministère de l'Éducation nationale, jeu `fr-en-calendrier-scolaire` de
data.education.gouv.fr (Licence Ouverte 2.0 Etalab : réutilisation « à des fins commerciales ou non », mention de la
source et de la date de mise à jour). Une commune → son département (geo.api.gouv.fr) → son académie (table ci-dessous,
tirée de l'Annuaire de l'éducation `fr-en-annuaire-education`, Licence Ouverte 2.0, le 05.11.2026) → ses dates.
Dates de l'API en UTC : converties en Europe/Paris ; `start_date` = premier jour de vacances, `end_date` = jour de
reprise (dernier jour = veille), un pont d'un jour a les deux égaux.

Suisse : aucune source nationale ouverte (la CDIP ne publie qu'un PDF sans licence). Seuls les cantons qui publient
leurs dates en données ouvertes sont couverts : Bâle-Ville (data.bs.ch, jeu 100397, CC BY 4.0) et Bâle-Campagne
(data.bl.ch, jeu 13350, « Open use. Must provide the source »). Autres cantons : hors_couverture, renvoi vers
web_search.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from nokker.tools.essentials.common import EssentialsError, RateLimit, Source, TTLCache, fold
from nokker.tools.essentials.geo_fr import FrenchPlaces
from nokker.tools.essentials.holidays_ch import canton_code

FR_URL = "https://data.education.gouv.fr/api/explore/v2.1/catalog/datasets/fr-en-calendrier-scolaire/records"
CH_SOURCES = {
    "BS": ("https://data.bs.ch/api/explore/v2.1/catalog/datasets/100397/records", "start_date", "end_date", "name",
           "Source : Kanton Basel-Stadt, Schulferien (data.bs.ch, jeu 100397), CC BY 4.0."),
    "BL": ("https://data.bl.ch/api/explore/v2.1/catalog/datasets/13350/records", "startdatum", "enddatum", "name",
           "Source : Kanton Basel-Landschaft, Schulferien (data.bl.ch, jeu 13350), données ouvertes."),
}
FR_SOURCE = ("Source : ministère de l'Éducation nationale, calendrier scolaire (data.education.gouv.fr, Licence "
             "Ouverte 2.0) ; communes : geo.api.gouv.fr.")
PARIS = ZoneInfo("Europe/Paris")

# Département → académie (Annuaire de l'éducation, 05.11.2026 ; libellés du calendrier scolaire).
ACADEMY = {
    "01": "Lyon", "02": "Amiens", "03": "Clermont-Ferrand", "04": "Aix-Marseille", "05": "Aix-Marseille", "06": "Nice",
    "07": "Grenoble", "08": "Reims", "09": "Toulouse", "10": "Reims", "11": "Montpellier", "12": "Toulouse",
    "13": "Aix-Marseille", "14": "Normandie", "15": "Clermont-Ferrand", "16": "Poitiers", "17": "Poitiers",
    "18": "Orléans-Tours", "19": "Limoges", "21": "Dijon", "22": "Rennes", "23": "Limoges", "24": "Bordeaux",
    "25": "Besançon", "26": "Grenoble", "27": "Normandie", "28": "Orléans-Tours", "29": "Rennes", "2A": "Corse",
    "2B": "Corse", "30": "Montpellier", "31": "Toulouse", "32": "Toulouse", "33": "Bordeaux", "34": "Montpellier",
    "35": "Rennes", "36": "Orléans-Tours", "37": "Orléans-Tours", "38": "Grenoble", "39": "Besançon", "40": "Bordeaux",
    "41": "Orléans-Tours", "42": "Lyon", "43": "Clermont-Ferrand", "44": "Nantes", "45": "Orléans-Tours",
    "46": "Toulouse", "47": "Bordeaux", "48": "Montpellier", "49": "Nantes", "50": "Normandie", "51": "Reims",
    "52": "Reims", "53": "Nantes", "54": "Nancy-Metz", "55": "Nancy-Metz", "56": "Rennes", "57": "Nancy-Metz",
    "58": "Dijon", "59": "Lille", "60": "Amiens", "61": "Normandie", "62": "Lille", "63": "Clermont-Ferrand",
    "64": "Bordeaux", "65": "Toulouse", "66": "Montpellier", "67": "Strasbourg", "68": "Strasbourg", "69": "Lyon",
    "70": "Besançon", "71": "Dijon", "72": "Nantes", "73": "Grenoble", "74": "Grenoble", "75": "Paris",
    "76": "Normandie", "77": "Créteil", "78": "Versailles", "79": "Poitiers", "80": "Amiens", "81": "Toulouse",
    "82": "Toulouse", "83": "Nice", "84": "Aix-Marseille", "85": "Nantes", "86": "Poitiers", "87": "Limoges",
    "88": "Nancy-Metz", "89": "Dijon", "90": "Besançon", "91": "Versailles", "92": "Versailles", "93": "Créteil",
    "94": "Créteil", "95": "Versailles", "971": "Guadeloupe", "972": "Martinique", "973": "Guyane", "974": "Réunion",
    "975": "Saint Pierre et Miquelon", "976": "Mayotte",
}
ACADEMIES = sorted(set(ACADEMY.values()))
_WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def school_year(raw: str, today: date) -> str:
    v = raw.strip().replace("/", "-")
    if not v:
        start = today.year if today.month >= 8 else today.year - 1
        return f"{start}-{start + 1}"
    parts = v.split("-")
    try:
        a = int(parts[0])
        b = int(parts[1]) if len(parts) > 1 else a + 1
    except ValueError:
        raise EssentialsError(f"année scolaire illisible : « {raw} » (ex. 2026-2027).", "argument_invalide") from None
    if b != a + 1:
        raise EssentialsError(f"année scolaire illisible : « {raw} » (ex. 2026-2027).", "argument_invalide")
    return f"{a}-{b}"


def _local(raw: str) -> date | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(PARIS).date()
    except (TypeError, ValueError):
        return None


def _day(d: date) -> str:
    return f"{_WEEKDAYS[d.weekday()]} {d.isoformat()}"


@dataclass
class SchoolHolidays:
    places: FrenchPlaces = field(default_factory=FrenchPlaces)
    source: Source = field(default_factory=lambda: Source("data.education.gouv.fr", RateLimit(per_minute=30)))
    ch_source: Source = field(default_factory=lambda: Source("données cantonales", RateLimit(per_minute=20)))
    cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=86_400, max_items=256))
    today: Any = None  # () -> date, injectable en test

    def _today(self) -> date:
        return self.today() if self.today else datetime.now(PARIS).date()

    def render(self, region: str, year_raw: str) -> tuple[str, str]:
        """(texte, ligne de source)."""
        year = school_year(year_raw, self._today())
        r = region.strip()
        f = fold(r)
        if f in ("zone a", "zone b", "zone c", "a", "b", "c"):
            zone = "Zone " + f[-1].upper()
            return self._france(f"zones=\"{zone}\"", zone, year), FR_SOURCE
        academy = next((a for a in ACADEMIES if fold(a) == f or fold(f"academie de {a}") == f), None)
        if academy:
            return self._france(f"location=\"{academy}\"", f"académie de {academy}", year), FR_SOURCE
        canton = canton_code(r) if len(r) <= 30 else None
        if canton:
            if canton not in CH_SOURCES:
                raise EssentialsError(f"le canton {canton} ne publie pas ses vacances scolaires en données ouvertes "
                                      "(seuls BS et BL sont couverts) ; pour ce canton, web_search", "hors_couverture")
            return self._suisse(canton, year), CH_SOURCES[canton][4]
        commune = self.places.resolve(r)
        if commune is None:
            raise EssentialsError(f"« {r} » n'est ni une zone (A, B, C), ni une académie, ni une commune française, ni "
                                  "un canton suisse couvert (BS, BL) ; ailleurs, web_search.", "hors_couverture")
        academy = ACADEMY.get(commune.department)
        if not academy:
            raise EssentialsError(f"académie inconnue pour le département {commune.department}.", "introuvable")
        return self._france(f"location=\"{academy}\"", f"{commune.label}, académie de {academy}", year), FR_SOURCE

    def _france(self, where: str, label: str, year: str) -> str:
        params = {"where": f"{where} and annee_scolaire=\"{year}\"", "order_by": "start_date", "limit": "100"}
        url = f"{FR_URL}?{urlencode(params)}"
        rows = self.cache.get(url)  # les lignes, pas le texte : le libellé (commune) change d'un appel à l'autre
        if rows is None:
            rows = self.source.get_json(url, max_bytes=2_000_000).get("results") or []
            self.cache.set(url, rows)
        seen, lines = set(), []
        zone = None
        for row in rows:
            if str(row.get("population") or "-") not in ("-", "Élèves"):
                continue  # l'été apparaît aussi pour les enseignants
            start, end = _local(row.get("start_date")), _local(row.get("end_date"))
            name = str(row.get("description") or "?")
            if start is None or (name, start, end) in seen:
                continue
            seen.add((name, start, end))
            zone = zone or row.get("zones")
            if end is None or end <= start:
                lines.append(f"- {name} : le {_day(start)}")
            else:
                lines.append(f"- {name} : du {_day(start)} au {_day(end - timedelta(days=1))} (reprise le {_day(end)})")
        if not lines:
            raise EssentialsError(f"pas de calendrier publié pour {label} en {year}.", "introuvable")
        head = f"Vacances scolaires {year}, {label}" + (f" ({zone})" if zone and zone not in label else "") + " :"
        return "\n".join([head, *lines])

    def _suisse(self, canton: str, year: str) -> str:
        url_base, start_f, end_f, name_f, _src = CH_SOURCES[canton]
        a = int(year[:4])
        params = {"where": f"{start_f}>=date'{a}-08-01' and {start_f}<date'{a + 1}-08-01'", "order_by": start_f,
                  "limit": "100"}
        url = f"{url_base}?{urlencode(params)}"
        text = self.cache.get(url)
        if text is not None:
            return text
        rows = self.ch_source.get_json(url, max_bytes=1_000_000).get("results") or []
        lines = []
        for row in rows:
            try:
                s, e = date.fromisoformat(str(row[start_f])[:10]), date.fromisoformat(str(row[end_f])[:10])
            except (KeyError, ValueError):
                continue
            lines.append(f"- {row.get(name_f) or '?'} : " + (f"le {_day(s)}" if s == e else f"du {_day(s)} au {_day(e)}"))
        if not lines:
            raise EssentialsError(f"pas de calendrier publié pour le canton {canton} en {year}.", "introuvable")
        text = "\n".join([f"Vacances scolaires {year}, canton {canton} (dates du canton ; les communes peuvent "
                          "différer) :", *lines])
        self.cache.set(url, text)
        return text


__all__ = ["ACADEMY", "SchoolHolidays", "school_year"]
