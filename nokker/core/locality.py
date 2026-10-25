"""Localité des ressources (docs/LOKKAL.md §6.7, lot X21) : où vit une ressource, et donc ce qui peut en sortir.

Trois valeurs, portées par les outils (`ToolSpec.locality`), les connecteurs, les secrets et les notes de mémoire :
  - `local` : sur la machine (dépôt, hôte ssh, coffre local, connecteur posé dans Lokkal, note de projet par défaut) ;
    n'en sort JAMAIS : ni le miroir ni la passerelle ne l'envoient chez nokker.ai ;
  - `cloud` : chez nokker.ai (connecteur posé au web, son identifiant y reste) ; la machine l'appelle par relais (X20) ;
  - `both`  : des deux côtés (notes de l'utilisateur, notes d'un projet mis en miroir), synchronisée par le miroir.

Une localité absente ou inconnue est traitée comme `local` par le filtre à l'envoi (`may_leave`) : ce qui n'a pas dit
où il vit ne part pas (défaut prudent, comme `Effect.WRITE` pour un outil qui ne dit pas son effet).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar

LOCAL, CLOUD, BOTH = "local", "cloud", "both"
LOCALITIES = (LOCAL, CLOUD, BOTH)

T = TypeVar("T")


def check(value: str | None) -> str | None:
    """La localité telle quelle si elle est connue (ou None, non déclarée) ; ValueError sinon."""
    if value is None or value in LOCALITIES:
        return value
    raise ValueError(f"localité inconnue : {value!r} (attendu : {', '.join(LOCALITIES)})")


def may_leave(locality: str | None) -> bool:
    """True si la ressource peut partir chez nokker.ai (miroir, passerelle) : `both` ou `cloud` seulement."""
    return locality in (BOTH, CLOUD)


def outbound(items: Iterable[T], locality_of: Callable[[T], str | None]) -> list[T]:
    """Filtre à l'envoi : ne garde que ce qui peut quitter la machine. Le miroir et la passerelle passent tout par ici
    juste avant d'écrire sur le réseau, même ce qu'ils ont déjà choisi (un tri en amont peut changer, pas ce filtre)."""
    return [it for it in items if may_leave(locality_of(it))]


def of(obj: Any) -> str | None:
    """Localité d'un objet (attribut `locality`) ou d'un dict (clé `locality`)."""
    if isinstance(obj, dict):
        return obj.get("locality")
    return getattr(obj, "locality", None)


__all__ = ["BOTH", "CLOUD", "LOCAL", "LOCALITIES", "check", "may_leave", "of", "outbound"]
