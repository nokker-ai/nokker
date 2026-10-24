"""`PgApprovals` — backend `Approvals` Postgres (docs/APPROVALS.md §4, prod). Même logique de preuve que
`memory.InMemoryApprovals` (partagée dans `base.BaseApprovals`) ; ici, psycopg 3 + les tables de
`migrations/0001_approvals.sql`. Le secret TOTP est chiffré (Fernet) si `NOKKER_APPROVALS_ENC_KEY` est
posée dans l'environnement, stocké en clair sinon (limite documentée, docs/APPROVALS.md §3) — dans les
DEUX cas, aucune route de `nokker/api/server.py` ne le renvoie jamais."""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from nokker.approvals.base import BaseApprovals
from nokker.approvals.types import Device, Session
from nokker.core.ports import Approval
from nokker.store.conn import TenantConn, connect_admin, migrate

MIGRATION_SQL = (Path(__file__).parent / "migrations" / "0001_approvals.sql").read_text(encoding="utf-8")

_ENC_KEY_ENV = "NOKKER_APPROVALS_ENC_KEY"


def _fernet_or_none():
    key = os.environ.get(_ENC_KEY_ENV, "").strip()
    if not key:
        return None
    from cryptography.fernet import Fernet

    return Fernet(key.encode("utf-8"))


def _enc_approval(a: Approval) -> dict[str, Any]:
    return {"id": a.id, "thread_id": a.thread_id, "turn_index": a.turn_index, "channel": a.channel,
            "tool_name": a.tool_name, "arguments": a.arguments, "args_sha256": a.args_sha256,
            "strength": a.strength, "rule": a.rule, "reason": a.reason, "advice": a.advice,
            "status": a.status, "created_at": a.created_at, "expires_at": a.expires_at,
            "decided_by": a.decided_by, "decided_via": a.decided_via, "decided_at": a.decided_at,
            "result_summary": a.result_summary, "error": a.error}


def _row_to_approval(row: tuple) -> Approval:
    (aid, thread_id, turn_index, channel, tool_name, arguments, args_sha256, strength, rule, reason, advice,
     status, created_at, expires_at, decided_by, decided_via, decided_at, result_summary, error) = row
    return Approval(id=aid, thread_id=thread_id, turn_index=turn_index, channel=channel, tool_name=tool_name,
                    arguments=dict(arguments), args_sha256=args_sha256, strength=strength, rule=rule,
                    reason=reason or "", advice=advice, status=status, created_at=created_at,
                    expires_at=expires_at, decided_by=decided_by, decided_via=decided_via,
                    decided_at=decided_at, result_summary=result_summary, error=error)


_APPROVAL_COLUMNS = ("id, thread_id, turn_index, channel, tool_name, arguments, args_sha256, strength, rule, "
                    "reason, advice, status, created_at, expires_at, decided_by, decided_via, decided_at, "
                    "result_summary, error")


@dataclass
class PgApprovals(BaseApprovals):
    dsn: str = ""

    def __post_init__(self) -> None:
        if not self.dsn:
            raise ValueError("PgApprovals requiert un DSN Postgres")
        migrate(self.dsn, MIGRATION_SQL)  # en propriétaire, puis la RLS (nokker/store/conn.py)
        self._db = TenantConn(self.dsn)
        # Tables d'authentification (devices, sessions, totp_secrets, used_nonces) : hors RLS, lues AVANT de connaître
        # le locataire (verify_session, login_challenge, create_session). Même DSN, donc même rôle nokker_app, mais une
        # connexion à part qui ne porte jamais app.tenant_id ; filtrage par compte dans base.py.
        self._auth = connect_admin(self.dsn, autocommit=True)
        self._lock = threading.Lock()
        self._fernet = _fernet_or_none()

    @property
    def _conn(self) -> Any:
        return self._db.get()  # app.tenant_id = locataire courant, reposé à chaque usage (NoTenant hors scope)

    def close(self) -> None:
        self._db.close()
        self._auth.close()

    # ─────────────────────── approbations ───────────────────────

    def _load_approval(self, approval_id: str) -> Approval | None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(f"SELECT {_APPROVAL_COLUMNS} FROM approvals WHERE id = %s", (approval_id,))
            row = cur.fetchone()
        return _row_to_approval(row) if row else None

    def _store_approval(self, approval: Approval) -> None:
        d = _enc_approval(approval)
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO approvals (id, thread_id, turn_index, channel, tool_name, arguments, args_sha256, "
                "strength, rule, reason, advice, status, created_at, expires_at, decided_by, decided_via, "
                "decided_at, result_summary, error) "
                "VALUES (%(id)s, %(thread_id)s, %(turn_index)s, %(channel)s, %(tool_name)s, %(arguments)s, "
                "%(args_sha256)s, %(strength)s, %(rule)s, %(reason)s, %(advice)s, %(status)s, %(created_at)s, "
                "%(expires_at)s, %(decided_by)s, %(decided_via)s, %(decided_at)s, %(result_summary)s, %(error)s) "
                "ON CONFLICT (id) DO UPDATE SET status=%(status)s, decided_by=%(decided_by)s, "
                "decided_via=%(decided_via)s, decided_at=%(decided_at)s, result_summary=%(result_summary)s, "
                "error=%(error)s, arguments=%(arguments)s, args_sha256=%(args_sha256)s",
                {**d, "arguments": Jsonb(d["arguments"])})

    def _list_pending_approvals(self, user: str | None) -> list[Approval]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(f"SELECT {_APPROVAL_COLUMNS} FROM approvals WHERE status = 'pending'")
            rows = cur.fetchall()
        return [_row_to_approval(r) for r in rows]

    def _append_event(self, approval_id: str, event_type: str, data: dict) -> None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute("INSERT INTO approval_events (approval_id, event_type, data) VALUES (%s, %s, %s)",
                        (approval_id, event_type, Jsonb(data)))

    # ─────────────────────── nonces ───────────────────────

    def _reserve_nonce(self, nonce: str, scope_id: str, exp: float) -> None:
        with self._lock, self._auth.cursor() as cur:
            cur.execute("INSERT INTO used_nonces (nonce, scope_id, exp, used) VALUES (%s, %s, %s, FALSE)",
                        (nonce, scope_id, exp))

    def _consume_nonce(self, nonce: str, scope_id: str) -> bool:
        with self._lock, self._auth.cursor() as cur:
            cur.execute(
                "UPDATE used_nonces SET used = TRUE "
                "WHERE nonce = %s AND scope_id = %s AND used = FALSE AND exp >= %s "
                "RETURNING nonce", (nonce, scope_id, self.clock()))
            return cur.fetchone() is not None

    # ─────────────────────── appareils ───────────────────────

    def _device_row_to_obj(self, row: tuple) -> Device:
        (did, code, user, name, pub, status, created_at, approved_at, approved_via, tg_id) = row
        return Device(id=did, code=code, user=user, name=name, public_key_spki=pub, status=status,
                      created_at=created_at, approved_at=approved_at, approved_via=approved_via,
                      telegram_user_id=tg_id)

    def _load_device(self, device_id: str) -> Device | None:
        with self._lock, self._auth.cursor() as cur:
            cur.execute('SELECT id, code, "user", name, public_key_spki, status, created_at, approved_at, '
                       'approved_via, telegram_user_id FROM devices WHERE id = %s', (device_id,))
            row = cur.fetchone()
        return self._device_row_to_obj(row) if row else None

    def _load_device_by_code(self, code: str) -> Device | None:
        with self._lock, self._auth.cursor() as cur:
            cur.execute('SELECT id, code, "user", name, public_key_spki, status, created_at, approved_at, '
                       'approved_via, telegram_user_id FROM devices WHERE code = %s', (code,))
            row = cur.fetchone()
        return self._device_row_to_obj(row) if row else None

    def _store_device(self, device: Device) -> None:
        with self._lock, self._auth.cursor() as cur:
            cur.execute(
                'INSERT INTO devices (id, code, "user", name, public_key_spki, status, created_at, approved_at, '
                'approved_via, telegram_user_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                'ON CONFLICT (id) DO UPDATE SET status=%s, approved_at=%s, approved_via=%s',
                (device.id, device.code, device.user, device.name, device.public_key_spki, device.status,
                 device.created_at, device.approved_at, device.approved_via, device.telegram_user_id,
                 device.status, device.approved_at, device.approved_via))

    def _list_devices(self, user: str | None) -> list[Device]:
        with self._lock, self._auth.cursor() as cur:
            if user is None:
                cur.execute('SELECT id, code, "user", name, public_key_spki, status, created_at, approved_at, '
                           'approved_via, telegram_user_id FROM devices')
            else:
                cur.execute('SELECT id, code, "user", name, public_key_spki, status, created_at, approved_at, '
                           'approved_via, telegram_user_id FROM devices WHERE "user" = %s', (user,))
            rows = cur.fetchall()
        return [self._device_row_to_obj(r) for r in rows]

    def _delete_device(self, device_id: str) -> None:
        with self._lock, self._auth.cursor() as cur:
            cur.execute("DELETE FROM devices WHERE id = %s", (device_id,))

    # ─────────────────────── TOTP ───────────────────────

    def _load_totp(self, user: str) -> tuple[str, int | None] | None:
        with self._lock, self._auth.cursor() as cur:
            cur.execute('SELECT secret, encrypted, last_step FROM totp_secrets WHERE "user" = %s', (user,))
            row = cur.fetchone()
        if row is None:
            return None
        secret, encrypted, last_step = row
        if encrypted and self._fernet is not None:
            secret = self._fernet.decrypt(secret.encode("utf-8")).decode("utf-8")
        return secret, last_step

    def _store_totp_secret(self, user: str, secret_b32: str) -> None:
        encrypted = self._fernet is not None
        stored = self._fernet.encrypt(secret_b32.encode("utf-8")).decode("utf-8") if encrypted else secret_b32
        with self._lock, self._auth.cursor() as cur:
            cur.execute(
                'INSERT INTO totp_secrets ("user", secret, encrypted, last_step) VALUES (%s, %s, %s, NULL) '
                'ON CONFLICT ("user") DO UPDATE SET secret = %s, encrypted = %s, last_step = NULL',
                (user, stored, encrypted, stored, encrypted))

    def _set_totp_last_step(self, user: str, step: int) -> None:
        with self._lock, self._auth.cursor() as cur:
            cur.execute('UPDATE totp_secrets SET last_step = %s WHERE "user" = %s', (step, user))

    # ─────────────────────── sessions ───────────────────────

    def _store_session(self, session: Session) -> None:
        with self._lock, self._auth.cursor() as cur:
            cur.execute('INSERT INTO sessions (token, "user", device_id, created_at, expires_at) '
                       'VALUES (%s, %s, %s, %s, %s)',
                       (session.token, session.user, session.device_id, session.created_at, session.expires_at))

    def _load_session(self, token: str) -> Session | None:
        with self._lock, self._auth.cursor() as cur:
            cur.execute('SELECT token, "user", device_id, created_at, expires_at FROM sessions WHERE token = %s',
                       (token,))
            row = cur.fetchone()
        if row is None:
            return None
        return Session(token=row[0], user=row[1], device_id=row[2], created_at=row[3], expires_at=row[4])


__all__ = ["MIGRATION_SQL", "PgApprovals"]
