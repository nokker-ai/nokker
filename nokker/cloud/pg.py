"""Magasin Postgres du lien Lokkal (tables des migrations 014, 018, 019 et 020), même interface que `MemoryLinkStore`.

`lokkal_machines` et les tables de la passerelle sont hors RLS (cherchées par empreinte) ; la charge des machines
(`lokkal_machine_load`, 020) est sous RLS : lue et écrite dans le `tenant_scope` du locataire de la machine."""
from __future__ import annotations

import time
from typing import Any

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
                       revoked=r[7] is not None, last_seen_at=r[8] if len(r) > 8 else None)

    _RCOLS = "device_sha256, user_code, machine_name, expires_at, state, machine_id"
    _MCOLS = "id, account_id, tenant_id, name, host, token_sha256, created_at, revoked_at, last_seen_at"

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
        self._q(f"INSERT INTO lokkal_machines ({self._MCOLS}) VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, NULL)",
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

    def seen(self, machine_id: str, at: float) -> None:
        self._q("UPDATE lokkal_machines SET last_seen_at = %s WHERE id = %s", (at, machine_id))

    def of_account(self, account: str) -> list[Machine]:
        return [self._mac(r) for r in self._q(
            f"SELECT {self._MCOLS} FROM lokkal_machines WHERE account_id = %s AND revoked_at IS NULL", (account,))]

    def _tq(self, tenant: str, sql: str, params: tuple = ()) -> list[tuple]:
        """Requête sous la RLS de `tenant` (`app.tenant_id` posé par `connect`)."""
        from nokker.store.conn import connect
        from nokker.tenancy.context import tenant_scope

        with tenant_scope(tenant), connect(self.dsn, autocommit=True) as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def set_load(self, machine_id: str, tenant: str, load: dict[str, Any], at: float) -> None:
        from psycopg.types.json import Jsonb

        self._tq(tenant, "INSERT INTO lokkal_machine_load (machine_id, tenant_id, load, at) VALUES (%s, %s, %s, %s) "
                 "ON CONFLICT (machine_id) DO UPDATE SET load = EXCLUDED.load, at = EXCLUDED.at",
                 (machine_id, tenant, Jsonb(load), at))

    def loads_of(self, tenant: str, machine_ids: list[str]) -> dict[str, tuple[dict[str, Any], float]]:
        if not machine_ids:
            return {}
        return {r[0]: (r[1], r[2]) for r in self._tq(
            tenant, "SELECT machine_id, load, at FROM lokkal_machine_load WHERE machine_id = ANY(%s)",
            (list(machine_ids),))}


class PgGatewayAuthStore:
    """Codes et jetons de renouvellement de la passerelle (migration 019), même interface que
    `MemoryGatewayAuthStore`. Usage unique par `DELETE … RETURNING` / `UPDATE … RETURNING` (atomiques)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _q(self, sql: str, params: tuple = ()) -> list[tuple]:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def put_code(self, sha: str, c: Any) -> None:
        self._q("DELETE FROM lokkal_gateway_codes WHERE expires_at < %s", (time.time(),))
        self._q("INSERT INTO lokkal_gateway_codes (code_sha256, account_id, tenant_id, machine_id, host, challenge, "
                "expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (sha, c.account, c.tenant, c.machine_id, c.host, c.challenge, c.expires_at))

    def take_code(self, sha: str) -> Any:
        from nokker.cloud.gateway_auth import _Code

        rows = self._q("DELETE FROM lokkal_gateway_codes WHERE code_sha256 = %s RETURNING account_id, tenant_id, "
                       "machine_id, host, challenge, expires_at", (sha,))
        return _Code(*rows[0]) if rows else None

    def put_refresh(self, sha: str, r: Any) -> None:
        self._q("INSERT INTO lokkal_gateway_refresh (token_sha256, family, account_id, tenant_id, machine_id, host, "
                "expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (sha, r.family, r.account, r.tenant, r.machine_id, r.host, r.expires_at))

    def use_refresh(self, sha: str) -> Any:
        from nokker.cloud.gateway_auth import _Refresh

        # L'état AVANT le marquage : une seule requête gagne used_at NULL → non NULL.
        rows = self._q("UPDATE lokkal_gateway_refresh r SET used_at = COALESCE(r.used_at, %s) FROM "
                       "(SELECT token_sha256, used_at AS was_used FROM lokkal_gateway_refresh WHERE token_sha256 = %s "
                       "FOR UPDATE) old WHERE r.token_sha256 = old.token_sha256 RETURNING r.family, r.account_id, "
                       "r.tenant_id, r.machine_id, r.host, r.expires_at, old.was_used IS NOT NULL, "
                       "r.revoked_at IS NOT NULL", (time.time(), sha))
        if not rows:
            return None
        f = rows[0]
        return _Refresh(f[0], f[1], f[2], f[3], f[4], f[5], used=bool(f[6]), revoked=bool(f[7]))

    def revoke_family(self, family: str) -> None:
        self._q("UPDATE lokkal_gateway_refresh SET revoked_at = %s WHERE family = %s AND revoked_at IS NULL",
                (time.time(), family))

    def family_of(self, sha: str) -> str | None:
        rows = self._q("SELECT family FROM lokkal_gateway_refresh WHERE token_sha256 = %s", (sha,))
        return rows[0][0] if rows else None


__all__ = ["PgGatewayAuthStore", "PgLinkStore"]
