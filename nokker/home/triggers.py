"""Mots déclencheurs de la famille d'outils `maison` (docs/HOME-ASSISTANT.md §11.1), relus par la relecture des textes.

Deux listes :
  - ALONE : un de ces mots suffit (« éteins la lampe », « ferme le volet ») ;
  - WITH_OBJECT : ces mots ne déclenchent qu'avec un objet de la maison (HOME_OBJECTS) dans la même phrase. Ainsi
    « 25 degrés demain ? » (météo) et « il joue au foot » ne chargent pas `maison`, alors que « chauffage à 21 degrés »
    et « joue de la musique » la chargent.
« historique », « météo » et « agenda » ne déclenchent jamais seuls (weather_ch et calendar_events d'abord).
"""
from __future__ import annotations

import re
import unicodedata

ALONE: tuple[str, ...] = (
    "lumière", "lampe", "allume", "éteins", "prise", "volet", "store", "chauffage", "thermostat", "musique",
    "playlist", "spotify", "enceinte", "volume", "liste de courses", "aspirateur", "serrure", "verrouille", "garage",
    "portail", "alarme", "caméra", "sonnette", "maison",
)
WITH_OBJECT: tuple[str, ...] = ("mets", "suivant", "suivante", "pause", "porte", "annonce", "degrés", "joue")
HOME_OBJECTS: tuple[str, ...] = (
    "musique", "chanson", "morceau", "titre", "album", "playlist", "radio", "enceinte", "sonos", "spotify",
    "salon", "chambre", "cuisine", "bureau", "entrée", "cave", "jardin", "garage", "chauffage", "thermostat",
    "radiateur", "clim", "lumière", "lampe", "volet", "store", "télé",
)
NEVER_ALONE: tuple[str, ...] = ("historique", "météo", "agenda")
NOTIFIER: tuple[str, ...] = ("notifie", "préviens sur son téléphone", "envoie une notification")
# « quand … ouvert ? » : question sur le passé d'une porte, d'une fenêtre ou d'un volet
_WHEN_OPEN = re.compile(r"\bquand\b.*\b(ouvert|ouverte|ferm[ée]e?|verrouill[ée]e?)\b")


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", (s or "").casefold()) if not unicodedata.combining(c))


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<!\w){re.escape(_fold(w))}(?!\w)", text) for w in words)


def triggers(text: str) -> bool:
    """La phrase charge-t-elle la famille `maison` ?"""
    t = _fold(text)
    if _has(t, ALONE) or _WHEN_OPEN.search(t):
        return True
    return _has(t, WITH_OBJECT) and _has(t, HOME_OBJECTS)


def as_dict() -> dict[str, list[str]]:
    return {"alone": list(ALONE), "with_object": list(WITH_OBJECT), "home_objects": list(HOME_OBJECTS),
            "never_alone": list(NEVER_ALONE), "notifier": list(NOTIFIER), "when_open": [_WHEN_OPEN.pattern]}


__all__ = ["ALONE", "HOME_OBJECTS", "NEVER_ALONE", "NOTIFIER", "WITH_OBJECT", "as_dict", "triggers"]
