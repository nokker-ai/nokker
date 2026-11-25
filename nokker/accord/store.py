"""ACCORD — clés publiques des appareils appairés (table `accord_devices`, migration 092 ; mémoire pour les tests).

Révoquer garde la ligne (journal) et pose `revoked_at` : la clé n'est plus jamais acceptée, même ré-annoncée.
Nom donné par la personne (migration 094) : NULL = « ACCORD <key8> » ; dernière connexion au `ready` (hub)."""
from __future__ import annotations

import threading
import unicodedata
from dataclasses import dataclass, replace
from typing import Any

NAME_MAX = 40


def clean_name(raw: Any) -> str | None:
    """Nom d'appareil : NFC, espaces de bord retirés, au plus 40 caractères, aucun caractère de contrôle ni de format
    (Cc, Cf : retours, tabulations, marques bidi invisibles), ni séparateur de ligne. Vide → None (nom par défaut).
    Lève ValueError sinon : on refuse plutôt que de nettoyer en silence ce que la personne a tapé."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("bad_name")
    text = unicodedata.normalize("NFC", raw).strip()
    if not text:
        return None
    if len(text) > NAME_MAX or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in text):
        raise ValueError("bad_name")
    return text


@dataclass(frozen=True)
class Device:
    device_id: str
    pubkey: str
    key8: str
    host_fp: str
    paired_at: float
    revoked_at: float | None = None
    name: str | None = None
    last_seen_at: float | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    @property
    def display_name(self) -> str:
        return self.name or f"ACCORD {self.key8}"

    def to_json(self) -> dict[str, Any]:
        return {"device_id": self.device_id, "key8": self.key8, "name": self.display_name,
                "name_is_default": self.name is None, "paired_at": self.paired_at,
                "last_seen_at": self.last_seen_at, "revoked": not self.active, "revoked_at": self.revoked_at}


class MemoryAccordStore:
    def __init__(self) -> None:
        self._rows: dict[str, Device] = {}
        self._lock = threading.Lock()

    def add(self, device: Device) -> None:
        with self._lock:
            if any(d.active and d.pubkey == device.pubkey for d in self._rows.values()):
                raise ValueError("clé déjà appairée")
            self._rows[device.device_id] = device

    def get(self, device_id: str) -> Device | None:
        return self._rows.get(device_id)

    def by_pubkey(self, pubkey: str) -> Device | None:
        """L'appareil actif de cette clé, sinon le dernier révoqué (pour dire `revoked` plutôt que `unpaired`)."""
        rows = [d for d in self._rows.values() if d.pubkey == pubkey]
        return next((d for d in rows if d.active), None) or (max(rows, key=lambda d: d.paired_at) if rows else None)

    def list(self) -> list[Device]:
        return sorted(self._rows.values(), key=lambda d: d.paired_at)

    def revoke(self, device_id: str, now: float) -> bool:
        with self._lock:
            d = self._rows.get(device_id)
            if d is None or not d.active:
                return False
            self._rows[device_id] = replace(d, revoked_at=now)
            return True

    def rename(self, device_id: str, name: str | None) -> bool:
        with self._lock:
            d = self._rows.get(device_id)
            if d is None or not d.active:
                return False
            self._rows[device_id] = replace(d, name=name)
            return True

    def touch(self, device_id: str, now: float) -> None:
        with self._lock:
            d = self._rows.get(device_id)
            if d is not None and d.active:
                self._rows[device_id] = replace(d, last_seen_at=now)


class PgAccordStore:
    """Table `accord_devices` (092) ; la RLS du locataire courant fait la frontière (`nokker.store.conn`)."""

    _COLS = "device_id, pubkey, key8, host_fp, paired_at, revoked_at, name, last_seen_at"

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):
        from nokker.store.conn import connect

        return connect(self.dsn)

    def add(self, device: Device) -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO accord_devices (device_id, pubkey, key8, host_fp, paired_at) "
                         "VALUES (%s, %s, %s, %s, %s)",
                         (device.device_id, device.pubkey, device.key8, device.host_fp, device.paired_at))

    def get(self, device_id: str) -> Device | None:
        with self._conn() as conn:
            row = conn.execute(f"SELECT {self._COLS} FROM accord_devices WHERE device_id = %s",
                               (device_id,)).fetchone()
        return Device(*row) if row else None

    def by_pubkey(self, pubkey: str) -> Device | None:
        with self._conn() as conn:
            row = conn.execute(f"SELECT {self._COLS} FROM accord_devices WHERE pubkey = %s "
                               "ORDER BY (revoked_at IS NULL) DESC, paired_at DESC LIMIT 1", (pubkey,)).fetchone()
        return Device(*row) if row else None

    def list(self) -> list[Device]:
        with self._conn() as conn:
            rows = conn.execute(f"SELECT {self._COLS} FROM accord_devices ORDER BY paired_at").fetchall()
        return [Device(*r) for r in rows]

    def revoke(self, device_id: str, now: float) -> bool:
        with self._conn() as conn:
            return conn.execute("UPDATE accord_devices SET revoked_at = %s WHERE device_id = %s AND revoked_at IS NULL",
                                (now, device_id)).rowcount == 1

    def rename(self, device_id: str, name: str | None) -> bool:
        with self._conn() as conn:
            return conn.execute("UPDATE accord_devices SET name = %s WHERE device_id = %s AND revoked_at IS NULL",
                                (name, device_id)).rowcount == 1

    def touch(self, device_id: str, now: float) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE accord_devices SET last_seen_at = %s WHERE device_id = %s AND revoked_at IS NULL",
                         (now, device_id))
