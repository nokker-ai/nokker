"""Appareils de l'app et leurs capacités (P8, docs/LOKKAL-PHONE.md §4.3) : `PUT /v1/me/devices/{id}/caps`.

Un appareil déclare ce qu'il sait LIRE pour son membre : `calendar.read`, `contacts.read`, `reminders.read`. Le noyau
garde son id, son nom (saisi par la personne, neutralisé : `qa/untrusted.inline`, 40 caractères), sa plateforme et ces
capacités ; jamais une donnée lue.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any

CAPS = ("calendar.read", "contacts.read", "reminders.read")
PLATFORMS = ("ios", "android", "web")
NAME_MAX = 40
MAX_DEVICES = 5  # par membre
_DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class DeviceError(ValueError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code = status, code


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    platform: str
    caps: tuple[str, ...] = ()

    def json(self) -> dict[str, Any]:
        return {"device_id": self.id, "name": self.name, "platform": self.platform, "caps": list(self.caps)}


def device_name(value: Any) -> str:
    """Nom saisi par la personne, tel qu'il entre dans un texte vu par le modèle : `inline()` (ni balise, ni « [/…] »,
    une ligne) PUIS 40 caractères au plus."""
    from nokker.qa.untrusted import inline

    return " ".join(inline(value if isinstance(value, str) else "").split())[:NAME_MAX]


def valid_device_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_DEVICE_ID.match(value))


def clean_caps(device_id: str, body: Any) -> Device:
    if not valid_device_id(device_id):
        raise DeviceError(400, "bad_device", "device_id : 8 à 64 caractères [A-Za-z0-9_-]")
    if not isinstance(body, dict) or set(body) - {"name", "platform", "caps"}:
        raise DeviceError(400, "bad_request", "corps attendu : {name, platform, caps}")
    name = device_name(body.get("name"))
    if not name:
        raise DeviceError(400, "bad_device", "name : 1 à 40 caractères")
    platform = body.get("platform")
    if platform not in PLATFORMS:
        raise DeviceError(400, "bad_device", f"platform : {' | '.join(PLATFORMS)}")
    caps = body.get("caps")
    if not isinstance(caps, list) or any(c not in CAPS for c in caps):
        raise DeviceError(400, "bad_caps", f"caps : sous-ensemble de {', '.join(CAPS)}")
    return Device(id=device_id, name=name, platform=str(platform), caps=tuple(c for c in CAPS if c in caps))


@dataclass
class MemoryDeviceStore:
    _rows: dict[tuple[str, str], dict[str, Device]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def _key(account: str) -> tuple[str, str]:
        from nokker.tenancy.context import current_tenant

        return current_tenant(), account

    def list(self, account: str) -> list[Device]:
        with self._lock:
            return sorted(self._rows.get(self._key(account), {}).values(), key=lambda d: (d.name, d.id))

    def get(self, account: str, device_id: str) -> Device | None:
        with self._lock:
            return self._rows.get(self._key(account), {}).get(device_id)

    def put(self, account: str, device: Device) -> Device:
        with self._lock:
            mine = self._rows.setdefault(self._key(account), {})
            if device.id not in mine and len(mine) >= MAX_DEVICES:
                raise DeviceError(409, "too_many", f"{MAX_DEVICES} appareils au plus")
            mine[device.id] = device
            return device

    def delete(self, account: str, device_id: str) -> bool:
        with self._lock:
            return self._rows.get(self._key(account), {}).pop(device_id, None) is not None


class PgDeviceStore:
    """Postgres (migration 076), connexion `nokker_app` sous RLS (locataire courant)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):
        from psycopg.rows import dict_row

        from nokker.store.conn import connect

        return connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _dev(r: dict[str, Any]) -> Device:
        return Device(id=r["device_id"], name=r["name"], platform=r["platform"], caps=tuple(r["caps"] or ()))

    def list(self, account: str) -> list[Device]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM member_devices WHERE account_id = %s ORDER BY name, device_id",
                             (account,)).fetchall()
        return [self._dev(r) for r in rows]

    def get(self, account: str, device_id: str) -> Device | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM member_devices WHERE account_id = %s AND device_id = %s",
                          (account, device_id)).fetchone()
        return self._dev(r) if r else None

    def put(self, account: str, device: Device) -> Device:
        with self._conn() as c, c.transaction():
            n = c.execute("SELECT count(*) AS n FROM member_devices WHERE account_id = %s AND device_id <> %s",
                          (account, device.id)).fetchone()["n"]
            if n >= MAX_DEVICES:
                raise DeviceError(409, "too_many", f"{MAX_DEVICES} appareils au plus")
            c.execute("INSERT INTO member_devices (account_id, device_id, name, platform, caps) VALUES (%s,%s,%s,%s,%s) "
                      "ON CONFLICT (account_id, device_id) DO UPDATE SET name = EXCLUDED.name, "
                      "platform = EXCLUDED.platform, caps = EXCLUDED.caps, updated_at = now()",
                      (account, device.id, device.name, device.platform, list(device.caps)))
        return device

    def delete(self, account: str, device_id: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM member_devices WHERE account_id = %s AND device_id = %s RETURNING device_id",
                             (account, device_id)).fetchone() is not None


__all__ = ["CAPS", "MAX_DEVICES", "NAME_MAX", "PLATFORMS", "Device", "DeviceError", "MemoryDeviceStore",
           "PgDeviceStore", "clean_caps", "device_name", "valid_device_id"]
