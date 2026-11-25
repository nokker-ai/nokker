"""ACCORD — niveau et titre d'une carte montrée sur l'appareil (docs/ACCORD-DEVICE.md §3-§4).

Titre : le libellé déjà construit par le noyau pour le heurtoir (`spoken_label` : outil + nom de la cible, sans dossier,
jamais le motif de la carte), borné à 60 caractères et normalisé NFC ; aucun texte nouveau. Niveau : `ordinary`
seulement pour une carte STANDARD hors des familles qu'un geste physique n'approuve jamais (mêmes listes fermées que le
heurtoir, plus les planchers de `decide_local`) ; tout le reste est `strong`, que l'appareil peut seulement REFUSER."""
from __future__ import annotations

import unicodedata
from typing import Any

from nokker.knock.policy import NEVER_BY_KNOCK, NEVER_BY_KNOCK_PREFIXES, clean_label

# Planchers de `ApprovalsBase.decide_local` (approuver exige une clé d'appareil) : jamais `ordinary` ici non plus.
LOCAL_FLOORS = frozenset({"household:guardian", "ops:prod", "home:strong", "home_watch:strong", "pages:publish"})


def level_of(approval: Any) -> str:
    rule = str(getattr(approval, "rule", "") or "")
    if getattr(approval, "strength", None) != "standard":
        return "strong"
    if rule in NEVER_BY_KNOCK or rule in LOCAL_FLOORS or rule.startswith(NEVER_BY_KNOCK_PREFIXES):
        return "strong"
    return "ordinary"


def title_of(approval: Any) -> str:
    from nokker.knock.hub import spoken_label

    return unicodedata.normalize("NFC", clean_label(spoken_label(approval), 60)).strip()
