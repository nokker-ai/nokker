"""Stockage des mandats et des budgets de dépense (docs/PAYMENTS.md §3-§4) : mémoire (tests, dev) ou Postgres sous RLS
(`nokker/store/migrations/056_pay_mandates.sql`). Tout est rangé dans le locataire COURANT
(`current_tenant()`) : un mandat dans celui du membre pour qui la carte a été posée, un budget dans celui du membre
qu'il borne (le payeur l'écrit sous `tenant_scope(locataire du membre)`, après contrôle par le service).

Rien n'est effacé ici, sauf avec le locataire (effacement de compte) : un mandat signé est une pièce."""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nokker.tenancy.context import current_tenant

MIGRATION = Path(__file__).resolve().parent.parent / "store" / "migrations" / "056_pay_mandates.sql"
LIVE = ("pending", "signed")  # comptent dans le budget : une carte en attente réserve son montant


@dataclass
class MandateRecord:
    id: str
    approval_id: str
    account: str | None
    approver: str | None
    status: str  # pending | signed | rejected | expired
    amount_minor: int
    currency: str
    merchant: str
    created_at: float
    valid_until: float
    mandate: dict[str, Any]
    mandate_sha256: str
    envelope: dict[str, Any] | None = None
    signed_at: float | None = None
    tenant_id: str = ""


@dataclass
class Budget:
    account: str
    currency: str
    per_purchase_minor: int
    per_day_minor: int
    per_month_minor: int
    minor_allowed: bool = False
    set_by: str | None = None
    updated_at: float = 0.0
    tenant_id: str = ""

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("tenant_id", None)
        return d


@dataclass
class MemoryPaymentStore:
    _mandates: dict[str, dict[str, MandateRecord]] = field(default_factory=dict)
    _budgets: dict[str, dict[str, Budget]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _m(self) -> dict[str, MandateRecord]:
        return self._mandates.setdefault(current_tenant(), {})

    def put_mandate(self, rec: MandateRecord) -> None:
        with self._lock:
            rec.tenant_id = current_tenant()
            self._m()[rec.id] = rec

    def get_mandate(self, mandate_id: str) -> MandateRecord | None:
        with self._lock:
            return self._m().get(mandate_id)

    def by_approval(self, approval_id: str) -> MandateRecord | None:
        with self._lock:
            return next((r for r in self._m().values() if r.approval_id == approval_id), None)

    def list_mandates(self, account: str | None, limit: int = 50) -> list[MandateRecord]:
        with self._lock:
            rows = [r for r in self._m().values() if account is None or r.account == account]
        return sorted(rows, key=lambda r: r.created_at, reverse=True)[:limit]

    def spent(self, account: str | None, currency: str, since: float, now: float) -> int:
        with self._lock:
            return sum(r.amount_minor for r in self._m().values()
                       if r.account == account and r.currency == currency and r.created_at >= since
                       and (r.status == "signed" or (r.status == "pending" and r.valid_until > now)))

    def get_budget(self, account: str) -> Budget | None:
        with self._lock:
            return self._budgets.setdefault(current_tenant(), {}).get(account)

    def put_budget(self, b: Budget) -> None:
        with self._lock:
            b.tenant_id = current_tenant()
            self._budgets.setdefault(b.tenant_id, {})[b.account] = b

    def delete_budget(self, account: str) -> bool:
        with self._lock:
            return self._budgets.setdefault(current_tenant(), {}).pop(account, None) is not None


_MANDATE_COLS = ("id, approval_id, account, approver, status, amount_minor, currency, merchant, created_at, "
                 "valid_until, mandate, mandate_sha256, envelope, signed_at, tenant_id")
_BUDGET_COLS = "account, currency, per_purchase_minor, per_day_minor, per_month_minor, minor_allowed, set_by, " \
               "updated_at, tenant_id"


class PgPaymentStore:
    """Même contrat que `MemoryPaymentStore`, Postgres sous RLS (`app.tenant_id` reposé à chaque usage)."""

    def __init__(self, dsn: str) -> None:
        from nokker.store.conn import TenantConn, migrate

        migrate(dsn, MIGRATION.read_text(encoding="utf-8"))
        self._db = TenantConn(dsn)
        self._lock = threading.Lock()

    @staticmethod
    def _rec(row: tuple) -> MandateRecord:
        (mid, aid, account, approver, status, amount, currency, merchant, created, valid, mandate, digest, env,
         signed_at, tid) = row
        return MandateRecord(id=mid, approval_id=aid, account=account, approver=approver, status=status,
                             amount_minor=int(amount), currency=currency, merchant=merchant, created_at=created,
                             valid_until=valid, mandate=dict(mandate), mandate_sha256=digest,
                             envelope=dict(env) if env else None, signed_at=signed_at, tenant_id=tid)

    def put_mandate(self, rec: MandateRecord) -> None:
        from psycopg.types.json import Jsonb

        with self._lock:
            rec.tenant_id = current_tenant()
            self._db.get().execute(
                "INSERT INTO pay_mandates (id, approval_id, account, approver, status, amount_minor, currency, merchant,"
                " created_at, valid_until, mandate, mandate_sha256, envelope, signed_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id, id) DO UPDATE SET "
                "status = EXCLUDED.status, envelope = EXCLUDED.envelope, signed_at = EXCLUDED.signed_at",
                (rec.id, rec.approval_id, rec.account, rec.approver, rec.status, rec.amount_minor, rec.currency,
                 rec.merchant, rec.created_at, rec.valid_until, Jsonb(rec.mandate), rec.mandate_sha256,
                 Jsonb(rec.envelope) if rec.envelope is not None else None, rec.signed_at))

    def _one(self, where: str, arg: Any) -> MandateRecord | None:
        with self._lock:
            row = self._db.get().execute(f"SELECT {_MANDATE_COLS} FROM pay_mandates WHERE {where}", (arg,)).fetchone()
        return self._rec(row) if row else None

    def get_mandate(self, mandate_id: str) -> MandateRecord | None:
        return self._one("id = %s", mandate_id)

    def by_approval(self, approval_id: str) -> MandateRecord | None:
        return self._one("approval_id = %s", approval_id)

    def list_mandates(self, account: str | None, limit: int = 50) -> list[MandateRecord]:
        with self._lock:
            if account is None:
                rows = self._db.get().execute(f"SELECT {_MANDATE_COLS} FROM pay_mandates ORDER BY created_at DESC "
                                              "LIMIT %s", (limit,)).fetchall()
            else:
                rows = self._db.get().execute(f"SELECT {_MANDATE_COLS} FROM pay_mandates WHERE account = %s "
                                              "ORDER BY created_at DESC LIMIT %s", (account, limit)).fetchall()
        return [self._rec(r) for r in rows]

    def spent(self, account: str | None, currency: str, since: float, now: float) -> int:
        with self._lock:
            row = self._db.get().execute(
                "SELECT COALESCE(SUM(amount_minor), 0) FROM pay_mandates WHERE account IS NOT DISTINCT FROM %s "
                "AND currency = %s AND created_at >= %s "
                "AND (status = 'signed' OR (status = 'pending' AND valid_until > %s))",
                (account, currency, since, now)).fetchone()
        return int(row[0] or 0)

    def get_budget(self, account: str) -> Budget | None:
        with self._lock:
            row = self._db.get().execute(f"SELECT {_BUDGET_COLS} FROM pay_budgets WHERE account = %s",
                                         (account,)).fetchone()
        if not row:
            return None
        acc, cur, pp, pd, pm, minor, by, at, tid = row
        return Budget(account=acc, currency=cur, per_purchase_minor=int(pp), per_day_minor=int(pd),
                      per_month_minor=int(pm), minor_allowed=bool(minor), set_by=by, updated_at=at, tenant_id=tid)

    def put_budget(self, b: Budget) -> None:
        with self._lock:
            b.tenant_id = current_tenant()
            self._db.get().execute(
                "INSERT INTO pay_budgets (account, currency, per_purchase_minor, per_day_minor, per_month_minor, "
                "minor_allowed, set_by, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id, account) DO UPDATE SET currency = EXCLUDED.currency, "
                "per_purchase_minor = EXCLUDED.per_purchase_minor, per_day_minor = EXCLUDED.per_day_minor, "
                "per_month_minor = EXCLUDED.per_month_minor, minor_allowed = EXCLUDED.minor_allowed, "
                "set_by = EXCLUDED.set_by, updated_at = EXCLUDED.updated_at",
                (b.account, b.currency, b.per_purchase_minor, b.per_day_minor, b.per_month_minor, b.minor_allowed,
                 b.set_by, b.updated_at))

    def delete_budget(self, account: str) -> bool:
        with self._lock:
            cur = self._db.get().execute("DELETE FROM pay_budgets WHERE account = %s", (account,))
            return (cur.rowcount or 0) > 0


__all__ = ["LIVE", "Budget", "MandateRecord", "MemoryPaymentStore", "PgPaymentStore"]
