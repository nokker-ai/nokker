"""Pack Essentiels, vague 2 (docs/ESSENTIALS.md §Vague 2) — transport simulé : AUCUNE requête réseau."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from nokker.tools.essentials import build_essentials, common, transit
from nokker.tools.essentials.texts import NAMES
from nokker.tools.essentials.texts_v2 import NAMES_V2, TOOLS_V2, TRANSIT_FALLBACK_SUFFIX, texts_v2_sha256
from tests.unit.tools import essentials_fixtures as fx1
from tests.unit.tools import essentials_v2_fixtures as fx2
from tests.unit.tools.essentials_fixtures import CTX

ROOT = Path(__file__).resolve().parents[3]
ISO = re.compile(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2}))?")
FENCE = re.compile(r'<untrusted-data kind="essentials" id="([0-9a-f]{16})">')


def _fenced(content: str) -> bool:
    return content.startswith(common.UNTRUSTED_NOTICE) and bool(FENCE.search(content))


# ─────────────────────────────── réglage et câblage ───────────────────────────────


def test_settings_off_is_wave1_identical():
    base = [(s.name, s.description, s.parameters) for s in build_essentials({}).specs()]
    assert [(s.name, s.description, s.parameters) for s in build_essentials({"enabled": False}).specs()] == base
    assert [s[0] for s in base] == list(NAMES)
    off = build_essentials(None)
    res = off.call("weather_alerts_fr", {"place": "Gex"}, CTX)
    assert res.is_error and res.content.startswith("introuvable : outil inconnu")


def test_settings_on_adds_wave2_and_gates_charging_and_fallback():
    on = build_essentials({"enabled": True})
    names = [s.name for s in on.specs()]
    assert names == list(NAMES) + [n for n in NAMES_V2 if n != "charging_nearby"]
    assert on.transit.fallback is None  # pas de jeton : pas de repli, description de transit inchangée
    assert next(s for s in on.specs() if s.name == "transit_trip").description.endswith("préférer à web_search.")
    full = build_essentials({"enabled": True, "charging_permitted": True, "navitia_token": "secret:NAVITIA_TOKEN"},
                            lambda ref: "jeton" if ref == "secret:NAVITIA_TOKEN" else "")
    assert "charging_nearby" in [s.name for s in full.specs()] and full.transit.fallback is not None
    trip = next(s for s in full.specs() if s.name == "transit_trip")
    from nokker.tools.essentials.texts import TOOLS

    assert trip.description == TOOLS["transit_trip"][0] + TRANSIT_FALLBACK_SUFFIX
    assert trip.parameters == TOOLS["transit_trip"][1]  # schéma inchangé
    assert full.transit.fallback.source.headers == {"Authorization": "jeton"}
    # « charging_permitted » doit être le booléen vrai, pas une chaîne.
    assert "charging_nearby" not in [s.name for s in build_essentials({"enabled": True,
                                                                      "charging_permitted": "yes"}).specs()]
    # Jeton illisible : pas de repli, jamais d'exception.
    broken = build_essentials({"enabled": True, "navitia_token": "secret:X"}, lambda ref: 1 / 0)
    assert broken.transit.fallback is None


def test_contact_email_is_sent_as_from_header_only_when_set():
    with_mail = build_essentials({"enabled": True, "contact_email": "beta@nokker.ai"})
    assert with_mail.v2.mobility.source.headers == {"From": "beta@nokker.ai"}
    assert build_essentials({"enabled": True}).v2.mobility.source.headers == {}


def test_texts_rules():
    firsts = {  # premières phrases imposées en relecture (05.11.2026) : le pays couvert y est dit
        "weather_alerts_fr": "Vigilance météo officielle de Météo-France par département (vert, jaune, orange, rouge, "
                             "par phénomène, aujourd'hui et demain) ; pas de prévision chiffrée.",
        "air_quality_fr": "Qualité de l'air en France (indice ATMO de 1 à 6, Atmo AURA et réseau Atmo) ; pas de "
                          "données ouvertes pour la Suisse.",
        "school_holidays": "Vacances scolaires : France (zones et académies) ; en Suisse, seulement BS et BL pour "
                           "l'instant.",
        "water_status": "Suisse : rivières et lacs : niveau, débit, température de l'eau (stations OFEV) ; pas l'eau "
                        "du robinet.",
    }
    for name, (desc, params) in TOOLS_V2.items():
        assert desc.startswith(firsts.get(name, "Suisse :")), name
        assert "web_search" in desc, name
        for p in params["properties"].values():
            assert "enum" not in p or p == TOOLS_V2["mobility_nearby"][1]["properties"]["kind"], name
    assert TOOLS_V2["mobility_nearby"][1]["properties"]["kind"]["enum"] == ["velo", "trottinette", "voiture", "tout"]
    assert TOOLS_V2["water_status"][0].startswith(
        "Suisse : rivières et lacs : niveau, débit, température de l'eau (stations OFEV) ; pas l'eau du robinet.")
    for n in ("mobility_nearby", "charging_nearby"):
        assert "défaut 500, maximum 2000" in TOOLS_V2[n][1]["properties"]["radius_m"]["description"]
    assert "prix" in TOOLS_V2["charging_nearby"][0].lower()


# ─────────────────────────────── outils ───────────────────────────────


def test_every_wave2_example_is_fenced_with_source_iso_time_and_units():
    units = {"mobility_nearby": " m,", "charging_nearby": " kW", "water_status": " m³/s", "air_quality_fr": "µg/m³",
             "transit_departures": " min)"}
    for name, args, content in fx2.render_examples()[: len(fx2.EXAMPLE_CALLS)]:
        assert _fenced(content), name
        assert content.splitlines()[-1].startswith("Source : "), name
        assert ISO.search(content), name
        if name in units:
            assert units[name] in content, name


def test_weather_alerts_fr_vigilance_and_coverage_messages():
    s = fx2.v2_source()
    res = s.call("weather_alerts_fr", {"place": "Gex"}, CTX)
    assert "Gex (Ain, département 01), carte du 2026-11-05T17:34:52Z" in res.content
    assert "niveau maximal jaune ; orages jaune (2026-11-05T17:30:00Z → 2026-11-05T21:00:00Z)" in res.content
    out = s.call("weather_alerts_fr", {"place": "Lausanne"}, CTX).content
    assert out.startswith("hors_couverture : ") and "pour la Suisse, weather_ch ; ailleurs, web_search." in out
    ch = s.call("weather_ch", {"place": "Paris"}, CTX).content
    assert ch.endswith("Cet outil ne couvre que la Suisse ; pour une prévision à l'étranger, web_search ; pour les "
                       "alertes en France, weather_alerts_fr.")
    # Vague 1 seule : le texte d'avant, sans renvoi vers un outil absent.
    v1 = fx1.full_source().call("weather_ch", {"place": "Paris"}, CTX).content
    assert v1.endswith("Cet outil ne couvre que la Suisse ; pour l'étranger, web_search.")


def test_cross_border_name_is_ambiguous_never_picked():
    s = fx2.v2_source()
    for tool in ("weather_alerts_fr", "air_quality_fr"):
        res = s.call(tool, {"place": "Sion"}, CTX)
        assert res.content.startswith("ambigu : ") and "Sion (Gers, 32), France ; Sion (NPA 1950), Suisse" in res.content


def test_french_postal_code_shared_by_communes_is_ambiguous():
    res = fx2.v2_source().call("weather_alerts_fr", {"place": "74100"}, CTX)
    assert res.content.startswith("ambigu : ") and "Annemasse (74100) ; Ambilly (74100)" in res.content


def test_mobility_circle_filter_kind_enum_and_radius_cap():
    net = fx2.v2_net()
    s = fx2.v2_source(net)
    res = s.call("mobility_nearby", {"place": "Genève", "radius_m": 99999}, CTX)
    assert "dans un rayon de 2000 m" in res.content and "Loin" not in res.content  # coin de la boîte écarté
    assert "station « Genève Gare Cornavin » (Mobility) : voiture, voiture électrique ; 1 véhicule disponible" in res.content
    assert "2 véhicules en libre-service (vélo électrique, Donkey Republic Geneva)" in res.content
    cars = s.call("mobility_nearby", {"place": "Genève", "kind": "voiture"}, CTX).content
    assert "Mobility" in cars and "Donkey" not in cars
    assert s.call("mobility_nearby", {"place": "Genève", "kind": "avion"}, CTX).content.startswith("argument_invalide")
    first = next(u for u in net.calls if "identify" in u)
    assert "Tolerance=2000" in first and "99999" not in first  # rayon plafonné avant l'appel


def test_charging_groups_points_status_time_and_never_a_price():
    res = fx2.v2_source().call("charging_nearby", {"place": "Genève", "radius_m": 1000}, CTX)
    assert "statut lu le 2026-11-05T20:00+01:00" in res.content
    assert "Quai Turrettini 1, 1201 Genève (Move) : 2 points de charge, 22 kW, Type 2 Outlet ; 1 libre, 1 occupée" \
        in res.content
    assert "disponibilité inconnue : 1" in res.content
    assert "CHF" not in res.content and "0.66" not in res.content  # tarif présent dans les données, jamais rendu


def test_water_status_by_river_station_lake_and_locality():
    s = fx2.v2_source()
    arve = s.call("water_status", {"place": "Arve"}, CTX).content
    assert ("Arve à Genève, Bout du Monde (rivière, station 2170), mesure du 2026-11-05T19:40:00+01:00 : niveau "
            "378.881 m d'altitude ; débit 21.991 m³/s ; température de l'eau 8.09 °C") in arve
    rhone = s.call("water_status", {"place": "Rhône"}, CTX).content
    assert rhone.startswith("ambigu : ") and "Rhône à Porte du Scex ; Rhône à Genève, Halle de l'Île" in rhone
    lake = s.call("water_status", {"place": "Lac Léman"}, CTX).content
    assert "Lac Léman à Genève, Sécheron (lac, station 2028)" in lake and "débit" not in lake
    near = s.call("water_status", {"place": "Genève"}, CTX).content
    assert near.startswith("ambigu : ")  # trois stations « Genève, … » : la liste, pas un choix
    old = s.call("water_status", {"place": "Blatten"}, CTX).content
    assert "MESURE ANCIENNE" in old and "hauteur à l'échelle 0.125 m" in old


def test_air_quality_index_name_scale_and_no_concentration_unit():
    res = fx2.v2_source().call("air_quality_fr", {"place": "Annemasse"}, CTX).content
    assert "échelle de 1 « bon » à 6 « extrêmement mauvais »" in res and "pas des µg/m³" in res
    assert "- 2026-11-05 : indice ATMO 2 « moyen » ; sous-indices NO2 1, O3 1, PM10 2, PM2.5 2, SO2 1" in res
    lines = [ln for ln in res.splitlines() if ln.startswith("- 2026")]
    assert [ln[2:12] for ln in lines] == ["2026-11-05", "2026-11-06", "2026-11-07"]  # triées
    assert "Atmo France et les Associations agréées" in res.splitlines()[-1]


def test_school_holidays_france_switzerland_and_out_of_scope():
    s = fx2.v2_source()
    gex = s.call("school_holidays", {"region": "Gex", "year": "2026-2027"}, CTX).content
    assert "Gex (Ain, 01), académie de Lyon (Zone A)" in gex
    assert "du samedi 2026-12-19 au dimanche 2027-01-03 (reprise le lundi 2027-01-04)" in gex
    assert "Pont de l'Ascension : le vendredi 2027-05-07" in gex
    assert gex.count("Vacances d'Été") == 1  # la ligne « Enseignants » est écartée
    bs = s.call("school_holidays", {"region": "BS"}, CTX).content
    assert "canton BS" in bs and "1. Mai : le samedi 2027-05-01" in bs and "CC BY 4.0" in bs
    ge = s.call("school_holidays", {"region": "GE"}, CTX).content
    assert ge.startswith("hors_couverture : ") and ge.endswith("; pour ce canton, web_search")
    assert s.call("school_holidays", {"region": "Gex", "year": "2026-2028"}, CTX).content.startswith(
        "argument_invalide : ")


def test_transit_fallback_names_the_sources_actually_used():
    s = fx2.v2_source()
    swiss = s.call("transit_departures", {"station": "Lausanne"}, CTX).content
    assert swiss.endswith(transit.SOURCE_LINE)  # l'horaire suisse seul
    fr = s.call("transit_departures", {"station": "Thonon-les-Bains", "limit": 2}, CTX).content
    assert fr.endswith(transit.SOURCE_LINE_FALLBACK) and "SNCF (Navitia" in fr
    assert "Prochains départs de Thonon-les-Bains (2026-11-05, heures locales UTC+01:00)" in fr
    assert "- 20:12 (+3 min) Léman Express L1 → Coppet" in fr
    # Sans jeton : l'arrêt inconnu de l'horaire suisse reste une erreur « introuvable », sans repli.
    no = fx2.v2_source(navitia=False).call("transit_departures", {"station": "Thonon-les-Bains"}, CTX).content
    assert no.startswith("introuvable : ")


def test_navitia_token_never_in_url_or_text():
    net = fx2.v2_net()
    s = fx2.v2_source(net)
    s.transit.fallback.source.headers = {"Authorization": "jeton-secret-123"}
    out = s.call("transit_departures", {"station": "Thonon-les-Bains"}, CTX).content
    assert "jeton-secret-123" not in out and all("jeton-secret-123" not in u for u in net.calls)
    assert net.headers == {"Authorization": "jeton-secret-123"}


# ─────────────────────────────── doc et textes ───────────────────────────────


def _doc() -> str:
    return (ROOT / "docs" / "ESSENTIALS.md").read_text(encoding="utf-8")


def test_doc_carries_wave2_texts_hash_and_examples():
    doc = _doc()
    section = doc.split(fx1.V2_TEXTS_TITLE, 1)[1]
    for name, (desc, params) in TOOLS_V2.items():
        assert f"### `{name}`" in section and f"> {desc}" in section, name
        assert json.dumps(params, ensure_ascii=False, indent=1) in section, name
    blob = json.dumps({"tools": TOOLS_V2, "transit_suffix": TRANSIT_FALLBACK_SUFFIX}, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))
    assert texts_v2_sha256() == hashlib.sha256(blob.encode()).hexdigest() and f"`{texts_v2_sha256()}`" in section
    assert fx2.doc_section() in doc
    assert doc.count(fx1.V2_TEXTS_TITLE) == 1 and doc.count(fx1.V2_EXAMPLES_TITLE) == 1


def test_school_holidays_triggers_never_on_vacances_alone():
    from nokker.tools.select import relevant_tools

    specs = build_essentials({"enabled": True}).specs()

    def loads(text: str) -> bool:
        return "school_holidays" in {s.name for s in relevant_tools(text, set(), specs)}

    for yes in ("vacances scolaires à Gex", "congés scolaires zone A", "Schulferien Basel", "les relâches"):
        assert loads(yes), yes
    for no in ("vacances", "on part en vacances", "mes vacances à Gex"):
        assert not loads(no), no


@pytest.mark.parametrize("fixture_name", ["fx2"])
def test_no_network_in_fixtures(fixture_name, monkeypatch):
    import nokker.previews.fetch as pf

    def refuse(*a, **k):
        raise AssertionError("appel réseau dans un test")

    monkeypatch.setattr(pf, "guarded_get", refuse)
    fx2.render_examples()


def _kernel(monkeypatch, settings):
    from nokker.api.events import EventBus
    from nokker.api.server import build_hub, build_kernel
    from nokker.config import load_config
    from nokker.tools.select import compact_index

    for var in ("NOKKER_PG_DSN", "API_SERVER_KEY", "NOKKER_PG_OWNER_DSN", "NOKKER_BROWSER_POOL_URL"):
        monkeypatch.delenv(var, raising=False)
    config = load_config(str(ROOT / "deploy/beta/core-config.yaml"))
    config.prefix_path = str(ROOT / "bench/conv/fixtures/prefix.md")
    config.previews_enabled = False
    config.admin_enabled = False
    config.essentials = True
    config.essentials_v2 = settings
    kernel = build_kernel(config, hub=build_hub(config, EventBus()))
    tester = [s for src in kernel.tools.tester_sources for s in src.specs()]
    dump = [(s.name, s.description, json.dumps(s.parameters, sort_keys=True, ensure_ascii=False)) for s in tester]
    return dump, compact_index([s.name for s in tester if not s.core])


def test_kernel_wave2_off_is_byte_identical(monkeypatch):
    monkeypatch.delenv("NAVITIA_TOKEN", raising=False)
    beta_off, idx_beta = _kernel(monkeypatch, {"enabled": False, "contact_email": "beta@nokker.ai",
                                               "charging_permitted": False, "navitia_token": "secret:NAVITIA_TOKEN"})
    empty, idx_empty = _kernel(monkeypatch, {})
    assert beta_off == empty and idx_beta.encode() == idx_empty.encode()
    on, idx_on = _kernel(monkeypatch, {"enabled": True})
    assert [x for x in on if x[0] not in NAMES_V2] == empty  # vague 1 inchangée sans jeton SNCF
    assert {x[0] for x in on} - {x[0] for x in empty} == set(NAMES_V2) - {"charging_nearby"}
    monkeypatch.setenv("NAVITIA_TOKEN", "jeton")
    with_token, _ = _kernel(monkeypatch, {"enabled": True, "navitia_token": "secret:NAVITIA_TOKEN"})
    trip = next(x for x in with_token if x[0] == "transit_trip")
    assert trip[1].endswith(TRANSIT_FALLBACK_SUFFIX)
    assert trip[2] == next(x for x in empty if x[0] == "transit_trip")[2]  # schéma inchangé
