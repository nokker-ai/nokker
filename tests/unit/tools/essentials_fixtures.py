"""Données simulées des sources Essentiels (format réel des API, relevé le 05.11.2026) et exemples rendus par le code
pour docs/ESSENTIALS.md (« Exemples de résultats » : un test vérifie que le doc les reprend à l'identique).

Régénérer les sections générées du doc (familles, textes, exemples) : `python -m tests.unit.tools.essentials_fixtures`.
Idempotent : tout ce qui suit le premier titre généré est réécrit, rien n'est ajouté à la suite.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from nokker.core.ports import ToolContext
from nokker.tools.essentials import EssentialsSource
from nokker.tools.essentials import fx, meteo, slf, transit, wiki
from nokker.tools.essentials.common import RateLimit, Source

CTX = ToolContext(thread_id="t1", turn_index=0, channel="api")


class FakeNet:
    """Récupérateur simulé : {préfixe d'URL: (statut, corps)} ; garde la trace des URL demandées."""

    def __init__(self, routes: dict[str, tuple[int, bytes | str]]):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str, max_bytes: int, accept: str,
                 headers: dict[str, str] | None = None) -> tuple[int, str, bytes]:
        self.calls.append(url)
        self.headers = headers
        for prefix in sorted(self.routes, key=len, reverse=True):
            if url.startswith(prefix):
                status, body = self.routes[prefix]
                raw = body.encode("latin-1" if url.endswith(".csv") else "utf-8") if isinstance(body, str) else body
                return status, "application/octet-stream", raw
        return 404, "text/plain", b"not found"


def src(net: FakeNet, per_minute: int = 1000) -> Source:
    return Source("test", RateLimit(per_minute=per_minute), fetcher=net)


# ─────────────────────────────── MétéoSuisse ───────────────────────────────

POINTS = (
    "point_id;point_type_id;station_abbr;postal_code;point_name;point_type_de;point_type_fr;point_type_it;"
    "point_type_en;point_height_masl;point_coordinates_lv95_east;point_coordinates_lv95_north;"
    "point_coordinates_wgs84_lat;point_coordinates_wgs84_lon\n"
    "58;1;GVE;;Genève / Cointrin;Station;Station;Stazione;Station;411.0;0;0;46.247519;6.127742\n"
    "120200;2;;1202;Genève;Z;C;C;P;406.0;0;0;46.216103;6.136864\n"
    "120100;2;;1201;Genève;Z;C;C;P;381.0;0;0;46.209839;6.143739\n"
    "195000;2;;1950;Sion;Z;C;C;P;491.0;0;0;46.2311;7.3589\n"
    "1746;1;;;Buchs;Station;Station;Stazione;Station;470.0;0;0;47.205894;8.040483\n"
    "503300;2;;5033;Buchs AG;Z;C;C;P;400.0;0;0;47.390358;8.077114\n"
    "947000;2;;9470;Buchs SG;Z;C;C;P;450.0;0;0;47.162414;9.475725\n"
)
RUN_OLD, RUN_NEW = "202611051400", "202611051500"
ITEM_URL = "https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-local-forecasting/items/20261105-ch"
POLLEN_STATIONS = ("station_abbr;station_name;station_canton;station_coordinates_wgs84_lat;"
                   "station_coordinates_wgs84_lon\nPGE;Genève;GE;46.191969;6.147544\nPSN;Sion;VS;46.235403;7.384606\n"
                   "PZH;Zürich;ZH;47.378225;8.565644\n")


def asset(run: str, p: str) -> tuple[str, dict[str, str]]:
    name = f"vnut12.lssw.{run}.{p}.csv"
    return name, {"href": f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-local-forecasting/20261105-ch/{name}"}


def _param_csv(p: str, values: dict[str, list[float]]) -> str:
    lines = [f"point_id;point_type_id;Date;{p}"]
    for pid, vals in values.items():
        for i, v in enumerate(vals):
            lines.append(f"{pid};2;2026110{5 + i}0000;{v}")
    return "\n".join(lines) + "\n"


def meteo_net() -> FakeNet:
    assets = dict(asset(RUN_NEW, p) for p in meteo.PARAMS)
    assets.update(dict(asset(RUN_OLD, p) for p in meteo.PARAMS))
    assets.update(dict([asset("202611051600", "tre200px")]))  # run de 16:00 incomplet : ignoré
    data = {"tre200pn": [6.6, 6.0, 4.8], "tre200px": [9.3, 9.6, 10.1], "rka150p0": [1.1, 3.4, 0.0],
            "jp2000d0": [29, 17, 99]}
    routes: dict[str, tuple[int, bytes | str]] = {
        meteo.POINTS_URL: (200, POINTS),
        ITEM_URL: (200, json.dumps({"assets": assets})),
        meteo.POLLEN_STATIONS_URL: (200, POLLEN_STATIONS),
    }
    for p in meteo.PARAMS:
        routes[asset(RUN_NEW, p)[1]["href"]] = (200, _param_csv(p, {"120100": data[p], "195000": data[p]}))
    head = "station_abbr;reference_timestamp;" + ";".join(c for c, _ in meteo.POLLEN_TAXA)
    routes[meteo.POLLEN_DATA_URL.format(abbr="psn")] = (
        200, head + "\nPSN;03.11.2026 00:00;" + ";".join(["0"] * 7) + "\nPSN;04.11.2026 00:00;1;2;3;4;5;6;7\n")
    return FakeNet(routes)


def meteo_service(net: FakeNet) -> meteo.MeteoSwiss:
    return meteo.MeteoSwiss(source=src(net), clock_utc=lambda: datetime(2026, 11, 5, 16, 30, tzinfo=timezone.utc))


# ─────────────────────────────── transports ───────────────────────────────

LOCATIONS = {
    "Lausanne": [{"id": "8501120", "name": "Lausanne"}, {"id": "8591988", "name": "Lausanne, Bel-Air"}],
    "Genève": [{"id": "8501008", "name": "Genève"}, {"id": "8587057", "name": "Genève, gare Cornavin"}],
    "Buchs": [{"id": "8502123", "name": "Buchs AG"}, {"id": "8509404", "name": "Buchs SG"},
              {"id": "8503527", "name": "Buchs-Dällikon"}],
    "Lausanne, Flon": [{"id": "8591818", "name": "Lausanne, Flon"}, {"id": "8501181", "name": "Lausanne-Flon"}],
}
BOARD = {"station": {"name": "Lausanne"}, "stationboard": [
    {"category": "IR", "number": "15", "to": "Genève Aéroport",
     "stop": {"departure": "2026-11-05T18:55:00+0100", "delay": 3, "platform": "6",
              "prognosis": {"platform": "7"}}},
    {"category": "M", "number": "2", "to": "Croisettes",
     "stop": {"departure": "2026-11-05T18:57:00+0100", "delay": 0, "platform": None, "prognosis": {}}}]}
TRIP = {"connections": [{
    "from": {"station": {"name": "Lausanne"}, "departure": "2026-11-05T19:00:00+0100", "delay": 2, "platform": "8",
             "prognosis": {}},
    "to": {"station": {"name": "Genève"}, "arrival": "2026-11-05T19:51:00+0100", "delay": 0, "platform": "2"},
    "duration": "00d00:49:00", "transfers": 0,
    "sections": [{"journey": {"category": "RE", "number": "33"},
                  "departure": {"station": {"name": "Lausanne"}, "departure": "2026-11-05T19:00:00+0100",
                                "delay": 2, "platform": "8"},
                  "arrival": {"station": {"name": "Genève"}, "arrival": "2026-11-05T19:51:00+0100"}}]}]}


def transit_net() -> FakeNet:
    from urllib.parse import urlencode

    routes: dict[str, tuple[int, bytes | str]] = {
        transit.BASE + "/stationboard?": (200, json.dumps(BOARD)),
        transit.BASE + "/connections?": (200, json.dumps(TRIP)),
    }
    for q, stations in LOCATIONS.items():
        for extra in ({"type": "station"}, {}):
            routes[f"{transit.BASE}/locations?{urlencode({'query': q, **extra})}"] = (
                200, json.dumps({"stations": stations}))
    return FakeNet(routes)


# ─────────────────────────────── BCE, Wikipédia, SLF ───────────────────────────────

ECB = """<?xml version="1.0" encoding="UTF-8"?><gesmes:Envelope><Cube><Cube time='2026-11-05'>
<Cube currency='USD' rate='1.1224'/><Cube currency='CHF' rate='0.9350'/></Cube></Cube></gesmes:Envelope>"""

CERVIN = {"title": "Cervin", "type": "standard", "description": "sommet des Alpes",
          "timestamp": "2026-07-04T03:41:00Z",
          "extract": "Le Cervin est, par son altitude de 4 478 mètres, le douzième sommet des Alpes.",
          "content_urls": {"desktop": {"page": "https://evil.example/"}}}

BULLETIN = {"bulletins": [{
    "publicationTime": "2026-02-01T07:00:00Z", "nextUpdate": "2026-02-01T16:00:00Z",
    "validTime": {"startTime": "2026-02-01T07:00:00Z", "endTime": "2026-02-01T16:00:00Z"},
    "regions": [{"regionID": "CH-5111", "name": "Davos"}, {"regionID": "CH-5112", "name": "Prättigau"}],
    "dangerRatings": [{"mainValue": "considerable", "validTimePeriod": "all_day"}],
    "avalancheProblems": [{"problemType": "persistent_weak_layers", "elevation": {"lowerBound": 2200},
                           "aspects": ["N", "NE"], "comment": "<p>Couches fragiles.</p>"}]}]}


def full_source() -> EssentialsSource:
    """Les cinq services sur transport simulé (aucun réseau)."""
    return EssentialsSource(
        meteo=meteo_service(meteo_net()),
        transit=transit.Transit(source=src(transit_net())),
        fx=fx.Fx(source=src(FakeNet({fx.URL: (200, ECB)}))),
        wiki=wiki.Wiki(source=src(FakeNet({
            "https://fr.wikipedia.org/api/rest_v1/page/summary/Cervin": (200, json.dumps(CERVIN))}))),
        avalanche=slf.Avalanche(source=src(FakeNet({slf.URL.format(lang="fr"): (200, json.dumps(BULLETIN))}))),
    )


# Un appel par outil, dans l'ordre des textes : ce que le doc montre comme « exemple de résultat ».
EXAMPLE_CALLS: list[tuple[str, dict]] = [
    ("weather_ch", {"place": "Genève", "days": 2}),
    ("pollen", {"place": "1950"}),
    ("transit_departures", {"station": "Lausanne"}),
    ("transit_trip", {"from": "Lausanne", "to": "Genève"}),
    ("fx_rate", {"from": "CHF", "to": "USD", "amount": 100}),
    ("holidays", {"year": 2026, "canton": "GE"}),
    ("wiki_summary", {"title": "Cervin", "lang": "fr"}),
    ("avalanche_bulletin", {"region": "Davos"}),
    ("unit_convert", {"value": 10, "from_unit": "km", "to_unit": "mi"}),
    ("math_calc", {"expression": "(12.5*3)^2"}),
    ("date_calc", {"start": "2026-11-05", "end": "2026-12-25", "days": 30}),
]
ERROR_EXAMPLES: list[tuple[str, dict]] = [
    ("weather_ch", {"place": "Buchs"}),
    ("weather_ch", {"place": "Paris"}),
    ("transit_departures", {"station": "Buchs"}),
]


def render_examples() -> list[tuple[str, dict, str]]:
    s = full_source()
    return [(n, a, s.call(n, a, CTX).content) for n, a in EXAMPLE_CALLS + ERROR_EXAMPLES]


def doc_section() -> str:
    parts = []
    for name, args, content in render_examples():
        parts.append(f"#### `{name}({json.dumps(args, ensure_ascii=False)})`\n\n```text\n{content}\n```\n")
    return "\n".join(parts)


FAMILIES_TITLE = "## Familles et mots déclencheurs (`FAMILIES`, `nokker/tools/select.py`)"
TEXTS_TITLE = "## Textes vus par le modèle"
EXAMPLES_TITLE = "## Exemples de résultats rendus par le code"
V2_TEXTS_TITLE = "## Vague 2 — textes vus par le modèle"
V2_EXAMPLES_TITLE = "## Vague 2 — exemples de résultats rendus par le code"
GENERATED_TITLES = (FAMILIES_TITLE, TEXTS_TITLE, EXAMPLES_TITLE, V2_TEXTS_TITLE, V2_EXAMPLES_TITLE)


def families_section() -> str:
    """Section « Familles et mots déclencheurs », tirée de `FAMILIES` (seules les familles du pack)."""
    from nokker.tools.essentials.texts import NAMES
    from nokker.tools.essentials.texts_v2 import NAMES_V2
    from nokker.tools.select import FAMILIES

    lines = [f"- **{f.label}** (`{'`, `'.join(f.load)}`) : `{f.match.pattern}`"
             for f in FAMILIES if set(f.load) <= set(NAMES) | set(NAMES_V2)]
    return (FAMILIES_TITLE + "\n\nPolitique `relevance` : un message qui contient un de ces motifs (expressions "
            "régulières, sans casse) charge les schémas\nde la famille ; sinon `tool_find` ou un appel direct par le "
            "nom. Index compact : une ligne par famille.\n\n" + "\n".join(lines) + "\n")


def texts_section() -> str:
    from nokker.tools.essentials.common import LOCAL_SOURCE, UNTRUSTED_NOTICE
    from nokker.tools.essentials.texts import TOOLS, texts_sha256

    out = (TEXTS_TITLE + """

À valider par la relecture des textes avant fusion (Nokker dev). Source unique : `nokker/tools/essentials/texts.py` ; ce
document en est la copie exacte (un test compare description et schéma, octet pour octet).

Empreinte des textes (sha256 de `json.dumps(TOOLS, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`,
`texts_sha256()`) : `""" + texts_sha256() + """`

Noms : `<domaine>_<verbe ou objet>` en snake_case (`unit_convert`, `math_calc`, `date_calc`, `transit_trip`…) ;
`weather_ch`, `wiki_summary`, `fx_rate`, `pollen`, `holidays` gardent les noms du cahier des charges. Effet : `read`
pour les onze.

Préfixes fixes des résultats :

- résultat d'une source externe : `""" + UNTRUSTED_NOTICE + """` puis le bloc
  `<untrusted-data kind="essentials" id="…">…</untrusted-data id="…">` (`nokker.qa.untrusted.fence`), puis la ligne
  `Source : …` écrite par le noyau ;
- résultat d'un calcul local : le texte, puis `""" + LOCAL_SOURCE + """` ;
- erreur : `code : texte`.

""")
    for n, (d, pr) in TOOLS.items():
        out += f"### `{n}`\n\n> {d}\n\n```json\n{json.dumps(pr, ensure_ascii=False, indent=1)}\n```\n\n"
    return out


def examples_section() -> str:
    return (EXAMPLES_TITLE + """

Rendus par `tests/unit/tools/essentials_fixtures.py::render_examples` (données simulées au format réel des API, run et
horloge fixés) ; un test vérifie que cette section est identique à la sortie du code. Régénérer :
`python -m tests.unit.tools.essentials_fixtures`.

""" + doc_section())


def v2_texts_section() -> str:
    from nokker.tools.essentials.texts_v2 import TOOLS_V2, TRANSIT_FALLBACK_SUFFIX, texts_v2_sha256

    out = (V2_TEXTS_TITLE + """

Source unique : `nokker/tools/essentials/texts_v2.py` (copie exacte, un test compare). Les textes de la vague 1 ne
changent pas, sauf : `transit_departures` et `transit_trip` reçoivent le suffixe ci-dessous quand le repli SNCF est
actif (jeton présent).

Empreinte des textes de la vague 2 (sha256 de `json.dumps({"tools": TOOLS_V2, "transit_suffix": …},
ensure_ascii=False, sort_keys=True, separators=(",", ":"))`, `texts_v2_sha256()`) : `""" + texts_v2_sha256() + """`

Suffixe des descriptions de transit (repli SNCF actif) :

> """ + TRANSIT_FALLBACK_SUFFIX.strip() + """

""")
    for n, (d, pr) in TOOLS_V2.items():
        out += f"### `{n}`\n\n> {d}\n\n```json\n{json.dumps(pr, ensure_ascii=False, indent=1)}\n```\n\n"
    return out


def v2_examples_section() -> str:
    from tests.unit.tools.essentials_v2_fixtures import doc_section as v2_doc_section

    return (V2_EXAMPLES_TITLE + """

Rendus par `tests/unit/tools/essentials_v2_fixtures.py::render_examples` (données simulées au format réel des sources,
horloges fixées ; Navitia d'après sa documentation). Un test vérifie que cette section est identique à la sortie du
code.

""" + v2_doc_section())


def regenerate_doc(text: str) -> str:
    """Le doc avec ses sections générées remplacées (jamais ajoutées) : la partie rédigée s'arrête au PREMIER titre
    généré trouvé, tout le reste est réécrit."""
    cut = min((i for i in (text.find(t) for t in GENERATED_TITLES) if i >= 0), default=len(text))
    head = text[:cut].rstrip("\n") + "\n\n"
    return (head + families_section() + "\n" + texts_section() + examples_section() + "\n" + v2_texts_section()
            + v2_examples_section()).rstrip("\n") + "\n"


if __name__ == "__main__":  # pragma: no cover
    from pathlib import Path

    doc = Path(__file__).resolve().parents[3] / "docs" / "ESSENTIALS.md"
    doc.write_text(regenerate_doc(doc.read_text(encoding="utf-8")), encoding="utf-8")
