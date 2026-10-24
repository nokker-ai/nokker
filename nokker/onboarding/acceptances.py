"""Preuve d'acceptation des conditions (table `terms_acceptances`, migration 005) : ajout seul."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol


class Acceptances(Protocol):
    def record(self, account_id: str, invitation_id: str, terms_sha256: str, lang: str, ip: str | None) -> None: ...

    def for_account(self, account_id: str) -> list[dict]: ...


@dataclass
class MemoryAcceptances:
    rows: list[dict] = field(default_factory=list)

    def record(self, account_id: str, invitation_id: str, terms_sha256: str, lang: str, ip: str | None) -> None:
        self.rows.append({"account_id": account_id, "invitation_id": invitation_id,
                          "terms_version_sha256": terms_sha256, "lang": lang, "ip": ip, "accepted_at": time.time()})

    def for_account(self, account_id: str) -> list[dict]:
        return [r for r in self.rows if r["account_id"] == account_id]


class PgAcceptances:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):
        from psycopg.rows import dict_row

        from nokker.store.conn import connect

        return connect(self.dsn, row_factory=dict_row)  # table sous RLS : locataire du compte (docs/TENANCY.md §4)

    def record(self, account_id: str, invitation_id: str, terms_sha256: str, lang: str, ip: str | None) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO terms_acceptances (account_id, invitation_id, terms_version_sha256, lang, ip) "
                        "VALUES (%s,%s,%s,%s,%s)", (account_id, invitation_id, terms_sha256, lang, ip))

    def for_account(self, account_id: str) -> list[dict]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT account_id, invitation_id, terms_version_sha256, lang, ip, accepted_at "
                        "FROM terms_acceptances WHERE account_id = %s ORDER BY id", (account_id,))
            return list(cur.fetchall())
