"""Profils mineurs aux frontières du noyau (lot X23, docs/LOKKAL.md §7.29.4) : petites enveloppes posées par le
serveur quand `household_minors` est allumé. Chacune dit non pour un compte mineur et ne change rien pour les autres."""
from __future__ import annotations

from collections.abc import Callable


def no_minor_mirror(allowed: Callable[[str], bool] | None,
                    is_minor: Callable[[str], bool]) -> Callable[[str], bool] | None:
    """`mirror_allowed` de nokker.ai (tours, notes, pages, notes d'équipe) : jamais pour un compte mineur — aucune
    donnée d'un mineur ne part vers le miroir. Miroir éteint (None) : il le reste."""
    if allowed is None:
        return None

    def check(account_id: str) -> bool:
        return not is_minor(account_id) and allowed(account_id)

    return check


def no_minor_public_pages(allowed: Callable[[str], bool] | None,
                          is_minor: Callable[[str], bool]) -> Callable[[str], bool] | None:
    """`pages_public_allowed` de nokker.ai (§7.26, lu par la machine et le relais) : un compte mineur ne publie
    jamais de page publique. Pages publiques éteintes (None) : elles le restent."""
    return no_minor_mirror(allowed, is_minor)


def minor_may_create_agent(name: str, whitelist: list[str]) -> bool:
    """Un profil mineur ne crée un agent que si son nom est dans la liste blanche du foyer (`minor_agents`)."""
    return bool(name) and name.strip().lower() in {w.lower() for w in whitelist}


__all__ = ["minor_may_create_agent", "no_minor_mirror", "no_minor_public_pages"]
