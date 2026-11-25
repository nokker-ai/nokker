"""Textes vus par le modèle — descriptions et schémas des outils Essentiels, recopiés à l'identique dans
docs/ESSENTIALS.md (« Textes vus par le modèle », un test compare). Un seul argument par notion, pas d'enum dynamique."""
from __future__ import annotations

from typing import Any


def _obj(props: dict[str, dict[str, Any]], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


_S = "string"
_I = "integer"
_N = "number"
_LANG = {"type": _S, "description": "fr, de, it ou en : la langue de l'utilisateur (défaut fr)"}

TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    "weather_ch": (
        "Météo officielle MétéoSuisse pour un lieu EN SUISSE (localité, NPA, station ou sommet) : ciel, températures "
        "min et max, précipitations, jusqu'à 9 jours. Pour une prévision suisse, préférer à web_search. Ne couvre pas "
        "l'étranger.",
        _obj({"place": {"type": _S, "description": "localité ou NPA suisse, ex. « Lausanne » ou « 1003 »"},
              "days": {"type": _I, "description": "nombre de jours, 1 à 9 (défaut 5)"}}, ["place"])),
    "pollen": (
        "Pollen mesuré hier par MétéoSuisse à la station suisse la plus proche d'un lieu (aune, bouleau, noisetier, "
        "hêtre, frêne, chêne, graminées), en grains par m³. Pour le pollen en Suisse, préférer à web_search.",
        _obj({"place": {"type": _S, "description": "localité, NPA ou canton suisse, ex. « Sion »"}}, ["place"])),
    "transit_departures": (
        "Horaire officiel suisse en temps réel : prochains départs (train, tram, bus, bateau, remontée) d'un arrêt, "
        "avec retards et voies. Pour un départ en Suisse, préférer à web_search.",
        _obj({"station": {"type": _S, "description": "nom de l'arrêt, ex. « Lausanne, Flon »"},
              "limit": {"type": _I, "description": "nombre de départs, 1 à 15 (défaut 8)"},
              "date": {"type": _S, "description": "facultatif : AAAA-MM-JJ ; vide = aujourd'hui"},
              "time": {"type": _S, "description": "facultatif : hh:mm ; vide = maintenant"}},
             ["station"])),
    "transit_trip": (
        "Horaire officiel suisse en temps réel : trajets en transports publics d'un lieu A à un lieu B, avec "
        "horaires, changements, voies et retards. Pour un trajet en Suisse, préférer à web_search.",
        _obj({"from": {"type": _S, "description": "départ : arrêt, adresse ou localité suisse"},
              "to": {"type": _S, "description": "arrivée : arrêt, adresse ou localité suisse"},
              "date": {"type": _S, "description": "facultatif : AAAA-MM-JJ ; vide = aujourd'hui"},
              "time": {"type": _S, "description": "facultatif : hh:mm ; vide = maintenant"},
              "arrival": {"type": "boolean", "description": "true : l'heure donnée est l'heure d'arrivée souhaitée"},
              "limit": {"type": _I, "description": "nombre de trajets, 1 à 6 (défaut 4)"}}, ["from", "to"])),
    "fx_rate": (
        "Taux de change de référence officiel du jour (Banque centrale européenne) entre deux devises, et conversion "
        "d'un montant. Indicatif, pas un taux de transaction. Pour un taux de change, préférer à web_search.",
        _obj({"from": {"type": _S, "description": "devise de départ, code ISO à 3 lettres, ex. CHF"},
              "to": {"type": _S, "description": "devise d'arrivée, code ISO à 3 lettres, ex. EUR"},
              "amount": {"type": _N, "description": "montant à convertir (défaut 1)"}}, ["from", "to"])),
    "holidays": (
        "Jours fériés officiels en Suisse pour une année, par canton (calcul local d'après la liste du SECO). Pour un "
        "jour férié suisse, préférer à web_search. Ne couvre pas les autres pays.",
        _obj({"year": {"type": _I, "description": "année (défaut : année en cours)"},
              "canton": {"type": _S, "description": "code ou nom du canton, ex. VD ; vide = toute la Suisse"}}, [])),
    "wiki_summary": (
        "Résumé d'un article Wikipédia (son introduction), dans la langue de l'utilisateur. Pour définir ou "
        "présenter un sujet, une personne ou un lieu connus, préférer à web_search ; pour l'actualité, web_search.",
        _obj({"title": {"type": _S, "description": "titre de l'article, ex. « Cervin »"}, "lang": _LANG},
             ["title"])),
    "avalanche_bulletin": (
        "Bulletin d'avalanches officiel du SLF en vigueur : degré de danger et problèmes avalancheux par région "
        "suisse. Pour le danger d'avalanches en Suisse, préférer à web_search. Aucun bulletin hors saison.",
        _obj({"region": {"type": _S, "description": "facultatif : région ou vallée suisse, ex. « Davos » ; vide = "
                                                    "vue d'ensemble"}, "lang": _LANG}, [])),
    "unit_convert": (
        "Convertit une valeur d'une unité à une autre (longueur, masse, volume, surface, vitesse, durée, données, "
        "énergie, pression, puissance, température). Calcul local exact : préférer à web_search et au calcul de tête.",
        _obj({"value": {"type": _N, "description": "valeur à convertir"},
              "from_unit": {"type": _S, "description": "unité de départ, ex. km, lb, °F"},
              "to_unit": {"type": _S, "description": "unité d'arrivée, ex. mi, kg, °C"}},
             ["value", "from_unit", "to_unit"])),
    "math_calc": (
        "Calcule une expression arithmétique (+ - * / ^ %, parenthèses, sqrt, log, sin…). Calcul local exact : "
        "préférer au calcul de tête et à web_search.",
        _obj({"expression": {"type": _S, "description": "l'expression, avec un point décimal, ex. (12.5*3)^2"}},
             ["expression"])),
    "date_calc": (
        "Calculs de dates : jour de la semaine, écart entre deux dates (jours, jours ouvrables), date plus ou moins "
        "N jours. Calcul local exact : préférer au calcul de tête et à web_search.",
        _obj({"start": {"type": _S, "description": "date AAAA-MM-JJ ou « aujourd'hui »"},
              "end": {"type": _S, "description": "facultatif : seconde date, pour l'écart"},
              "days": {"type": _I, "description": "facultatif : jours à ajouter (négatif pour retrancher)"}},
             ["start"])),
}

NAMES = tuple(TOOLS)


def texts_sha256() -> str:
    """Empreinte des textes vus par le modèle (descriptions + schémas), recopiée dans docs/ESSENTIALS.md."""
    import hashlib
    import json

    blob = json.dumps(TOOLS, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
# Familles de `tools/select.py` (index compact, chargement par mots-clés).
ESS_METEO = ("weather_ch", "pollen", "avalanche_bulletin")
ESS_TRANSIT = ("transit_departures", "transit_trip")
ESS_WIKI = ("wiki_summary",)
ESS_CALC = ("fx_rate", "holidays", "unit_convert", "math_calc", "date_calc")
assert set(ESS_METEO + ESS_TRANSIT + ESS_WIKI + ESS_CALC) == set(NAMES)

__all__ = ["ESS_CALC", "ESS_METEO", "ESS_TRANSIT", "ESS_WIKI", "NAMES", "TOOLS", "texts_sha256"]
