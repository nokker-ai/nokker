"""Magasin Postgres du lien Lokkal (tables de la migration 014), même interface que `MemoryLinkStore`."""
from __future__ import annotations

import time

import psycopg

from nokker.cloud.link import Machine, _Request


class PgLinkStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _q(self, sql: str, params: tuple = ()) -> list[tuple]:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    @staticmethod
    def _req(r: tuple) -> _Request:
        return _Request(device_sha=r[0], user_code=r[1], machine_name=r[2], expires_at=r[3], state=r[4],
                        machine_id=r[5])

    @staticmethod
    def _mac(r: tuple) -> Machine:
        return Machine(id=r[0], account=r[1], tenant=r[2], name=r[3], host=r[4], token_sha256=r[5], created_at=r[6],
                       revoked=r[7] is not None)

    _RCOLS = "device_sha256, user_code, machine_name, expires_at, state, machine_id"
    _MCOLS = "id, account_id, tenant_id, name, host, token_sha256, created_at, revoked_at"

    def add_request(self, r: _Request) -> None:
        self._q(f"INSERT INTO lokkal_link_requests ({self._RCOLS}) VALUES (%s, %s, %s, %s, %s, %s)",
                (r.device_sha, r.user_code, r.machine_name, r.expires_at, r.state, r.machine_id))

    def request(self, device_sha: str) -> _Request | None:
        rows = self._q(f"SELECT {self._RCOLS} FROM lokkal_link_requests WHERE device_sha256 = %s", (device_sha,))
        return self._req(rows[0]) if rows else None

    def pending_by_code(self, user_code: str) -> _Request | None:
        rows = self._q(f"SELECT {self._RCOLS} FROM lokkal_link_requests WHERE user_code = %s AND state = 'pending'",
                       (user_code,))
        return self._req(rows[0]) if rows else None

    def save_request(self, r: _Request) -> None:
        self._q("UPDATE lokkal_link_requests SET state = %s, machine_id = %s WHERE device_sha256 = %s",
                (r.state, r.machine_id, r.device_sha))

    def drop_request(self, device_sha: str) -> None:
        self._q("DELETE FROM lokkal_link_requests WHERE device_sha256 = %s OR expires_at < %s",
                (device_sha, time.time() - 3600))  # et les flux échus depuis plus d'une heure

    def hosts(self) -> set[str]:
        return {r[0] for r in self._q("SELECT host FROM lokkal_machines")}

    def add_machine(self, m: Machine) -> None:
        self._q(f"INSERT INTO lokkal_machines ({self._MCOLS}) VALUES (%s, %s, %s, %s, %s, %s, %s, NULL)",
                (m.id, m.account, m.tenant, m.name, m.host, m.token_sha256, m.created_at))

    def machine(self, machine_id: str) -> Machine | None:
        rows = self._q(f"SELECT {self._MCOLS} FROM lokkal_machines WHERE id = %s", (machine_id,))
        return self._mac(rows[0]) if rows else None

    def set_token(self, machine_id: str, token_sha: str) -> None:
        self._q("UPDATE lokkal_machines SET token_sha256 = %s WHERE id = %s", (token_sha, machine_id))

    def by_token(self, token_sha: str) -> Machine | None:
        rows = self._q(f"SELECT {self._MCOLS} FROM lokkal_machines WHERE token_sha256 = %s AND revoked_at IS NULL",
                       (token_sha,))
        return self._mac(rows[0]) if rows else None

    def revoke(self, machine_id: str) -> None:
        self._q("UPDATE lokkal_machines SET revoked_at = %s WHERE id = %s", (time.time(), machine_id))

    def of_account(self, account: str) -> list[Machine]:
        return [self._mac(r) for r in self._q(
            f"SELECT {self._MCOLS} FROM lokkal_machines WHERE account_id = %s AND revoked_at IS NULL", (account,))]


__all__ = ["PgLinkStore"]
