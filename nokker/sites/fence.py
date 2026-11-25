"""Sites connectés — clôture et traitement renforcé contre l'injection (docs/CONNECTED-SITES.md §10, textes §8.6).

Tout texte venu d'un site (nom affiché compris) entre dans `<untrusted-data kind="site:<site>:<champ>" id=…>`, après
normalisation (caractères invisibles, contrôles, balises Unicode, surcharges bidirectionnelles). Un élément qui
s'adresse à un assistant porte `suspect="consigne"` et le résultat commence par l'en-tête exact du §8.6.
"""
from __future__ import annotations

from nokker.qa.untrusted import fence_checked, is_consigne, normalize, suspect_header


def fence(site: str, field: str, text: str, *, check: bool = True) -> tuple[str, bool]:
    """(bloc clôturé, suspect ?) : la clôture commune (`qa/untrusted.py::fence_checked`), kind `site:<site>:<champ>`."""
    return fence_checked(f"site:{site}:{field}", text, check=check)


def header(n: int) -> str:
    return suspect_header(n)


class Fencer:
    """Accumule les champs d'un résultat et compte les éléments marqués (un élément = un champ clôturé)."""

    def __init__(self, site: str) -> None:
        self.site = site
        self.suspects = 0

    def __call__(self, field: str, text: str) -> str:
        block, suspect = fence(self.site, field, text)
        self.suspects += int(suspect)
        return block

    def wrap(self, body: str) -> str:
        head = header(self.suspects)
        return f"{head}\n{body}" if head else body


__all__ = ["Fencer", "fence", "header", "is_consigne", "normalize"]
