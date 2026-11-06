"""Côté noyau de la maison (docs/HOME-ASSISTANT.md §2) : le noyau, fermé au LAN, ne parle jamais à Home Assistant.
Il appelle les opérations nommées `ha.*` de la passerelle du foyer par le client commun
`nokker.ambient.bridge.HomeBridge` (`POST <NOKKER_HOME_BRIDGE_URL>/v1/op/<nom>`, jeton `NOKKER_HOME_BRIDGE_TOKEN`).
Le client Home Assistant lui-même vit dans la passerelle (`nokker/homebridge/ha.py`) ; aucun jeton de HA ici.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any


class HomeError(RuntimeError):
    """Erreur de la maison vue du noyau ; jamais de jeton dans le message."""

    def __init__(self, message: str, code: str = "home_error") -> None:
        super().__init__(message)
        self.code = code


def _op(bridge: Any, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    from nokker.ambient.bridge import BridgeError

    try:
        return bridge.op(name, payload)
    except BridgeError as e:
        raise HomeError(str(e), "bridge") from None


class BridgeSession:
    """Interface lue par `nokker/home/source.py` (états, registres, appel de service, abonnement), servie par les
    opérations `ha.*` de la passerelle."""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge
        self._registry: dict[str, Any] | None = None
        self._cursor = -1

    def _reg(self) -> dict[str, Any]:
        if self._registry is None:
            self._registry = _op(self.bridge, "ha.registry", {})
        return self._registry

    def states(self) -> list[dict[str, Any]]:
        return list(_op(self.bridge, "ha.states", {}).get("states") or [])

    def areas(self) -> list[dict[str, Any]]:
        return list(self._reg().get("areas") or [])

    def device_registry(self) -> list[dict[str, Any]]:
        return list(self._reg().get("devices") or [])

    def entity_registry(self) -> list[dict[str, Any]]:
        return list(self._reg().get("entities") or [])

    def call_service(self, domain: str, service: str, entity_ids: list[str], data: dict[str, Any]) -> None:
        _op(self.bridge, "ha.call_service", {"domain": domain, "service": service, "entity_ids": list(entity_ids),
                                             "data": dict(data or {})})

    def query(self, domain: str, service: str, entity_ids: list[str], data: dict[str, Any]) -> dict[str, Any]:
        """Service à réponse (liste fermée de la passerelle : météo, agenda, liste, recherche de média)."""
        got = _op(self.bridge, "ha.query", {"domain": domain, "service": service, "entity_ids": list(entity_ids),
                                            "data": dict(data or {})})
        return dict(got.get("response") or {})

    def history(self, entity_ids: list[str], hours: float) -> dict[str, list[dict[str, Any]]]:
        return dict(_op(self.bridge, "ha.history", {"entity_ids": list(entity_ids), "hours": hours}).get("history")
                    or {})

    def logbook(self, entity_ids: list[str], hours: float) -> list[dict[str, Any]]:
        return list(_op(self.bridge, "ha.logbook", {"entity_ids": list(entity_ids), "hours": hours}).get("entries")
                    or [])

    def notify(self, target: str, message: str, title: str = "") -> None:
        _op(self.bridge, "ha.notify", {"target": target, "message": message, "title": title})

    def subscribe_state_changed(self) -> int:
        self._cursor = int(_op(self.bridge, "ha.events", {"after": -1, "timeout": 0}).get("cursor") or 0)
        return self._cursor

    def events(self, timeout_s: float) -> Iterator[dict[str, Any]]:
        # long-poll plus court que le délai du client HTTP de la passerelle
        wait = min(timeout_s, max(1.0, float(getattr(self.bridge, "timeout_s", 10.0)) - 2.0))
        got = _op(self.bridge, "ha.events", {"after": self._cursor, "timeout": wait})
        evs = list(got.get("events") or [])
        if evs:
            self._cursor = max(int(e.get("seq") or 0) for e in evs)
        for e in evs:
            yield {"type": "event", "event": e.get("event") or {}}

    def close(self) -> None:
        return None


class BridgeHome:
    """Ce que la source maison attend d'un client : `session()` et `write_config()`."""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge

    def __repr__(self) -> str:
        return f"BridgeHome(url={getattr(self.bridge, 'url', '?')!r})"

    def session(self) -> BridgeSession:
        return BridgeSession(self.bridge)

    def write_config(self, kind: str, config_id: str, config: dict[str, Any]) -> None:
        _op(self.bridge, "ha.write_config", {"type": kind, "id": config_id, "config": config})


__all__ = ["BridgeHome", "BridgeSession", "HomeError"]
