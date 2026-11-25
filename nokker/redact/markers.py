"""Marqueurs de valeurs masquées sur l'appareil (docs/LOKKAL-PHONE.md §4.3.2, lot P9b) — pur, sans dépendance.

L'app du téléphone masque un texte produit sur l'appareil AVANT l'envoi (règles de `GET /v1/redact/rules`) : chaque
valeur devient un marqueur `⟦CODE-n⟧` (U+27E6, U+27E7), la valeur reste dans un dictionnaire local de l'appareil. Le
noyau ne connaît jamais la valeur ; il garantit trois choses :

  1. son propre caviardage ne touche jamais un marqueur (`nokker/qa/secrets.py::redact` découpe le texte autour) ;
  2. la consigne `CONSIGNE` entre UNE fois par fil dans l'en-tête du premier tour qui porte un marqueur
     (`consigne_needed`, posé par `nokker/context/engine.py` juste avant `[Pièces jointes]`) ;
  3. un outil à effet externe qui reçoit un marqueur est refusé avant toute porte (`nokker/redact/guard.py`).
"""
from __future__ import annotations

import re
from typing import Any

FAMILIES = ("PHONE", "EMAIL", "IBAN", "AHV", "CARD", "BIRTHDATE", "ADDRESS", "SECRET")

MARKER_PATTERN = "⟦(" + "|".join(FAMILIES) + ")-[1-9][0-9]{0,3}⟧"
MARKER_RE = re.compile(MARKER_PATTERN)

# Texte vu par le modèle, validé mot pour mot (empreinte figée dans tests/unit/redact/test_device_redact.py).
CONSIGNE = ("[Valeurs masquées]\n"
            "Les marqueurs comme ⟦PHONE-1⟧ remplacent des valeurs masquées sur l'appareil de la personne : tu ne les "
            "connais pas. Pour t'y référer, recopie le marqueur tel quel ; ne le devine pas, ne le reformate pas, ne le "
            "traduis pas. La personne voit la vraie valeur à sa place. Un outil qui envoie, paie ou écrit ailleurs "
            "refuse un marqueur.\n"
            "[/Valeurs masquées]")


# Variante quand un marqueur du fil vient du BUREAU (docs/DESKTOP.md §3.6) : le bureau ne démasque rien, la personne voit
# le marqueur. Seule la phrase « La personne … » change (relecture du 06.11) ; CONSIGNE et son empreinte restent intactes.
# Posée À LA PLACE de CONSIGNE dès qu'un marqueur du fil vient d'un outil `desktop_*` (fil mixte compris).
CONSIGNE_DESKTOP = ("[Valeurs masquées]\n"
                    "Les marqueurs comme ⟦PHONE-1⟧ remplacent des valeurs masquées sur l'appareil de la personne : tu ne "
                    "les connais pas. Pour t'y référer, recopie le marqueur tel quel ; ne le devine pas, ne le reformate "
                    "pas, ne le traduis pas. La personne ne voit pas toujours la vraie valeur à sa place : si elle en a "
                    "besoin, dis-lui où la lire à l'écran. Un outil qui envoie, paie ou écrit ailleurs refuse un "
                    "marqueur.\n"
                    "[/Valeurs masquées]")
DESKTOP_TOOL_PREFIX = "desktop_"


# Ce qui remplace un marqueur dans ce qui est gardé au-delà du fil (consolidation de la mémoire) : validé (relecture des textes).
UNMARKED = "[valeur masquée]"


def unmark(text: str) -> str:
    """`text` sans aucun marqueur : chacun devient « [valeur masquée] ». Pure ; pour tout texte gardé hors du fil."""
    return MARKER_RE.sub(UNMARKED, text) if text and "⟦" in text else text


def has_marker(text: str | None) -> bool:
    return bool(text) and "⟦" in text and MARKER_RE.search(text) is not None


def markers_in(obj: Any) -> list[str]:
    """Marqueurs DISTINCTS d'une valeur JSON (chaînes, listes, dicts — clés comprises — parcourus récursivement),
    dans l'ordre d'apparition."""
    found: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, str):
            if "⟦" in x:
                for m in MARKER_RE.finditer(x):
                    if m.group(0) not in found:
                        found.append(m.group(0))
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(k)
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)

    walk(obj)
    return found


def _turn_has_marker(turn: Any) -> bool:
    inp = getattr(turn, "input", None)
    if has_marker(getattr(inp, "text", "")) or has_marker(getattr(turn, "attached", "")):
        return True
    if any(has_marker(x) for x in getattr(turn, "injected", None) or []):
        return True
    for m in getattr(turn, "messages", None) or []:
        if has_marker(getattr(m, "content", "")):
            return True
        if any(markers_in(getattr(c, "arguments", None)) for c in getattr(m, "tool_calls", None) or []):
            return True
    return False


def consigne_needed(turn: Any, history: list[Any]) -> bool:
    """Vrai pour le premier tour (de l'historique visible) où un marqueur apparaît : ce tour en porte un, ou un tour
    précédent en portait un sans que la consigne ait été posée (marqueur arrivé par un résultat d'outil après le
    figement de l'en-tête). Faux dès qu'un en-tête de l'historique porte déjà la consigne : une fois par fil."""
    if any(c in (getattr(p, "head", "") or "") for p in history for c in (CONSIGNE, CONSIGNE_DESKTOP)):
        return False
    return _turn_has_marker(turn) or any(_turn_has_marker(p) for p in history)


def _desktop_marker(turn: Any) -> bool:
    """Un résultat d'outil `desktop_*` de ce tour porte un marqueur (nom du message, ou appel retrouvé par son id)."""
    names: dict[str, str] = {}
    for m in getattr(turn, "messages", None) or []:
        for c in getattr(m, "tool_calls", None) or []:
            names[getattr(c, "id", "")] = getattr(c, "name", "")
    for m in getattr(turn, "messages", None) or []:
        if getattr(m, "role", "") != "tool" or not has_marker(getattr(m, "content", "")):
            continue
        name = getattr(m, "name", None) or names.get(getattr(m, "tool_call_id", None) or "", "")
        if str(name or "").startswith(DESKTOP_TOOL_PREFIX):
            return True
    return False


def desktop_consigne_needed(turn: Any, history: list[Any]) -> bool:
    """Vrai si un marqueur du fil vient du bureau et que CONSIGNE_DESKTOP n'a pas encore été posée : elle l'est alors,
    une fois, même si CONSIGNE l'a été avant (fil mixte : la phrase du téléphone ne vaut plus)."""
    if any(CONSIGNE_DESKTOP in (getattr(p, "head", "") or "") for p in history):
        return False
    return _desktop_marker(turn) or any(_desktop_marker(p) for p in history)


__all__ = ["CONSIGNE", "CONSIGNE_DESKTOP", "FAMILIES", "desktop_consigne_needed", "MARKER_PATTERN", "MARKER_RE", "UNMARKED", "consigne_needed", "has_marker",
           "markers_in", "unmark"]
