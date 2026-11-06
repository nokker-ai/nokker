"""Météo et pollen suisses — données ouvertes de MétéoSuisse (OGD, CC BY 4.0, « Source: MeteoSwiss »).

- `weather_ch` : prévisions locales (collection STAC `ch.meteoschweiz.ogd-local-forecasting`), ~6 000 points (NPA,
  stations, sommets), valeurs journalières du jour local : min/max de température (`tre200pn`/`tre200px`), cumul de
  précipitations (`rka150p0`), pictogramme de jour (`jp2000d0`, textes officiels des symboles). Un fichier CSV par
  paramètre et par run horaire, pour TOUS les points : lu une fois par run et gardé en mémoire, jamais par question.
- `pollen` : mesures des 16 stations du réseau pollinique (collection `ch.meteoschweiz.ogd-pollen`), moyenne
  journalière de la veille (les données publiées vont jusqu'à hier).

Hors de Suisse : rien dans cette vague, l'outil le dit.
"""
from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

from nokker.tools.essentials.common import (
    Ambiguous,
    EssentialsError,
    NotFound,
    RateLimit,
    Source,
    TTLCache,
    fold,
)

BASE = "https://data.geo.admin.ch"
STAC_ITEM = BASE + "/api/stac/v1/collections/ch.meteoschweiz.ogd-local-forecasting/items/{day}-ch"
POINTS_URL = BASE + "/ch.meteoschweiz.ogd-local-forecasting/ogd-local-forecasting_meta_point.csv"
POLLEN_STATIONS_URL = BASE + "/ch.meteoschweiz.ogd-pollen/ogd-pollen_meta_stations.csv"
POLLEN_DATA_URL = BASE + "/ch.meteoschweiz.ogd-pollen/{abbr}/ogd-pollen_{abbr}_d_recent.csv"
SOURCE_LINE = "Source : MétéoSuisse (données ouvertes OGD, CC BY 4.0)."

PARAMS = ("tre200pn", "tre200px", "rka150p0", "jp2000d0")
_ASSET = re.compile(r"^vnut12\.lssw\.(\d{12})\.([a-z0-9]+)\.csv$")
_NPA = re.compile(r"\b(\d{4})\b")

# Pictogrammes de jour de MétéoSuisse (fichier officiel « Wettersymbole: Texte/Übersetzungen », version du 14.02.2022,
# colonne FR) — « le numéro et la description des symboles peuvent être utilisés » (doc OGD, prévisions locales).
SYMBOLS_FR = {
    1: "ensoleillé", 2: "assez ensoleillé, quelques passages nuageux",
    3: "en partie ensoleillé, passages nuageux parfois denses", 4: "nuageux", 5: "très nuageux",
    6: "éclaircies, averses isolées", 7: "éclaircies, averses isolées de pluie et neige mêlées",
    8: "éclaircies, giboulées de neige", 9: "nuageux, quelques averses",
    10: "nuageux, quelques averses de pluie et neige mêlées", 11: "nuageux, quelques averses de neige",
    12: "éclaircies, faiblement orageux", 13: "éclaircies, orageux", 14: "très nuageux, faibles pluies",
    15: "très nuageux, faibles chutes de pluie et neige mêlées", 16: "très nuageux, faibles chutes de neige",
    17: "très nuageux, pluies intermittentes", 18: "très nuageux, chutes intermittentes de pluie et neige mêlées",
    19: "très nuageux, chutes de neige intermittentes", 20: "très nuageux, pluie continue",
    21: "très nuageux, fréquentes chutes de pluie et neige mêlées", 22: "très nuageux, fortes chutes de neige",
    23: "très nuageux, faiblement orageux", 24: "très nuageux, orageux", 25: "très nuageux, très orageux",
    26: "nuages élevés", 27: "stratus", 28: "brouillard", 29: "belles éclaircies, averses éparses",
    30: "belles éclaircies, giboulées de neige", 31: "belles éclaircies, giboulées de pluie et de neige mêlées",
    32: "éclaircies, quelques averses", 33: "rares éclaircies, fréquentes averses",
    34: "rares éclaircies, fréquentes averses de neige", 35: "couvert et sec", 36: "partiellement ensoleillé, orageux",
    37: "partiellement ensoleillé, averses de neige orageuses", 38: "nuageux, averses orageuses",
    39: "nuageux, averses de neige orageuses", 40: "très nuageux, faiblement orageux",
    41: "nuageux, faiblement orageux", 42: "très nuageux, averses de neige orageuses",
}

POLLEN_TAXA = (("kaalnud1", "aune"), ("kabetud1", "bouleau"), ("kacoryd1", "noisetier"), ("kafagud1", "hêtre"),
               ("kafraxd1", "frêne"), ("kaquerd1", "chêne"), ("khpoacd1", "graminées"))

_WEEKDAYS = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")


@dataclass(frozen=True)
class Point:
    point_id: str
    point_type: str  # 1 station, 2 NPA, 3 sommet/col/cabane
    name: str
    postal_code: str
    lat: float
    lon: float

    @property
    def label(self) -> str:
        return f"{self.name} (NPA {self.postal_code})" if self.postal_code else self.name


def _csv_rows(body: bytes) -> list[dict[str, str]]:
    # Prévisions locales et pollen : Latin-1 (doc OGD) ; une erreur d'encodage ne casse rien (caractère remplacé).
    text = body.decode("latin-1", errors="replace")
    return list(csv.DictReader(io.StringIO(text), delimiter=";"))


def _float(value: str | None) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def _match(query: str, items: list[Any], name_of) -> Any:  # stations polliniques : 16 noms distincts
    """Égalité, puis début, puis contenu (forme repliée) ; le premier dans l'ordre de la liste gagne."""
    q = fold(query)
    if not q:
        return None
    for test in (lambda n: n == q, lambda n: n.startswith(q),
                 lambda n: f" {q} " in f" {n} "):
        for it in items:
            if test(fold(name_of(it))):
                return it
    return None


@dataclass
class MeteoSwiss:
    source: Source = field(default_factory=lambda: Source("MétéoSuisse", RateLimit(per_minute=30)))
    clock_utc: Any = None  # () -> datetime (UTC), injectable en test
    points_cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=86_400, max_items=2))
    item_cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=900, max_items=4))
    run_cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=7_200, max_items=8))
    answer_cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=900, max_items=512))
    pollen_cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=3_600, max_items=32))
    france_tool: bool = False  # vague 2 : weather_alerts_fr existe, l'erreur hors_couverture y renvoie

    def _now(self) -> datetime:
        return self.clock_utc() if self.clock_utc else datetime.now(timezone.utc)

    # --- points ---
    def points(self) -> list[Point]:
        cached = self.points_cache.get("points")
        if cached is not None:
            return cached
        rows = _csv_rows(self.source.get(POINTS_URL, max_bytes=4_000_000, accept="text/csv"))
        pts = []
        for r in rows:
            lat, lon = _float(r.get("point_coordinates_wgs84_lat")), _float(r.get("point_coordinates_wgs84_lon"))
            if not r.get("point_id") or lat is None or lon is None:
                continue
            pts.append(Point(point_id=r["point_id"].strip(), point_type=(r.get("point_type_id") or "").strip(),
                             name=(r.get("point_name") or "").strip(), postal_code=(r.get("postal_code") or "").strip(),
                             lat=lat, lon=lon))
        # Ordre de préférence : NPA (par numéro croissant), puis stations, puis sommets.
        order = {"2": 0, "1": 1, "3": 2}
        pts.sort(key=lambda p: (order.get(p.point_type, 3), p.postal_code or "9999", p.name))
        self.points_cache.set("points", pts)
        return pts

    def resolve(self, place: str) -> Point | None:
        """NPA exact, sinon nom (égal, puis début, puis contenu). Plusieurs lieux distincts (> 10 km l'un de l'autre)
        = `Ambiguous` avec la liste ; les NPA d'une même localité (Genève 1201…1209) sont un seul lieu, le plus petit
        NPA est pris et nommé dans la réponse."""
        pts = self.points()
        m = _NPA.search(place)
        if m:
            hit = next((p for p in pts if p.point_type == "2" and p.postal_code == m.group(1)), None)
            if hit is not None:
                return hit
            place = _NPA.sub(" ", place)
        q = fold(place)
        if not q:
            return None
        # « Buchs » = Buchs AG, Buchs LU, Buchs SG, Buchs ZH… : le nom suivi d'un code de canton compte comme égal.
        exact = re.compile(rf"{re.escape(q)}( [a-z]{{2}})?")
        for test in (lambda n: exact.fullmatch(n) is not None, lambda n: n.startswith(q),
                     lambda n: f" {q} " in f" {n} "):
            found = [p for p in pts if test(fold(p.name))]
            if not found:
                continue
            npa = [p for p in found if p.point_type == "2"]
            found = npa or found
            first = found[0]
            if all(_haversine((first.lat, first.lon), (p.lat, p.lon)) <= 10.0 for p in found):
                return first
            labels = list(dict.fromkeys(p.label for p in found))
            raise Ambiguous("lieu", place.strip(), labels, "Redemande avec le NPA du lieu voulu.")
        return None

    def exact_point(self, place: str) -> Point | None:
        """Localité suisse dont le nom égale exactement la demande (suffixe de canton admis), sinon None : sert à
        détecter un nom porté des deux côtés de la frontière (vague 2), sans les correspondances partielles."""
        q = fold(place)
        if not q:
            return None
        exact = re.compile(rf"{re.escape(q)}( [a-z]{{2}})?")
        return next((p for p in self.points() if p.point_type == "2" and exact.fullmatch(fold(p.name))), None)

    # --- prévisions ---
    def _latest_run(self) -> tuple[str, dict[str, str]]:
        """(horodatage du run AAAAMMJJHHMM UTC, {paramètre: url}) du dernier run complet."""
        today = self._now().date()
        for day in (today, today - timedelta(days=1)):
            key = day.strftime("%Y%m%d")
            assets = self.item_cache.get(key)
            if assets is None:
                try:
                    item = self.source.get_json(STAC_ITEM.format(day=key), max_bytes=3_000_000)
                except NotFound:
                    continue
                assets = {k: (a or {}).get("href", "") for k, a in (item.get("assets") or {}).items()}
                self.item_cache.set(key, assets)
            runs: dict[str, dict[str, str]] = {}
            for name, href in assets.items():
                m = _ASSET.match(name)
                if m and m.group(2) in PARAMS and href.startswith(BASE + "/"):
                    runs.setdefault(m.group(1), {})[m.group(2)] = href
            complete = sorted(r for r, ps in runs.items() if all(p in ps for p in PARAMS))
            if complete:
                return complete[-1], runs[complete[-1]]
        raise EssentialsError("aucune prévision locale publiée par MétéoSuisse ces deux derniers jours.")

    def _param(self, url: str, param: str) -> dict[tuple[str, str], list[tuple[str, float]]]:
        table = self.run_cache.get(url)
        if table is None:
            table = {}
            for r in _csv_rows(self.source.get(url, max_bytes=4_000_000, accept="text/csv")):
                v = _float(r.get(param))
                if v is None:
                    continue
                table.setdefault(((r.get("point_id") or "").strip(), (r.get("point_type_id") or "").strip()),
                                 []).append(((r.get("Date") or "").strip(), v))
            self.run_cache.set(url, table)
        return table

    def forecast(self, place: str, days: int) -> str:
        point = self.resolve(place)
        if point is None:
            raise OutOfCoverage(place, self.france_tool)
        run, urls = self._latest_run()
        key = f"{run}|{point.point_id}|{point.point_type}|{days}"
        cached = self.answer_cache.get(key)
        if cached is not None:
            return cached
        series = {p: dict(self._param(urls[p], p).get((point.point_id, point.point_type), [])) for p in PARAMS}
        dates = sorted(series["tre200px"])
        local_today = self._now().astimezone(ZoneInfo("Europe/Zurich")).strftime("%Y%m%d")  # jour local suisse
        dates = [d for d in dates if d[:8] >= local_today][:days] or dates[:days]
        if not dates:
            raise EssentialsError(f"pas de prévision pour {point.label} dans le dernier run.")
        run_dt = datetime.strptime(run, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        lines = [f"Prévisions locales MétéoSuisse pour {point.label}, run du {run_dt.isoformat()} ; jours locaux "
                 "suisses (Europe/Zurich) :"]
        for d in dates:
            day = datetime.strptime(d[:8], "%Y%m%d").date()
            sym = series["jp2000d0"].get(d)
            sky = SYMBOLS_FR.get(int(sym), f"pictogramme {int(sym)}") if sym is not None else "ciel non prévu"
            tmin, tmax, rain = series["tre200pn"].get(d), series["tre200px"].get(d), series["rka150p0"].get(d)
            temp = (f"température min {tmin:g} °C, max {tmax:g} °C" if tmin is not None and tmax is not None
                    else "température n/d")
            pluie = f"précipitations {rain:g} mm" if rain is not None else "précipitations n/d"
            lines.append(f"- {day.isoformat()} ({_WEEKDAYS[day.weekday()]}) : {sky} ; {temp} ; {pluie}")
        text = "\n".join(lines)
        self.answer_cache.set(key, text)
        return text

    # --- pollen ---
    def pollen(self, place: str) -> str:
        stations = self.pollen_cache.get("stations")
        if stations is None:
            stations = [r for r in _csv_rows(self.source.get(POLLEN_STATIONS_URL, max_bytes=200_000,
                                                              accept="text/csv")) if r.get("station_abbr")]
            self.pollen_cache.set("stations", stations)
        station = _match(place, stations, lambda r: r.get("station_name") or "")
        if station is None and len(place.strip()) == 2:
            station = next((r for r in stations if (r.get("station_canton") or "").upper() == place.strip().upper()),
                           None)
        if station is None:
            point = self.resolve(place)
            if point is None:
                raise OutOfCoverage(place)
            located = [(r, _float(r.get("station_coordinates_wgs84_lat")), _float(r.get("station_coordinates_wgs84_lon")))
                       for r in stations]
            located = [(r, la, lo) for r, la, lo in located if la is not None and lo is not None]
            if not located:
                raise EssentialsError("liste des stations polliniques illisible.")
            station = min(located, key=lambda t: _haversine((point.lat, point.lon), (t[1], t[2])))[0]
        abbr = station["station_abbr"].strip().lower()
        if not re.fullmatch(r"[a-z]{3}", abbr):
            raise EssentialsError("station pollinique inconnue.")
        key = f"data|{abbr}"
        text = self.pollen_cache.get(key)
        if text is not None:
            return text
        rows = _csv_rows(self.source.get(POLLEN_DATA_URL.format(abbr=abbr), max_bytes=2_000_000, accept="text/csv"))
        rows = [r for r in rows if any(_float(r.get(c)) is not None for c, _ in POLLEN_TAXA)]
        if not rows:
            raise EssentialsError(f"pas de mesure récente à la station {station.get('station_name')}.")
        last = rows[-1]
        raw_when = (last.get("reference_timestamp") or "").split(" ")[0]
        try:
            when = datetime.strptime(raw_when, "%d.%m.%Y").date().isoformat()
        except ValueError:
            when = raw_when
        values = []
        for col, label in POLLEN_TAXA:
            v = _float(last.get(col))
            values.append(f"{label} {v:g} grains/m³" if v is not None else f"{label} n/d")
        text = (f"Pollen mesuré à la station MétéoSuisse de {station.get('station_name')} ({station.get('station_canton')}),"
                f" moyenne journalière du {when} (00:00–24:00 UTC) : " + ", ".join(values) + ".\n"
                "Mesures de la veille, pas une prévision. Hors saison, les valeurs sont nulles.")
        self.pollen_cache.set(key, text)
        return text


class OutOfCoverage(EssentialsError):
    code = "hors_couverture"

    def __init__(self, place: str, france_tool: bool = False) -> None:
        # Vague 2 allumée (`weather_alerts_fr` présent) : renvoi vers weather_alerts_fr pour la France ; sinon texte de la vague 1.
        tail = ("Cet outil ne couvre que la Suisse ; pour une prévision à l'étranger, web_search ; pour les alertes en "
                "France, weather_alerts_fr." if france_tool
                else "Cet outil ne couvre que la Suisse ; pour l'étranger, web_search.")
        super().__init__(f"lieu « {place} » introuvable parmi les localités (NPA), stations et sommets suisses de "
                         f"MétéoSuisse. {tail}")


def parse_days(value: Any, default: int = 5) -> int:
    try:
        return max(1, min(9, int(value)))
    except (TypeError, ValueError):
        return default


__all__ = ["MeteoSwiss", "OutOfCoverage", "Point", "SYMBOLS_FR", "parse_days"]
