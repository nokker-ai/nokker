"""Comptes et identités. Un compte = un locataire pendant la bêta (`create_account` crée les deux).

Identités : `device` (id d'appareil enrôlé, docs/APPROVALS.md), `telegram` (id numérique Telegram, appairage), `email`
(adresse d'invitation, posée à la création). Une identité ne désigne jamais deux comptes (`IdentityTaken`) : c'est la
première ligne de l'isolation — un message Telegram, une session web ou un appareil n'arrivent que dans UN locataire.
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

KINDS = ("device", "telegram", "email")


class IdentityTaken(Exception):
    """L'identité est déjà liée à un autre compte."""


@dataclass(frozen=True)
class Account:
    tenant_id: str
    account_id: str
    email: str
    lang: str = "fr"
    status: str = "active"
    created_at: datetime | None = None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


class Tenancy(Protocol):
    def create_account(self, email: str, lang: str = "fr", invitation_id: str | None = None) -> Account: ...

    def account(self, account_id: str) -> Account | None: ...

    def account_by_email(self, email: str) -> Account | None: ...

    def bind_identity(self, account_id: str, kind: str, value: str) -> None: ...

    def account_for_identity(self, kind: str, value: str) -> Account | None: ...

    def identities_of(self, account_id: str, kind: str) -> list[str]: ...

    def account_for_tenant(self, tenant_id: str) -> Account | None: ...


@dataclass
class MemoryTenancy:
    accounts: dict[str, Account] = field(default_factory=dict)
    identities: dict[tuple[str, str], str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def create_account(self, email: str, lang: str = "fr", invitation_id: str | None = None) -> Account:
        email = _norm_email(email)
        if not email or "@" not in email:
            raise ValueError("e-mail invalide")
        with self._lock:
            if any(a.email == email for a in self.accounts.values()):
                raise IdentityTaken(f"compte existant pour {email}")
            acc = Account(tenant_id=_new_id("tn"), account_id=_new_id("us"), email=email,
                          lang="en" if lang == "en" else "fr", created_at=datetime.now(timezone.utc))
            self.accounts[acc.account_id] = acc
            self.identities[("email", email)] = acc.account_id
            return acc

    def account(self, account_id: str) -> Account | None:
        return self.accounts.get(account_id)

    def account_by_email(self, email: str) -> Account | None:
        return self.account_for_identity("email", _norm_email(email))

    def bind_identity(self, account_id: str, kind: str, value: str) -> None:
        if kind not in KINDS or not value:
            raise ValueError(f"identité invalide : {kind}")
        value = _norm_email(value) if kind == "email" else str(value)
        with self._lock:
            if account_id not in self.accounts:
                raise KeyError(account_id)
            owner = self.identities.get((kind, value))
            if owner is not None and owner != account_id:
                raise IdentityTaken(f"{kind} déjà lié à un autre compte")
            self.identities[(kind, value)] = account_id

    def identities_of(self, account_id: str, kind: str) -> list[str]:
        return sorted(v for (k, v), owner in self.identities.items() if k == kind and owner == account_id)

    def account_for_identity(self, kind: str, value: str) -> Account | None:
        owner = self.identities.get((kind, str(value)))
        acc = self.accounts.get(owner) if owner else None
        return acc if acc is not None and acc.status == "active" else None

    def account_for_tenant(self, tenant_id: str) -> Account | None:
        return next((a for a in self.accounts.values() if a.tenant_id == tenant_id and a.status == "active"), None)


class PgTenancy:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _acc(row) -> Account | None:
        if row is None:
            return None
        return Account(tenant_id=row["tenant_id"], account_id=row["id"], email=row["email"], lang=row["lang"],
                       status=row["status"], created_at=row["created_at"])

    def create_account(self, email: str, lang: str = "fr", invitation_id: str | None = None) -> Account:
        import psycopg

        email = _norm_email(email)
        if not email or "@" not in email:
            raise ValueError("e-mail invalide")
        tenant_id, account_id = _new_id("tn"), _new_id("us")
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO tenants (id) VALUES (%s)", (tenant_id,))
                cur.execute("INSERT INTO accounts (id, tenant_id, email, lang, invitation_id) VALUES (%s,%s,%s,%s,%s) "
                            "RETURNING *", (account_id, tenant_id, email, "en" if lang == "en" else "fr", invitation_id))
                row = cur.fetchone()
                cur.execute("INSERT INTO account_identities (kind, value, account_id) VALUES ('email', %s, %s)",
                            (email, account_id))
        except psycopg.errors.UniqueViolation as e:
            raise IdentityTaken(f"compte existant pour {email}") from e
        return self._acc(row)  # type: ignore[return-value]

    def account(self, account_id: str) -> Account | None:
        with self._conn() as conn:
            return self._acc(conn.execute("SELECT * FROM accounts WHERE id=%s", (account_id,)).fetchone())

    def account_by_email(self, email: str) -> Account | None:
        return self.account_for_identity("email", _norm_email(email))

    def bind_identity(self, account_id: str, kind: str, value: str) -> None:
        if kind not in KINDS or not value:
            raise ValueError(f"identité invalide : {kind}")
        value = _norm_email(value) if kind == "email" else str(value)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO account_identities (kind, value, account_id) VALUES (%s,%s,%s) "
                        "ON CONFLICT (kind, value) DO NOTHING", (kind, value, account_id))
            cur.execute("SELECT account_id FROM account_identities WHERE kind=%s AND value=%s", (kind, value))
            owner = cur.fetchone()["account_id"]
            if owner != account_id:
                raise IdentityTaken(f"{kind} déjà lié à un autre compte")

    def identities_of(self, account_id: str, kind: str) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT value FROM account_identities WHERE account_id=%s AND kind=%s ORDER BY value",
                                (account_id, kind)).fetchall()
        return [r["value"] for r in rows]

    def account_for_identity(self, kind: str, value: str) -> Account | None:
        with self._conn() as conn:
            row = conn.execute("SELECT a.* FROM account_identities i JOIN accounts a ON a.id = i.account_id "
                               "WHERE i.kind=%s AND i.value=%s AND a.status='active'", (kind, str(value))).fetchone()
        return self._acc(row)

    def account_for_tenant(self, tenant_id: str) -> Account | None:
        with self._conn() as conn:
            return self._acc(conn.execute("SELECT * FROM accounts WHERE tenant_id=%s AND status='active'",
                                          (tenant_id,)).fetchone())


__all__ = ["Account", "IdentityTaken", "KINDS", "MemoryTenancy", "PgTenancy", "Tenancy"]
