"""Fournisseurs d'outils des agents de la salle Maison (docs/HOUSE-ROOM.md §2.2).

Chaque chantier de la maison branche ici la `ToolSource` de SON agent, au lieu d'ajouter ses outils au registre des
membres : les outils de la maison n'existent QUE dans la salle (et par `ask_house`, qui y repasse).

    from nokker.house.providers import register
    register("maison", lambda config, kernel: HomeToolSource(...) if config.home_assistant.get("enabled") else None)

Contrat d'une source (core.ports.ToolSource) : `specs()` stables (effet déclaré : la salle en tire le niveau de droit
read/act/strong), `call(name, arguments, ctx)` avec `ctx.sender` = le compte du MEMBRE qui parle (ses droits propres,
p. ex. la politique de la domotique, s'appliquent EN PLUS de ceux de la salle). Une source ne rend que du contenu
COMMUN au foyer (jamais le courrier ou l'agenda personnels d'un membre). Elle ne touche jamais le réseau du foyer
elle-même : elle passe par la passerelle home-bridge (opérations nommées, docs/LOKKAL-HOME-BRIDGE.md).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from nokker.house.roster import INTENDANT, SLUGS

logger = logging.getLogger("nokker.house")

Factory = Callable[[Any, Any], Any]  # (config, kernel) → ToolSource | None
_FACTORIES: dict[str, list[Factory]] = {}


def register(slug: str, factory: Factory) -> None:
    if slug not in SLUGS or slug == INTENDANT:
        raise ValueError(f"agent sans fournisseur d'outils : {slug!r}")
    _FACTORIES.setdefault(slug, []).append(factory)


def clear() -> None:
    _FACTORIES.clear()


def providers_for(config: Any, kernel: Any) -> dict[str, Any]:
    """slug → source (la première fabrique qui en rend une) ; une fabrique en échec est journalisée et sautée."""
    out: dict[str, Any] = {}
    for slug, factories in _FACTORIES.items():
        for f in factories:
            try:
                src = f(config, kernel)
            except Exception:
                logger.exception("salle Maison : fournisseur de %s en échec", slug)
                src = None
            if src is not None:
                out[slug] = src
                break
    return out


__all__ = ["clear", "providers_for", "register"]
