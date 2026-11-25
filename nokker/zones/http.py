"""Routes des zones de l'app (docs/LOKKAL-PHONE.md §4.2), même contrat dans le noyau cloud et dans Lokkal :

    GET  /v1/me/zones                    → 200 {zones: [{id, name, on_enter, on_exit}]}
    PUT  /v1/me/zones {zones: [...]}     → 200 {zones} | 400 bad_request | bad_zone | too_many | unknown_scenario
    POST /v1/me/zones/{id}/event {event: "enter"|"exit", at, event_id}
                                         → 202 {queued: true, scenarios: [{id, status: started|card|skipped, reason?}]}
                                           | 200 {duplicate: true, …} | 404 zone_unknown | 409 out_of_order | 400
    GET  /v1/home/scenarios              → 200 {scenarios: [{id, name, strong}], bridge: "home-bridge"|"home-node"|null}

Phase S : session de l'app (compte `us_…` au cloud, propriétaire sur Lokkal). Le jeton du nœud téléphone vient en P7.
`configure(None)` : routes absentes (404), comme avant.
"""
from __future__ import annotations

import re
from typing import Any

from nokker.zones.store import ZoneError

_svc: Any = None
_EVENT = re.compile(r"^/v1/me/zones/([A-Za-z0-9_-]{1,40})/event$")


def configure(service: Any) -> None:
    global _svc
    _svc = service


def service() -> Any:
    return _svc


def is_zones_path(path: str) -> bool:
    route = path.split("?", 1)[0].rstrip("/")
    return route in ("/v1/me/zones", "/v1/home/scenarios") or route.startswith("/v1/me/zones/")


def handle(h: Any, method: str, path: str, member: str) -> None:
    svc = _svc
    route = path.split("?", 1)[0].rstrip("/")
    if svc is None:
        h._json(404, {"error": {"message": "not found"}})
        return
    try:
        if route == "/v1/me/zones" and method == "GET":
            h._json(200, svc.get(member))
        elif route == "/v1/me/zones" and method == "PUT":
            h._json(200, svc.put(member, h._read_json_body()))
        elif route == "/v1/home/scenarios" and method == "GET":
            h._json(200, svc.list_scenarios(member))
        elif (m := _EVENT.match(route)) and method == "POST":
            h._json(*svc.event(member, m.group(1), h._read_json_body()))
        else:
            h._json(404, {"error": {"message": "not found"}})
    except ZoneError as e:
        h._json(e.status, {"error": e.code, "message": str(e)})


__all__ = ["configure", "handle", "is_zones_path", "service"]
