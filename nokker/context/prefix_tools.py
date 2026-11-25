"""Préfixe qui suit le catalogue du fil : une ligne qui nomme des outils porte une marque en fin de ligne, lue au rendu.

Formes (une marque par ligne, en commentaire HTML, jamais envoyée au modèle) :

  `texte <!-- needs: a,b -->`        ligne en tête d'un groupe ; rendue si AUCUN des outils a, b n'est retiré du fil ; une condition `a|b`
                                     est satisfaite si AU MOINS UN des deux reste (ex. `read_file|search_files` : « on peut encore lire ») ;
  `variante <!-- or needs: c -->`    variante suivante du groupe, rendue si la précédente ne l'est pas et que c est présent ;
  `variante <!-- or -->`             dernier repli du groupe, toujours rendu s'il est atteint.

`terminal` seul vaut « on peut lancer une commande » : `execute_code` n'y ajoute rien, ses appels d'outils passent par les mêmes portes.

Un groupe dont aucune ligne n'est rendue disparaît (ligne comprise). Les variantes suivent immédiatement la tête, sans ligne
vide entre elles. Sans outil retiré, le rendu est le fichier d'avant les marques, à l'octet près : la tête de chaque groupe est
rendue, ses variantes ne le sont jamais. Le jeu d'outils retirés est posé à l'ouverture de la session (`deny_tools`) : le
préfixe d'un fil ne change donc pas pendant sa vie, et le cache du fournisseur n'est pas perturbé à l'intérieur d'une session.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

_MARK = re.compile(r"^(?P<body>.*?) ?<!-- (?P<kind>needs|or needs|or)(?:: (?P<names>[A-Za-z0-9_.:,|-]+))? -->$")
_NUMBERED = re.compile(r"^(\d+)\. ")
_EMPTY_HEADING = re.compile(r"^(#{1,6}) [^\n]*\n\n(?=(#{1,6}) |\Z)", re.MULTILINE)


def _parse(line: str) -> tuple[str, str, tuple[str, ...]] | None:
    """(texte sans marque, genre, conditions) d'une ligne marquée, sinon None. Une condition est un nom d'outil, ou `a|b` : au moins un des deux."""
    m = _MARK.match(line)
    if m is None:
        return None
    terms = tuple(n for n in (m.group("names") or "").split(",") if n)
    return m.group("body"), m.group("kind"), terms


def named(text: str) -> frozenset[str]:
    """Tous les outils cités par les marques du texte."""
    out: set[str] = set()
    for line in text.splitlines():
        p = _parse(line)
        if p is not None:
            out |= {n for term in p[2] for n in term.split("|")}
    return frozenset(out)


def relevant(text: str, hidden: Iterable[str]) -> frozenset[str]:
    """Parmi les outils retirés, ceux que le préfixe nomme (les seuls qui changent son rendu)."""
    return named(text) & frozenset(hidden)


def render(text: str, hidden: Iterable[str] = ()) -> str:
    """Le préfixe d'un fil dont les outils `hidden` sont retirés ; marques toujours ôtées."""
    gone = frozenset(hidden)
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    removed = False
    i = 0
    while i < len(lines):
        raw = lines[i]
        eol = raw[len(raw.rstrip("\r\n")):]
        p = _parse(raw.rstrip("\r\n"))
        if p is None:
            out.append(raw)
            i += 1
            continue
        group = [(p[0], p[1], p[2])]
        i += 1
        while i < len(lines):
            q = _parse(lines[i].rstrip("\r\n"))
            if q is None or not q[1].startswith("or"):
                break
            group.append(q)
            i += 1
        chosen = next((g for g in group if all(set(t.split("|")) - gone for t in g[2])), None)
        if chosen is None:
            removed = True
            continue
        if chosen is not group[0]:
            removed = True
        out.append(chosen[0] + (eol or "\n" if i < len(lines) else eol))
    rendered = "".join(out)
    return _tidy(rendered) if removed else rendered


def _tidy(text: str) -> str:
    """Après un retrait : pas de ligne vide en trop, pas de titre sans contenu, listes numérotées resserrées."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = _EMPTY_HEADING.sub(lambda m: m.group(0) if m.group(2) and len(m.group(2)) > len(m.group(1)) else "", text)  # titre sans contenu : seulement s'il n'ouvre pas une sous-section
    lines = text.split("\n")
    n = 0
    for k, line in enumerate(lines):
        m = _NUMBERED.match(line)
        if m is None:
            n = 0
            continue
        n += 1
        if int(m.group(1)) != n:
            lines[k] = f"{n}. " + line[m.end():]
    return "\n".join(lines)


__all__ = ["named", "relevant", "render"]
