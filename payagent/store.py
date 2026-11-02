"""Stockage de l'agent de paiement : moyens enregistrés, jetons émis, événements Stripe déjà vus.

Tables `pay_agent_*` (migration nokker/store/migrations/063_pay_agent.sql) ; le rôle de l'agent y écrit, le rôle du
noyau n'y a aucun droit (docs/PAY-AGENT.md §6). Clés des passkeys : table `devices`, en LECTURE SEULE (`Devices`).
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, replace
from typing import Any


@dataclass(frozen=True)
class Method:
    account: str
    payment_method: str          # pm_… préparée par le Payment Element (aucun numéro de carte chez nous)
    kind: str                    # card, link…
    brand: str | None
    last4: str | None
    exp_month: int | None
    exp_year: int | None
    device_id: str               # appareil dont la passkey a signé l'enregistrement
    created_at: float

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "brand": self.brand, "last4": self.last4, "exp_month": self.exp_month,
                "exp_year": self.exp_year, "created_at": self.created_at}


@dataclass(frozen=True)
class Token:
    id: str                      # tk_… (le nôtre)
    mandate_id: str
    mandate_sha256: str
    account: str
    spt_id: str
    status: str                  # active | requires_action | deactivated
    seller_profile: str
    currency: str
    max_amount_minor: int
    expires_at: int
    created_at: float
    updated_at: float
    used: bool = False
    deactivated_reason: str | None = None
    kernel_ref: str = ""         # rendu tel quel dans les rappels au noyau ; aucune décision n'en dépend

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("account", "kernel_ref", "spt_id"):  # le numéro du SPT ne sort pas de l'agent
            d.pop(k)
        return d


@dataclass(frozen=True)
class DeviceKey:
    id: str
    user: str
    public_key_spki: str
    status: str


class MemoryStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.methods: dict[str, Method] = {}
        self.tokens: dict[str, Token] = {}
        self.events: set[str] = set()

    def put_method(self, m: Method) -> None:
        with self._lock:
            self.methods[m.account] = m

    def get_method(self, account: str) -> Method | None:
        with self._lock:
            return self.methods.get(account)

    def delete_method(self, account: str) -> bool:
        with self._lock:
            return self.methods.pop(account, None) is not None

    def insert_token(self, t: Token) -> bool:
        """Faux si un jeton existe déjà pour ce mandat (un seul jeton par mandat)."""
        with self._lock:
            if any(x.mandate_id == t.mandate_id for x in self.tokens.values()):
                return False
            self.tokens[t.id] = t
            return True

    def update_token(self, t: Token) -> None:
        with self._lock:
            self.tokens[t.id] = t

    def token_by_mandate(self, mandate_id: str) -> Token | None:
        with self._lock:
            return next((t for t in self.tokens.values() if t.mandate_id == mandate_id), None)

    def token_by_spt(self, spt_id: str) -> Token | None:
        with self._lock:
            return next((t for t in self.tokens.values() if t.spt_id == spt_id), None)

    def live_tokens(self, account: str) -> list[Token]:
        with self._lock:
            return [t for t in self.tokens.values() if t.account == account and t.status != "deactivated"]

    def first_event(self, event_id: str) -> bool:
        with self._lock:
            if event_id in self.events:
                return False
            self.events.add(event_id)
            return True


class MemoryDevices:
    def __init__(self) -> None:
        self.keys: dict[str, DeviceKey] = {}

    def get(self, device_id: str) -> DeviceKey | None:
        return self.keys.get(device_id)


class PgStore:  # pragma: no cover - couvert par tests/integration/test_pay_agent_pg.py
    _TOKEN_COLS = ("id, mandate_id, mandate_sha256, account, spt_id, status, seller_profile, currency, "
                   "max_amount_minor, expires_at, created_at, updated_at, used, deactivated_reason, kernel_ref")

    def __init__(self, dsn: str) -> None:
        import psycopg

        self._conn = psycopg.connect(dsn, autocommit=True)
        self._lock = threading.Lock()

    def _one(self, sql: str, args: tuple) -> tuple | None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone()

    def _all(self, sql: str, args: tuple) -> list[tuple]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()

    def _exec(self, sql: str, args: tuple) -> int:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.rowcount

    def put_method(self, m: Method) -> None:
        self._exec("INSERT INTO pay_agent_methods (account, payment_method, kind, brand, last4, exp_month, exp_year, "
                   "device_id, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (account) DO UPDATE SET "
                   "payment_method=EXCLUDED.payment_method, kind=EXCLUDED.kind, brand=EXCLUDED.brand, "
                   "last4=EXCLUDED.last4, exp_month=EXCLUDED.exp_month, exp_year=EXCLUDED.exp_year, "
                   "device_id=EXCLUDED.device_id, created_at=EXCLUDED.created_at",
                   (m.account, m.payment_method, m.kind, m.brand, m.last4, m.exp_month, m.exp_year, m.device_id,
                    m.created_at))

    def get_method(self, account: str) -> Method | None:
        row = self._one("SELECT account, payment_method, kind, brand, last4, exp_month, exp_year, device_id, created_at "
                        "FROM pay_agent_methods WHERE account = %s", (account,))
        return Method(*row) if row else None

    def delete_method(self, account: str) -> bool:
        return self._exec("DELETE FROM pay_agent_methods WHERE account = %s", (account,)) > 0

    def insert_token(self, t: Token) -> bool:
        return self._exec(f"INSERT INTO pay_agent_tokens ({self._TOKEN_COLS}) VALUES "
                          "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (mandate_id) DO NOTHING",
                          tuple(asdict(t).values())) > 0

    def update_token(self, t: Token) -> None:
        self._exec("UPDATE pay_agent_tokens SET status=%s, used=%s, deactivated_reason=%s, updated_at=%s WHERE id=%s",
                   (t.status, t.used, t.deactivated_reason, t.updated_at, t.id))

    def token_by_mandate(self, mandate_id: str) -> Token | None:
        row = self._one(f"SELECT {self._TOKEN_COLS} FROM pay_agent_tokens WHERE mandate_id = %s", (mandate_id,))
        return Token(*row) if row else None

    def token_by_spt(self, spt_id: str) -> Token | None:
        row = self._one(f"SELECT {self._TOKEN_COLS} FROM pay_agent_tokens WHERE spt_id = %s", (spt_id,))
        return Token(*row) if row else None

    def live_tokens(self, account: str) -> list[Token]:
        return [Token(*r) for r in self._all(f"SELECT {self._TOKEN_COLS} FROM pay_agent_tokens WHERE account = %s "
                                              "AND status <> 'deactivated'", (account,))]

    def first_event(self, event_id: str) -> bool:
        return self._exec("INSERT INTO pay_agent_events (id) VALUES (%s) ON CONFLICT DO NOTHING", (event_id,)) > 0


class PgDevices:  # pragma: no cover
    """Clés des passkeys, lues avec un rôle en LECTURE SEULE sur `devices` (le noyau ne les fournit pas)."""

    def __init__(self, dsn: str) -> None:
        import psycopg

        self._conn = psycopg.connect(dsn, autocommit=True)
        self._lock = threading.Lock()

    def get(self, device_id: str) -> DeviceKey | None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute('SELECT id, "user", public_key_spki, status FROM devices WHERE id = %s', (device_id,))
            row = cur.fetchone()
        return DeviceKey(*row) if row else None


__all__ = ["DeviceKey", "MemoryDevices", "MemoryStore", "Method", "PgDevices", "PgStore", "Token", "replace"]
