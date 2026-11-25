"""Les zones de l'app alimentent la présence ambiante (docs/AMBIENT.md §2.2, `PresenceSource`) : une passerelle du
foyer enveloppée dont `presence()` ajoute `away` (membres sortis de leur zone « Maison »). Tout le reste passe tel quel
à la passerelle. Le service des zones est relu à chaque appel (`nokker/zones/http.py::service`) : branché après
l'ambiant, ou éteint, sans ordre imposé."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from nokker.ambient.presence import Presence


class ZonePresenceBridge:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def presence(self) -> Presence:
        from nokker.zones import http as zones_http

        p = self._inner.presence()
        svc = zones_http.service()
        if svc is None or not isinstance(p, Presence) or not p.ok:
            return p
        located = {m for st in p.rooms.values() for m in st.members}
        return replace(p, away=frozenset(svc.away()) - located)


def wrap(bridge: Any) -> Any:
    return ZonePresenceBridge(bridge) if bridge is not None else None


__all__ = ["ZonePresenceBridge", "wrap"]
