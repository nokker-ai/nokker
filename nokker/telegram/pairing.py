"""Codes d'appairage Telegram (contrat C4) : émis pour un compte par l'app web, consommés une fois par `/start <code>`.
On ne garde que le SHA-256 du code ; 10 minutes de validité."""
from __future__ import annotations

import hashlib
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

TTL = timedelta(minutes=10)
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sans 0/O/1/I : se tape sans ambiguïté


def _sha(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def new_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(8))


class PairingStore(Protocol):
    def issue(self, account_id: str) -> tuple[str, datetime]: ...

    def redeem(self, code: str) -> str | None: ...


@dataclass
class MemoryPairing:
    now: object = None
    _codes: dict[str, tuple[str, datetime, bool]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _now(self) -> datetime:
        return self.now() if callable(self.now) else datetime.now(timezone.utc)

    def issue(self, account_id: str) -> tuple[str, datetime]:
        code, exp = new_code(), self._now() + TTL
        with self._lock:
            self._codes[_sha(code)] = (account_id, exp, False)
        return code, exp

    def redeem(self, code: str) -> str | None:
        with self._lock:
            entry = self._codes.get(_sha(code))
            if entry is None or entry[2] or entry[1] < self._now():
                return None
            self._codes[_sha(code)] = (entry[0], entry[1], True)
            return entry[0]


class PgPairing:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):
        import psycopg

        return psycopg.connect(self.dsn)

    def issue(self, account_id: str) -> tuple[str, datetime]:
        code, exp = new_code(), datetime.now(timezone.utc) + TTL
        with self._conn() as conn:
            conn.execute("INSERT INTO telegram_pairing (code_sha256, account_id, expires_at) VALUES (%s,%s,%s)",
                         (_sha(code), account_id, exp))
        return code, exp

    def redeem(self, code: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("UPDATE telegram_pairing SET used_at=now() WHERE code_sha256=%s AND used_at IS NULL "
                               "AND expires_at > now() RETURNING account_id", (_sha(code),)).fetchone()
        return row[0] if row else None


__all__ = ["MemoryPairing", "PairingStore", "PgPairing", "TTL", "new_code"]
