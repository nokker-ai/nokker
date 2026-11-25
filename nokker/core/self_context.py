"""Bloc `[Environnement]` : où tourne le fil, par quel canal la personne écrit, quelles machines liées existent.

Le modèle ne connaissait ni le vocabulaire de son produit ni l'endroit où il tourne (« Tu tournes sur Lokkal ? » → « pas
sûr, c'est quoi, Lokkal ? »). Le bloc entre dans l'en-tête figé du tour (`Turn.head`, `ContextEngineV0.turn_head`) : calculé
au premier assemblage, rejoué tel quel ensuite, jamais dans le préfixe stable (l'empreinte `prefix_sha256` ne bouge pas).

Règles : au plus 5 lignes ; une information inconnue est OMISE (jamais devinée) ; aucun identifiant opaque (le nom d'une
machine liée est parfois le hachage du conteneur : « 61d51b276e7b » devient « une machine liée »). Le canal « api » ne dit
pas si la personne écrit depuis l'application web, le terminal ou une requête directe : sa ligne est omise.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from nokker.core import self_context_texts as tx

PLACES = ("cloud", "local", "linked")
MAX_NAMES = 5
NAME_MAX = 40

_OPAQUE = (
    re.compile(r"^[0-9a-f]{8,}$", re.IGNORECASE),                       # hachage ou nom de conteneur (12 hexa)
    re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.IGNORECASE),  # UUID
    re.compile(r"^(mc|us|tn|th|ho|cs|dv)_[A-Za-z0-9]+$"),               # identifiants du service
    re.compile(r"^[0-9]{6,}$"),
)
_UNSAFE = re.compile(r"[^\w .'()+-]", re.UNICODE)


def readable_name(name: str | None) -> str | None:
    """Nom de machine montrable au modèle, sinon None (vide, identifiant opaque). Une seule ligne, sans balise ni guillemet."""
    text = " ".join(_UNSAFE.sub(" ", str(name or "")).split())[:NAME_MAX].strip(" .-")
    if not text or not any(c.isalpha() for c in text):
        return None
    if any(rx.match(text) for rx in _OPAQUE):
        return None
    return text


def _names(machines: Iterable[str | None], lang: str) -> str:
    named: list[str] = []
    unnamed = 0
    for m in machines:
        n = readable_name(m)
        if n is None:
            unnamed += 1
        elif n not in named:
            named.append(n)
    shown, extra = named[:MAX_NAMES], len(named) - MAX_NAMES
    items = [tx.pick(tx.QUOTED, lang).format(name=n) for n in shown]
    if extra > 0:
        items.append(tx.pick(tx.MORE, lang).format(k=extra))
    if unnamed:
        items.append(tx.pick(tx.UNNAMED_ONE, lang) if unnamed == 1 else tx.pick(tx.UNNAMED_MANY, lang).format(n=unnamed))
    return ", ".join(items)


def normalize_surface(raw: object) -> str | None:
    """Valeur de l'en-tête `X-Nokker-Surface` : l'une de `HEADER_SURFACES` (casse et espaces tolérés), sinon None. Jamais
    la valeur reçue n'est reprise : seule la clé de la liste fermée sort."""
    if not isinstance(raw, str) or len(raw) > 32:
        return None
    value = raw.strip().lower()
    return value if value in tx.HEADER_SURFACES else None


def render(*, channel: str | None = None, place: str | None = None, local_name: str | None = None,
           machines: Iterable[str | None] = (), lang: str | None = None) -> str:
    """Le bloc, ou "" quand rien n'est connu. `place` : `cloud`, `local` ou `linked` (Lokkal atteint depuis le cloud) ;
    `local_name` : nom de CETTE machine (Lokkal) ; `machines` : noms des machines liées au compte (cloud)."""
    machines = list(machines)
    lines: list[str] = []
    surface = tx.SURFACES.get(str(channel or ""))
    if surface is not None:
        lines.append(tx.pick(tx.CHANNEL, lang).format(surface=tx.pick(surface, lang)))
    name = readable_name(local_name)
    quoted = tx.pick(tx.QUOTED, lang).format(name=name) if name else ""
    if place == "cloud":
        lines.append(tx.pick(tx.PLACE_CLOUD, lang))
        names = _names(machines, lang)
        if names:
            many = len(machines) > 1
            lines.append(tx.pick(tx.MACHINES_MANY if many else tx.MACHINES_ONE, lang).format(names=names))
    elif place == "local":
        lines.append(tx.pick(tx.PLACE_LOCAL, lang).format(name=quoted) if name else tx.pick(tx.PLACE_LOCAL_BARE, lang))
    elif place == "linked":
        lines.append(tx.pick(tx.PLACE_LINKED, lang).format(name=quoted) if name else tx.pick(tx.PLACE_LINKED_BARE, lang))
    if not lines:
        return ""
    return "\n".join([tx.OPEN, *lines, tx.CLOSE])


__all__ = ["PLACES", "normalize_surface", "readable_name", "render"]
