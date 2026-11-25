"""Nombres purs (docs/INVARIANTS.md §1, « Nombres purs ») — pur, sans état ni dépendance.

Un résultat d'outil numérique (`0.30000000000000004`, `3.141592653589793`, `-3.14e-05`, `6.02214076e23`) n'est pas un
secret. Les replis du caviardage (hexadécimal, entropie, numéro de carte) le prenaient pour un : une suite de 16 chiffres
ou un « e » entre deux chiffres ressemble à un hexadécimal, une suite de chiffres décimaux passe le test de Luhn une
fois sur dix. `number_spans(text)` rend les intervalles du texte qui sont UN nombre : décimal (UN point entre deux chiffres)
ou scientifique (exposant), signé ou non. Un nombre collé à une lettre, à un « _ » ou à
un signe précédé d'un caractère de mot (`sk-1234…`, `token=1234…ab`, `a1.5`) n'en est PAS un : le jeton entier reste
candidat. Un entier nu (même court) n'est PAS un nombre ici et reste jugé comme avant ; un jeton à plusieurs points
(AVS `756.1234.5678.97`, adresse IP) non plus. Cas ambigu : on masque.
"""
from __future__ import annotations

import re

_WORD = r"A-Za-z0-9_"
_BODY = r"(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+\-]?\d+)?"
# Début : un signe (précédé ni d'un caractère de mot, ni d'un autre signe, ni d'un point) ou rien, avec la même garde. Fin :
# ni caractère de mot, ni « .chiffre » (un jeton à plusieurs points, `756.1234.5678.97`, n'est PAS un décimal).
_NUMBER_RE = re.compile(
    rf"(?:(?<![{_WORD}+\-.])[+\-−]|(?<![{_WORD}+\-−.]))" + _BODY + rf"(?![{_WORD}]|\.\d)")
# Seules les formes DÉCIMALE (un point entre deux chiffres) et SCIENTIFIQUE (exposant) sont des nombres : un entier nu, même
# court, n'en est pas un ici (il reste jugé comme avant). Un point de fin de phrase (« carte : 4242… ») n'appartient pas
# au nombre : le point exige un chiffre après lui. Une notation `123e456` SANS point ni signe d'exposant et de 16
# caractères ou plus est ambiguë avec un hexadécimal (`141963579e003574`) : elle n'est pas un nombre.


def _hex_like(tok: str) -> bool:
    return len(tok) >= 16 and "." not in tok and re.fullmatch(r"\d+[eE]\d+", tok) is not None


def number_spans(text: str) -> list[tuple[int, int]]:
    """Intervalles `(début, fin)` des nombres purs de `text`, dans l'ordre."""
    if not text or not any(c.isdigit() for c in text):
        return []
    return [m.span() for m in _NUMBER_RE.finditer(text)
            if re.search(r"[.eE]", m.group(0)) and not _hex_like(m.group(0))]


def parts(token: str) -> tuple[str, str, bool]:
    """`(partie entière, chiffres de la mantisse sans point, scientifique ?)` d'un nombre de `number_spans`."""
    body = token.lstrip("+-−")
    mant = re.split(r"[eE]", body)[0]
    return mant.split(".")[0], mant.replace(".", ""), mant != body


def exempt_spans(text: str, int_limit: int) -> list[tuple[int, int]]:
    """Intervalles de `number_spans` qu'un repli peut épargner. Garde-fou (cas ambigu : on masque) : un décimal dont la
    PARTIE ENTIÈRE a `int_limit` chiffres ou plus (`4242424242424242.0`, produit par un tableur ou un cast JSON) n'est pas
    épargné, ni un nombre scientifique dont la mantisse sans point a `int_limit` chiffres ou plus (`4.242424242424242e15`) :
    la valeur reste jugée comme un entier."""
    out = []
    for a, b in number_spans(text):
        integer, mant, sci = parts(text[a:b])
        if len(integer) >= int_limit or (sci and len(mant) >= int_limit):
            continue
        out.append((a, b))
    return out


def inside(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    """Le segment `[start, end)` est entièrement contenu dans un des intervalles."""
    return any(s <= start and end <= e for s, e in spans)


def is_pure_number(token: str) -> bool:
    """Le jeton entier est un nombre pur."""
    spans = number_spans(token)
    return bool(spans) and spans[0] == (0, len(token))


__all__ = ["exempt_spans", "inside", "is_pure_number", "number_spans", "parts"]
