"""Identifiants de connecteurs par compte (contrat C3). Le secret est chiffré (Fernet) avant d'atteindre la base ; les
`settings` (hôtes, ports, adresse, URL) sont en clair et ne contiennent jamais de secret. Un connecteur par (compte, nature)."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from nokker.connectors.crypto import SecretBox
from nokker.connectors.errors import ConnectorError

KINDS = ("mail", "calendar")


@dataclass(frozen=True)
class Status:
    """Ce que montre la carte Connecteurs : jamais le secret."""

    account_id: str
    kind: str
    settings: dict[str, Any]
    created_at: datetime
    last_ok_at: datetime | None


@dataclass(frozen=True)
class Credential(Status):
    secret: str = ""

    def __repr__(self) -> str:  # le secret ne sort ni dans un log ni dans une trace
        return f"Credential({self.account_id!r}, {self.kind!r}, secret=***)"


class ConnectorStore(Protocol):
    def put(self, account_id: str, kind: str, settings: dict[str, Any], secret: str) -> Status: ...

    def get(self, account_id: str, kind: str) -> Credential | None: ...

    def statuses(self, account_id: str) -> list[Status]: ...

    def delete(self, account_id: str, kind: str) -> bool: ...

    def mark_ok(self, account_id: str, kind: str) -> None: ...


def _check(kind: str, settings: dict[str, Any], secret: str) -> None:
    if kind not in KINDS:
        raise ConnectorError("bad_settings", f"nature inconnue : {kind}")
    if not isinstance(settings, dict) or not secret:
        raise ConnectorError("bad_settings", "réglages et secret requis")
    if any(k in settings for k in ("password", "secret", "token", "app_password")):
        raise ConnectorError("bad_settings", "un secret ne va jamais dans les réglages")
    json.dumps(settings)


class MemoryConnectorStore:
    def __init__(self, box: SecretBox) -> None:
        self._box = box
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}

    def put(self, account_id: str, kind: str, settings: dict[str, Any], secret: str) -> Status:
        _check(kind, settings, secret)
        row = {"settings": json.loads(json.dumps(settings)), "sealed": self._box.seal(secret),
               "created_at": datetime.now(UTC), "last_ok_at": None}
        self._rows[(account_id, kind)] = row
        return Status(account_id, kind, row["settings"], row["created_at"], None)

    def get(self, account_id: str, kind: str) -> Credential | None:
        row = self._rows.get((account_id, kind))
        if row is None:
            return None
        return Credential(account_id, kind, dict(row["settings"]), row["created_at"], row["last_ok_at"],
                          self._box.open(row["sealed"]))

    def statuses(self, account_id: str) -> list[Status]:
        return [Status(a, k, dict(r["settings"]), r["created_at"], r["last_ok_at"])
                for (a, k), r in sorted(self._rows.items()) if a == account_id]

    def delete(self, account_id: str, kind: str) -> bool:
        return self._rows.pop((account_id, kind), None) is not None

    def mark_ok(self, account_id: str, kind: str) -> None:
        if (account_id, kind) in self._rows:
            self._rows[(account_id, kind)]["last_ok_at"] = datetime.now(UTC)


class PgConnectorStore:
    """Table `connector_credentials` (migration 005). `connect` rend une connexion psycopg (dict_row) : en bêta, celle du
    noyau qui pose `app.tenant_id` (RLS) ; par défaut aussi (`nokker/store/conn.py::connect`)."""

    def __init__(self, dsn: str, box: SecretBox, connect: Callable[[], Any] | None = None) -> None:
        self.dsn, self._box, self._connect = dsn, box, connect

    def _conn(self):
        if self._connect:
            return self._connect()
        from psycopg.rows import dict_row

        from nokker.store.conn import connect

        return connect(self.dsn, row_factory=dict_row)  # table sous RLS : locataire courant (docs/TENANCY.md §4)

    def put(self, account_id: str, kind: str, settings: dict[str, Any], secret: str) -> Status:
        _check(kind, settings, secret)
        from psycopg.types.json import Jsonb

        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO connector_credentials (account_id, kind, settings, secret_enc) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (account_id, kind) DO UPDATE SET settings = EXCLUDED.settings, "
                "secret_enc = EXCLUDED.secret_enc, created_at = now(), last_ok_at = NULL "
                "RETURNING created_at", (account_id, kind, Jsonb(settings), self._box.seal(secret)))
            created = cur.fetchone()["created_at"]
        return Status(account_id, kind, settings, created, None)

    def get(self, account_id: str, kind: str) -> Credential | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT settings, secret_enc, created_at, last_ok_at FROM connector_credentials "
                        "WHERE account_id = %s AND kind = %s", (account_id, kind))
            r = cur.fetchone()
        if r is None:
            return None
        return Credential(account_id, kind, r["settings"], r["created_at"], r["last_ok_at"], self._box.open(r["secret_enc"]))

    def statuses(self, account_id: str) -> list[Status]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT kind, settings, created_at, last_ok_at FROM connector_credentials "
                        "WHERE account_id = %s ORDER BY kind", (account_id,))
            return [Status(account_id, r["kind"], r["settings"], r["created_at"], r["last_ok_at"]) for r in cur.fetchall()]

    def delete(self, account_id: str, kind: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM connector_credentials WHERE account_id = %s AND kind = %s", (account_id, kind))
            return cur.rowcount > 0

    def mark_ok(self, account_id: str, kind: str) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE connector_credentials SET last_ok_at = now() WHERE account_id = %s AND kind = %s",
                        (account_id, kind))
