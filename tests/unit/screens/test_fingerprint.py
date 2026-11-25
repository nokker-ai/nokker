"""Empreinte des textes de V7 vus par le modèle, figée à la relecture : ALL_MODEL_TEXTS, PARAMETERS et catalog.py (les
messages de refus). Un changement repasse par la relecture, puis met FROZEN à jour. Les textes vus par la PERSONNE
(raison de la carte, rendu) sont gelés par citation, hors de cette empreinte."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from nokker.screens import texts as T
from nokker.tools.select import FAMILIES

FROZEN = "ebb5ff6f3914f82350b81e85dabcbb93e46c18b343583a8a71b7a1c01913ea09"


def fingerprint() -> str:
    h = hashlib.sha256()
    h.update(json.dumps(T.ALL_MODEL_TEXTS, ensure_ascii=False).encode())
    h.update(json.dumps(T.PARAMETERS, sort_keys=True, ensure_ascii=False).encode())
    h.update((Path(__file__).resolve().parents[3] / "nokker" / "screens" / "catalog.py").read_bytes())
    fam = next(f for f in FAMILIES if "screen_draft" in f.patterns)  # libellé de l'index des outils, vu par le modèle
    h.update(json.dumps([fam.label, list(fam.patterns), list(fam.load)], ensure_ascii=False).encode())
    return h.hexdigest()


def test_model_texts_are_frozen():
    assert fingerprint() == FROZEN, f"textes V7 changés : relecture, puis FROZEN = {fingerprint()!r}"


import pytest  # noqa: E402

from nokker.tools.select import relevant_tools  # noqa: E402


@pytest.mark.parametrize("text,loads", [
    ("fais-moi un écran pour le mur de la cuisine", True),
    ("fais-moi un écran avec les courses de la semaine", True),
    ("make me a screen for the kitchen", True),
    ("compose un écran du foyer pour ce soir", True),
    ("un écran de la cuisine avec le menu", True),
    ("put it on the wall", True),
    ("capture d'écran de la facture", False),
    ("mon écran est cassé", False),
    ("mon temps d'écran cette semaine", False),
    ("send me a screenshot", False),
    ("l'écran de mon téléphone", False),
])
def test_wall_screen_family_triggers(text, loads):
    from nokker.core.ports import ToolSpec
    from nokker.core.turn import Effect

    spec = ToolSpec(name="screen_draft", description="x", parameters={}, effect=Effect.READ)
    assert bool(relevant_tools(text, set(), [spec])) is loads
