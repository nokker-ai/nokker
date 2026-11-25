"""Routes des appareils de l'app (P8), même contrat dans le noyau cloud et dans Lokkal :

    GET    /v1/me/devices                          → 200 {devices: [{device_id, name, platform, caps}]}
    GET    /v1/me/devices/{device_id}/caps         → 200 {device_id, name, platform, caps} | 404
    PUT    /v1/me/devices/{device_id}/caps {name, platform, caps: ["calendar.read", "contacts.read", "reminders.read"]}
                                                   → 200 même corps | 400 | 409 too_many (5 appareils au plus)
    DELETE /v1/me/devices/{device_id}/caps         → 200 {deleted: bool}
    POST   /v1/me/device-requests/{request_id}/result {device_id, items | error: denied|unavailable|too_large}
                                                   → 204 | 409 already_answered | 410 expired | 403 not_yours | 400

Les demandes (`device.request {request_id, op, args, devices, expires_at}`) partent sur le flux `/v1/events` des
sessions du membre. Session de l'app : compte `us_…` au cloud, propriétaire sur Lokkal. `configure(None)` : 404.
"""
from __future__ import annotations

import re
from typing import Any

from nokker.devices.store import DeviceError, clean_caps

_broker: Any = None
_CAPS = re.compile(r"^/v1/me/devices/([A-Za-z0-9_-]{8,64})/caps$")
_RESULT = re.compile(r"^/v1/me/device-requests/(dr_[0-9a-f]{20})/result$")


def configure(broker: Any) -> None:
    global _broker
    _broker = broker


def broker() -> Any:
    return _broker


def is_devices_path(path: str) -> bool:
    route = path.split("?", 1)[0].rstrip("/")
    return route == "/v1/me/devices" or route.startswith(("/v1/me/devices/", "/v1/me/device-requests/"))


def handle(h: Any, method: str, path: str, member: str) -> None:
    b = _broker
    route = path.split("?", 1)[0].rstrip("/")
    if b is None:
        h._json(404, {"error": {"message": "not found"}})
        return
    store = b.devices
    try:
        if route == "/v1/me/devices" and method == "GET":
            h._json(200, {"devices": [d.json() for d in store.list(member)]})
            return
        m = _CAPS.match(route)
        if m and method == "GET":
            d = store.get(member, m.group(1))
            h._json(200, d.json()) if d is not None else h._json(404, {"error": "not_found"})
            return
        if m and method == "PUT":
            h._json(200, store.put(member, clean_caps(m.group(1), h._read_json_body())).json())
            return
        if m and method == "DELETE":
            h._json(200, {"deleted": store.delete(member, m.group(1))})
            return
        r = _RESULT.match(route)
        if r and method == "POST":
            status, body = b.answer(member, r.group(1), h._read_json_body())
            if body is None:
                h.send_response(status)
                h.send_header("Content-Length", "0")
                h.end_headers()
            else:
                h._json(status, body)
            return
        if route.startswith("/v1/me/device-requests/") and method == "POST":
            h._json(403, {"error": "not_yours"})  # id hors forme : jamais une demande de ce membre
            return
        h._json(404, {"error": {"message": "not found"}})
    except DeviceError as e:
        h._json(e.status, {"error": e.code, "message": str(e)})


__all__ = ["broker", "configure", "handle", "is_devices_path"]
