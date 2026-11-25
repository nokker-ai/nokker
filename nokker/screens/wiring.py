"""Câblage des écrans du mur (lot V7) : drapeau `ambient.screens.enabled` (éteint par défaut), qui exige
`ambient.glance.enabled`. Lokkal seulement pour l'instant (outil du locataire interne) ; au cloud, plus tard, par le
registre par locataire. Éteint : aucun outil, aucune bulle, aucun champ `screen` dans glance."""
from __future__ import annotations

import logging
from typing import Any

from nokker.screens.service import ScreenService

logger = logging.getLogger("nokker.screens")
_SERVICE: ScreenService | None = None


def configure(ambient: dict[str, Any] | None, dsn: str | None = None, *, local: bool = False) -> ScreenService | None:
    global _SERVICE
    raw = ambient or {}
    conf = raw.get("screens") or {}
    if not conf.get("enabled"):
        _SERVICE = None
        return None
    if not (raw.get("glance") or {}).get("enabled"):
        logger.warning("ambient.screens allumé sans ambient.glance : écrans du mur éteints (aucun mur pour les lire)")
        _SERVICE = None
        return None
    from nokker.screens.store import MemoryScreenStore, PgScreenStore

    _SERVICE = ScreenService(store=PgScreenStore(dsn) if dsn else MemoryScreenStore(),
                             tz=str(conf.get("tz") or "Europe/Zurich"), local=local)
    return _SERVICE


def service() -> ScreenService | None:
    return _SERVICE


__all__ = ["configure", "service"]
