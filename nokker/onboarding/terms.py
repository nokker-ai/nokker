"""Conditions de la bêta (terms/fr.md, terms/en.md). Ce que le testeur accepte, c'est le SHA-256 des octets affichés :
modifier un fichier change la version, donc l'écran d'acceptation périmé est refusé au lieu d'enregistrer un texte
que la personne n'a pas lu. Ces textes sont un brouillon tant que Nick ne les a pas validés (voir le dépôt interne,
docs/nokker/conditions-beta-2026-10.md)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

LANGS = ("fr", "en")
_DIR = Path(__file__).parent / "terms"


@dataclass(frozen=True)
class Terms:
    lang: str
    text: str
    sha256: str


@cache
def load(lang: str) -> Terms:
    lang = lang if lang in LANGS else "fr"
    raw = (_DIR / f"{lang}.md").read_bytes()
    return Terms(lang=lang, text=raw.decode(), sha256=hashlib.sha256(raw).hexdigest())


def lang_of_version(sha256: str) -> str | None:
    """Langue du texte courant dont c'est l'empreinte, ou None si la version est inconnue ou périmée."""
    return next((lang for lang in LANGS if load(lang).sha256 == sha256), None)
