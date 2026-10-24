"""Consentements du programme bêta (table `data_consents`, migration 012) : ajout seul, l'état courant est la
dernière ligne par (compte, portée). Jamais pré-coché : sans ligne, le consentement est refusé."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

SCOPES = ("training",)
SOURCES = ("signup", "account")


class Consents(Protocol):
    def record(self, account_id: str, scope: str, granted: bool, source: str, terms_sha256: str | None,
               ip: str | None) -> None: ...

    def current(self, account_id: str, scope: str) -> bool: ...

    def history(self, account_id: str) -> list[dict]: ...


@dataclass
class MemoryConsents:
    rows: list[dict] = field(default_factory=list)

    def record(self, account_id: str, scope: str, granted: bool, source: str, terms_sha256: str | None,
               ip: str | None) -> None:
        assert scope in SCOPES and source in SOURCES
        self.rows.append({"account_id": account_id, "scope": scope, "granted": bool(granted), "source": source,
                          "terms_sha256": terms_sha256, "ip": ip, "at": time.time()})

    def current(self, account_id: str, scope: str) -> bool:
        mine = [r for r in self.rows if r["account_id"] == account_id and r["scope"] == scope]
        return bool(mine and mine[-1]["granted"])

    def history(self, account_id: str) -> list[dict]:
        return [r for r in self.rows if r["account_id"] == account_id]


class PgConsents:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):
        from psycopg.rows import dict_row

        from nokker.store.conn import connect

        return connect(self.dsn, row_factory=dict_row)  # table sous RLS : locataire du compte (docs/TENANCY.md §4)

    def record(self, account_id: str, scope: str, granted: bool, source: str, terms_sha256: str | None,
               ip: str | None) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO data_consents (account_id, scope, granted, source, terms_sha256, ip) "
                        "VALUES (%s,%s,%s,%s,%s,%s)", (account_id, scope, bool(granted), source, terms_sha256, ip))

    def current(self, account_id: str, scope: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT granted FROM data_consents WHERE account_id = %s AND scope = %s "
                        "ORDER BY id DESC LIMIT 1", (account_id, scope))
            row = cur.fetchone()
            return bool(row and row["granted"])

    def history(self, account_id: str) -> list[dict]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT account_id, scope, granted, source, terms_sha256, ip, at FROM data_consents "
                        "WHERE account_id = %s ORDER BY id", (account_id,))
            return list(cur.fetchall())
