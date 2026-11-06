"""Vague 2 du pack Essentiels : données simulées au format réel des sources (relevé le 05.11.2026 ; Navitia d'après
doc.navitia.io, faute de jeton) et exemples rendus par le code pour docs/ESSENTIALS.md (« Vague 2 — exemples »)."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from nokker.tools.essentials import V2, EssentialsSource, air, charging, geo_fr, hydro, mobility, school, vigilance
from nokker.tools.essentials.navitia import BASE as NAVITIA
from nokker.tools.essentials.navitia import NavitiaFrance
from tests.unit.tools import essentials_fixtures as v1
from tests.unit.tools.essentials_fixtures import CTX, FakeNet, src

NOW = datetime(2026, 11, 5, 19, 0, tzinfo=timezone.utc)
CLOCK = "2026-11-05T20:00+01:00"
GEO = geo_fr.BASE

GEX = {"nom": "Gex", "code": "01173", "codesPostaux": ["01170"], "codeDepartement": "01",
       "centre": {"type": "Point", "coordinates": [6.0488, 46.3515]}, "population": 13627,
       "departement": {"code": "01", "nom": "Ain"}}
ANNEMASSE = {"nom": "Annemasse", "code": "74012", "codesPostaux": ["74100"], "codeDepartement": "74",
             "centre": {"type": "Point", "coordinates": [6.2453, 46.1909]}, "population": 37951,
             "departement": {"code": "74", "nom": "Haute-Savoie"}}
SION_FR = {"nom": "Sion", "code": "32432", "codesPostaux": ["32190"], "codeDepartement": "32",
           "centre": {"type": "Point", "coordinates": [0.3017, 43.7253]}, "population": 92,
           "departement": {"code": "32", "nom": "Gers"}}


def _geo(routes: dict) -> None:
    for name, rows in (("Gex", [GEX]), ("Annemasse", [ANNEMASSE]), ("Sion", [SION_FR]), ("Paris", []),
                       ("Lausanne", []), ("Thonon-les-Bains", [])):
        routes[f"{GEO}?nom={name}&"] = (200, json.dumps(rows))
    routes[f"{GEO}?codePostal=74100&"] = (200, json.dumps([ANNEMASSE, dict(ANNEMASSE, nom="Ambilly", code="74008")]))


DAY = f"{vigilance.BASE}/2026/11/05/"
CARD = {"product": {"update_time": "2026-11-05T17:34:52Z", "periods": [
    {"echeance": "J", "begin_validity_time": "2026-11-05T17:30:00Z", "end_validity_time": "2026-11-05T23:00:00Z",
     "timelaps": {"domain_ids": [
         {"domain_id": "01", "max_color_id": 2, "phenomenon_items": [
             {"phenomenon_id": "3", "phenomenon_max_color_id": 2, "timelaps_items": [
                 {"begin_time": "2026-11-05T17:30:00Z", "end_time": "2026-11-05T21:00:00Z", "color_id": 2}]},
             {"phenomenon_id": "1", "phenomenon_max_color_id": 1, "timelaps_items": []}]},
         {"domain_id": "74", "max_color_id": 1, "phenomenon_items": []}]}},
    {"echeance": "J1", "begin_validity_time": "2026-11-05T23:00:00Z", "end_validity_time": "2026-11-06T23:00:00Z",
     "timelaps": {"domain_ids": [{"domain_id": "01", "max_color_id": 1, "phenomenon_items": []}]}}]}}
INDEX = ('<a href="../">..</a>\n<a href="/meteofrance/data/vigilance/metropole/2026/11/05/150006/">150006/</a>\n'
         '<a href="/meteofrance/data/vigilance/metropole/2026/11/05/173449/">173449/</a>\n'
         '<a href="/meteofrance/data/vigilance/metropole/2026/11/05/174930/">174930/</a>\n')

IDENTIFY = {"results": [
    {"geometry": {"type": "Point", "coordinates": [6.14279317855835, 46.20571517944336]},
     "properties": {"provider": {"id": "donkey_ge", "name": "Donkey Republic Geneva"}, "id": "donkey_ge:18422",
                    "available": True, "pickup_type": "station_based",
                    "station": {"name": "Place St-Gervais", "status": {"installed": True, "renting": True,
                                                                       "returning": True, "num_vehicle_available": 11}},
                    "vehicle_type": ["Bike", "E-Bike", "E-Bike"]}},
    {"geometry": {"type": "Point", "coordinates": [6.144911766052246, 46.20243453979492]},
     "properties": {"provider": {"id": "donkey_ge", "name": "Donkey Republic Geneva"}, "id": "donkey_ge:1",
                    "pickup_type": "free_floating", "vehicle_type": ["E-Bike"],
                    "vehicle": {"status": {"disabled": False, "reserved": False}}}},
    {"geometry": {"type": "Point", "coordinates": [6.1451, 46.2026]},
     "properties": {"provider": {"id": "donkey_ge", "name": "Donkey Republic Geneva"}, "id": "donkey_ge:2",
                    "pickup_type": "free_floating", "vehicle_type": ["E-Bike"],
                    "vehicle": {"status": {"disabled": False, "reserved": False}}}},
    {"geometry": {"type": "Point", "coordinates": [6.1440, 46.2100]},
     "properties": {"provider": {"id": "mobility", "name": "Mobility"}, "id": "mobility:1", "available": True,
                    "pickup_type": "station_based",
                    "station": {"name": "Genève Gare Cornavin", "status": {"renting": True,
                                                                           "num_vehicle_available": 1}},
                    "vehicle_type": ["Car", "E-Car"]}},
    {"geometry": {"type": "Point", "coordinates": [6.2000, 46.2400]},  # dans la boîte, hors du cercle
     "properties": {"provider": {"name": "Loin"}, "pickup_type": "free_floating", "vehicle_type": ["E-Scooter"]}}]}

EVSE_DATA = {"EVSEData": [{"OperatorID": "CH*CCI", "OperatorName": "Move", "EVSEDataRecord": [
    {"EvseID": "CH*CCI*E1", "Address": {"City": "Genève", "PostalCode": "1201", "Street": "Quai Turrettini 1"},
     "GeoCoordinates": {"Google": "46.205696 6.140263"}, "ChargingFacilities": [{"power": "22.0"}],
     "Plugs": ["Type 2 Outlet"]},
    {"EvseID": "CH*CCI*E2", "Address": {"City": "Genève", "PostalCode": "1201", "Street": "Quai Turrettini 1"},
     "GeoCoordinates": {"Google": "46.205696 6.140263"}, "ChargingFacilities": [{"power": "22.0"}],
     "Plugs": ["Type 2 Outlet"]},
    {"EvseID": "CH*CCI*E3", "Address": {"City": "Genève", "PostalCode": "1201", "Street": "Rue de Lausanne 5"},
     "GeoCoordinates": {"Google": "46.2110 6.1450"}, "ChargingFacilities": [{"power": "150"}],
     "Plugs": ["CCS Combo 2 Plug (Cable Attached)"], "Tarif": "0.66 CHF/kWh"}]}]}
EVSE_STATUS = {"EVSEStatuses": [{"OperatorID": "CH*CCI", "EVSEStatusRecord": [
    {"EvseID": "CH*CCI*E1", "EVSEStatus": "Available"}, {"EvseID": "CH*CCI*E2", "EVSEStatus": "Occupied"}]}]}

HYDRO_CSV = (
    "id,name,waterbody,wbType,wkt,time,waterLevel,discharge,waterTemperature,dangerLevel\n"
    "2009,Porte du Scex,Rhône,https://environment.ld.admin.ch/foen/hydro/waterbody/type/river,"
    "POINT(6.888607996854026 46.34956713202868),2026-11-05T19:40:00+01:00,375.258,152.513,7.09,1\n"
    "2028,\"Genève, Sécheron\",Lac Léman,https://environment.ld.admin.ch/foen/hydro/waterbody/type/lake,"
    "POINT(6.15235732549977 46.218636691415625),2026-11-05T19:40:00+01:00,372.169,,,https://cube.link/Undefined\n"
    "2170,\"Genève, Bout du Monde\",Arve,https://environment.ld.admin.ch/foen/hydro/waterbody/type/river,"
    "POINT(6.159349423625321 46.18029993696814),2026-11-05T19:40:00+01:00,378.881,21.991,8.09,1\n"
    "2606,\"Genève, Halle de l'Île\",Rhône,https://environment.ld.admin.ch/foen/hydro/waterbody/type/river,"
    "POINT(6.1428 46.2043),2026-11-05T19:40:00+01:00,,281.374,12.69,1\n"
    "2269,Blatten,Lonza,https://environment.ld.admin.ch/foen/hydro/waterbody/type/river,"
    "POINT(7.8197 46.4206),2025-05-28T10:00:00+02:00,0.125,1.2,,1\n")

ATMO = {"type": "FeatureCollection", "features": [{"properties": {
    "date_maj": "2026-11-05T15:23:00.384Z", "code_no2": 1, "code_o3": 1, "code_pm10": 2, "code_pm25": 2,
    "code_qual": 2, "code_so2": 1, "code_zone": "74012", "date_ech": d, "lib_qual": "Moyen",
    "lib_zone": "Annemasse", "source": "Atmo Auvergne-Rhône-Alpes", "type_zone": "commune"}}
    for d in ("2026-11-06", "2026-11-05", "2026-11-07")]}

CALENDAR = {"total_count": 4, "results": [
    {"description": "Vacances de Noël", "population": "-", "start_date": "2026-12-18T23:00:00+00:00",
     "end_date": "2027-01-03T23:00:00+00:00", "location": "Lyon", "zones": "Zone A", "annee_scolaire": "2026-2027"},
    {"description": "Pont de l'Ascension", "population": "-", "start_date": "2027-05-06T22:00:00+00:00",
     "end_date": "2027-05-06T22:00:00+00:00", "location": "Lyon", "zones": "Zone A", "annee_scolaire": "2026-2027"},
    {"description": "Vacances d'Été", "population": "Élèves", "start_date": "2027-07-02T22:00:00+00:00",
     "end_date": "2027-09-01T22:00:00+00:00", "location": "Lyon", "zones": "Zone A", "annee_scolaire": "2026-2027"},
    {"description": "Vacances d'Été", "population": "Enseignants", "start_date": "2027-07-02T22:00:00+00:00",
     "end_date": "2027-08-31T22:00:00+00:00", "location": "Lyon", "zones": "Zone A", "annee_scolaire": "2026-2027"}]}
BS = {"results": [{"year": "2026", "name": "Weihnachtsferien", "start_date": "2026-12-19", "end_date": "2027-01-03"},
                  {"year": "2027", "name": "1. Mai", "start_date": "2027-05-01", "end_date": "2027-05-01"}]}

NAV_PLACES = {"places": [{"id": "stop_area:SNCF:87745000", "name": "Thonon-les-Bains (Thonon-les-Bains)",
                          "embedded_type": "stop_area"}]}
NAV_DEPARTURES = {"departures": [
    {"display_informations": {"commercial_mode": "Léman Express", "code": "L1", "direction": "Coppet (Coppet)"},
     "stop_date_time": {"departure_date_time": "20261105T201200", "base_departure_date_time": "20261105T200900"}},
    {"display_informations": {"commercial_mode": "TER", "code": "", "direction": "Évian-les-Bains (Évian-les-Bains)"},
     "stop_date_time": {"departure_date_time": "20261105T202500", "base_departure_date_time": "20261105T202500"}}]}


def v2_net() -> FakeNet:
    routes: dict = {}
    _geo(routes)
    routes[DAY] = (200, INDEX)
    routes[DAY + "173449/CDP_CARTE_EXTERNE.json"] = (200, json.dumps(CARD))
    routes[DAY + "174930/"] = (404, "")
    routes[mobility.URL + "?"] = (200, json.dumps(IDENTIFY))
    routes[charging.DATA_URL] = (200, json.dumps(EVSE_DATA))
    routes[charging.STATUS_URL] = (200, json.dumps(EVSE_STATUS))
    routes[hydro.URL + "?query="] = (200, HYDRO_CSV.encode("utf-8"))
    routes[air.URL + "?"] = (200, json.dumps(ATMO))
    routes[school.FR_URL + "?"] = (200, json.dumps(CALENDAR))
    routes[school.CH_SOURCES["BS"][0] + "?"] = (200, json.dumps(BS))
    routes[f"{NAVITIA}/places?q=Thonon"] = (200, json.dumps(NAV_PLACES))
    routes[f"{NAVITIA}/stop_areas/"] = (200, json.dumps(NAV_DEPARTURES))
    return FakeNet(routes)


def v2_source(net: FakeNet | None = None, charging_permitted: bool = True, navitia: bool = True) -> EssentialsSource:
    """Pack complet (vagues 1 et 2) sur transport simulé : aucune requête réseau."""
    net = net or v2_net()
    base = v1.full_source()
    tnet = base.transit.source.fetcher
    tnet.routes["https://transport.opendata.ch/v1/locations?query=Thonon-les-Bains"] = (200, json.dumps({"stations": []}))
    places = geo_fr.FrenchPlaces(source=src(net))
    v2 = V2(places=places,
            vigilance=vigilance.Vigilance(source=src(net), clock_utc=lambda: NOW),
            mobility=mobility.Mobility(source=src(net), clock=lambda: CLOCK),
            charging=charging.Charging(source=src(net), clock=lambda: CLOCK) if charging_permitted else None,
            water=hydro.Water(source=src(net), clock_utc=lambda: NOW),
            air=air.Air(source=src(net)),
            school=school.SchoolHolidays(places=places, source=src(net), ch_source=src(net),
                                         today=lambda: date(2026, 11, 5)))
    s = EssentialsSource(meteo=base.meteo, transit=base.transit, fx=base.fx, wiki=base.wiki,
                         avalanche=base.avalanche, v2=v2)
    s.meteo.france_tool = True
    if navitia:
        s.transit.fallback = NavitiaFrance(token="jeton-de-test", source=src(net))
    return s


EXAMPLE_CALLS: list[tuple[str, dict]] = [
    ("weather_alerts_fr", {"place": "Gex"}),
    ("mobility_nearby", {"place": "Genève", "kind": "tout", "radius_m": 500}),
    ("charging_nearby", {"place": "Genève", "min_power_kw": 11, "radius_m": 1000}),
    ("water_status", {"place": "Arve"}),
    ("air_quality_fr", {"place": "Annemasse"}),
    ("school_holidays", {"region": "Gex", "year": "2026-2027"}),
    ("transit_departures", {"station": "Thonon-les-Bains", "limit": 2}),
]
ERROR_EXAMPLES: list[tuple[str, dict]] = [
    ("weather_alerts_fr", {"place": "Sion"}),
    ("weather_alerts_fr", {"place": "Lausanne"}),
    ("weather_ch", {"place": "Paris"}),
    ("water_status", {"place": "Rhône"}),
    ("air_quality_fr", {"place": "Lausanne"}),
    ("school_holidays", {"region": "GE"}),
]


def render_examples() -> list[tuple[str, dict, str]]:
    s = v2_source()
    return [(n, a, s.call(n, a, CTX).content) for n, a in EXAMPLE_CALLS + ERROR_EXAMPLES]


def doc_section() -> str:
    parts = []
    for name, args, content in render_examples():
        parts.append(f"#### `{name}({json.dumps(args, ensure_ascii=False)})`\n\n```text\n{content}\n```\n")
    return "\n".join(parts)
