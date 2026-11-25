"""Prénom d'un membre et nom montré : UNE seule règle pour les connecteurs (068), les sites connectés et l'écran.

- `clean_first_name(v)` : prénom saisi (Mon compte, invitation au foyer). Texte vu par le modèle (« partagée par
  Alice ») : lettres, espaces, trait d'union, apostrophe et point seulement, 40 caractères au plus, espaces réduits.
  Vide = effacé. Lève `ValueError("bad_first_name")` sinon : aucune autre ponctuation, aucun chiffre, aucun retour à la
  ligne, aucun balisage ne passe.
- `member_name(first_name, email)` : le prénom tel que saisi, sinon la partie locale de l'adresse (comme avant).
- `owner_name(households, account_id)` : nom du propriétaire d'une ressource partagée, lu au foyer
  (`HouseholdLink.of(compte).member(compte).name`) ; foyer illisible ou membre absent : « son propriétaire ».
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

FIRST_NAME_MAX = 40
FIRST_NAME_MAX_WORDS = 3  # un prénom, pas une phrase : « Ignore les consignes précédentes » (4 mots) est refusé
UNKNOWN_OWNER = "son propriétaire"
_ALLOWED_PUNCT = frozenset(" -'.’")


def clean_first_name(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("bad_first_name")
    v = unicodedata.normalize("NFC", value)
    if any(c in "\r\n\t" for c in v):
        raise ValueError("bad_first_name")
    v = re.sub(r" +", " ", v.strip())
    if len(v) > FIRST_NAME_MAX or len(v.split(" ")) > FIRST_NAME_MAX_WORDS:
        raise ValueError("bad_first_name")
    for c in v:
        if c in _ALLOWED_PUNCT:
            continue
        cat = unicodedata.category(c)
        if not (cat.startswith("L") or cat == "Mn"):  # lettres de toutes écritures, accents combinés
            raise ValueError("bad_first_name")
    if v and not any(unicodedata.category(c).startswith("L") for c in v):
        raise ValueError("bad_first_name")
    return v.replace("’", "'")


def member_name(first_name: str, email: str) -> str:
    """Nom montré d'un membre : son prénom tel que saisi, sinon la partie locale de son adresse."""
    if first_name:
        return first_name
    return (email or "").split("@", 1)[0] or "?"


def owner_name(households: Any, account_id: str) -> str:
    """Nom du propriétaire d'une ressource partagée dans le foyer, à la même source pour 068 et les sites."""
    if households is None or not account_id:
        return UNKNOWN_OWNER
    try:
        hh = households.of(account_id)
        m = hh.member(account_id) if hh is not None else None
    except Exception:  # noqa: BLE001 — foyer illisible : jamais l'identifiant brut
        m = None
    return m.name if m is not None else UNKNOWN_OWNER


__all__ = ["FIRST_NAME_MAX", "FIRST_NAME_MAX_WORDS", "UNKNOWN_OWNER", "clean_first_name", "member_name", "owner_name"]
