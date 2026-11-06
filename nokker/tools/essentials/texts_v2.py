"""Textes vus par le modèle — vague 2 du pack Essentiels (docs/ESSENTIALS.md §Vague 2), validés en forme en relecture le
05.11.2026. Copie exacte dans le doc (un test compare) ; empreinte `texts_v2_sha256()`.

Règles : première phrase = pays couvert ; « préférer à web_search » ; un argument par notion ; `radius_m` défaut 500,
maximum 2000 ; `mobility_nearby.kind` est le seul enum (fixe, validé en relecture)."""
from __future__ import annotations

from typing import Any


def _obj(props: dict[str, dict[str, Any]], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


_S = "string"
_I = "integer"
_N = "number"
_RADIUS = {"type": _I, "description": "rayon de recherche en mètres (défaut 500, maximum 2000)"}

TOOLS_V2: dict[str, tuple[str, dict[str, Any]]] = {
    "weather_alerts_fr": (
        "Vigilance météo officielle de Météo-France par département (vert, jaune, orange, rouge, par phénomène, "
        "aujourd'hui et demain) ; pas de prévision chiffrée. France métropolitaine ; phénomènes : vent, "
        "pluie-inondation, orages, crues, neige-verglas, canicule, grand froid, avalanches, vagues-submersion. Pour une "
        "alerte météo en France, préférer à web_search.",
        _obj({"place": {"type": _S, "description": "commune ou code postal français, ex. « Gex » ou « 01170 »"}},
             ["place"])),
    "mobility_nearby": (
        "Suisse : vélos, trottinettes et voitures en partage disponibles autour d'un lieu (sharedmobility.ch, "
        "OFEN), avec distance en mètres et temps à pied. Pour trouver un véhicule partagé en Suisse, préférer à "
        "web_search.",
        _obj({"place": {"type": _S, "description": "localité, NPA ou arrêt suisse, ex. « Lausanne, Flon »"},
              "kind": {"type": _S, "enum": ["velo", "trottinette", "voiture", "tout"],
                       "description": "type de véhicule (défaut tout)"},
              "radius_m": _RADIUS}, ["place"])),
    "charging_nearby": (
        "Suisse : bornes de recharge pour voitures électriques autour d'un lieu (ich-tanke-strom.ch, OFEN) : "
        "puissance, prises, opérateur, disponibilité avec son heure, distance en mètres et temps à pied. Pour une "
        "borne de recharge en Suisse, préférer à web_search. Aucun prix.",
        _obj({"place": {"type": _S, "description": "localité, NPA ou adresse suisse"},
              "min_power_kw": {"type": _N, "description": "puissance minimale en kW (défaut 0)"},
              "radius_m": _RADIUS}, ["place"])),
    "water_status": (
        "Suisse : rivières et lacs : niveau, débit, température de l'eau (stations OFEV) ; pas l'eau du robinet. "
        "Mesures officielles des 10 dernières minutes environ ; pour une rivière ou un lac suisse, préférer à "
        "web_search.",
        _obj({"place": {"type": _S, "description": "rivière, lac, station ou localité suisse, ex. « Aare Bern » ou "
                                                  "« Léman »"}}, ["place"])),
    "air_quality_fr": (
        "Qualité de l'air en France (indice ATMO de 1 à 6, Atmo AURA et réseau Atmo) ; pas de données ouvertes pour "
        "la Suisse. Indice d'une commune (1 « bon » à 6 « extrêmement mauvais ») et sous-indices par polluant, "
        "aujourd'hui et les deux jours suivants ; pour la qualité de l'air d'une commune française, préférer à "
        "web_search.",
        _obj({"place": {"type": _S, "description": "commune ou code postal français, ex. « Gex » ou « 74100 »"}},
             ["place"])),
    "school_holidays": (
        "Vacances scolaires : France (zones et académies) ; en Suisse, seulement BS et BL pour l'instant. Dates "
        "officielles d'une zone, d'une académie ou d'une commune française, ou des cantons BS et BL ; pour des "
        "vacances scolaires, préférer à web_search ; les jours fériés sont dans holidays.",
        _obj({"region": {"type": _S, "description": "zone A, B ou C, académie ou commune française, ou canton "
                                                    "suisse, ex. « Gex », « zone A », « GE »"},
              "year": {"type": _S, "description": "année scolaire, ex. « 2026-2027 » (défaut : année en cours)"}},
             ["region"])),
}

# Descriptions de la vague 1 qui changent quand la vague 2 est allumée (repli SNCF actif, renvoi vers weather_alerts_fr).
TRANSIT_FALLBACK_SUFFIX = (" Arrêts français absents de l'horaire suisse : horaire SNCF (TER, TGV, Léman Express "
                           "côté France).")

NAMES_V2 = tuple(TOOLS_V2)
# Familles de `tools/select.py` (vague 2).
ESS2_METEO = ("weather_alerts_fr", "air_quality_fr")
ESS2_MOBILITY = ("mobility_nearby", "charging_nearby")
ESS2_WATER = ("water_status",)
ESS2_SCHOOL = ("school_holidays",)
assert set(ESS2_METEO + ESS2_MOBILITY + ESS2_WATER + ESS2_SCHOOL) == set(NAMES_V2)


def texts_v2_sha256() -> str:
    import hashlib
    import json

    blob = json.dumps({"tools": TOOLS_V2, "transit_suffix": TRANSIT_FALLBACK_SUFFIX}, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


__all__ = ["ESS2_METEO", "ESS2_MOBILITY", "ESS2_SCHOOL", "ESS2_WATER", "NAMES_V2", "TOOLS_V2", "TRANSIT_FALLBACK_SUFFIX", "texts_v2_sha256"]
