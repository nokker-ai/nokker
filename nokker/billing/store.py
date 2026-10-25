"""Stockage des abonnements (migration 010). `subscriptions` est sous RLS (connexion du locataire courant) ;
`billing_customers` et `billing_events` sont hors RLS (le webhook arrive sans locataire)."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from nokker.billing.plans import ACTIVE_STATUSES
from nokker.tenancy.context import current_tenant


class BillingStore(Protocol):
    def customer_for_tenant(self, tenant_id: str) -> str | None: ...

    def tenant_for_customer(self, customer: str) -> tuple[str, str] | None:
        """(tenant_id, account_id) du client Stripe, ou None s'il n'est pas à nous."""
        ...

    def put_customer(self, customer: str, tenant_id: str, account_id: str) -> str:
        """Enregistre et rend le client Stripe du locataire (celui déjà là si deux créations se sont croisées)."""
        ...

    def event_seen(self, event_id: str) -> bool: ...

    def mark_event(self, event_id: str, etype: str) -> None: ...

    def upsert_subscription(self, row: dict[str, Any]) -> bool:
        """Dans le locataire courant. Ignoré (False) si un événement plus récent a déjà été appliqué."""
        ...

    def subscription(self, tenant_id: str) -> dict[str, Any] | None:
        """Abonnement le plus récent du locataire, ou None."""
        ...


@dataclass
class MemoryBilling:
    customers: dict[str, tuple[str, str]] = field(default_factory=dict)
    events: dict[str, str] = field(default_factory=dict)
    subs: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def customer_for_tenant(self, tenant_id: str) -> str | None:
        return next((c for c, (t, _) in self.customers.items() if t == tenant_id), None)

    def tenant_for_customer(self, customer: str) -> tuple[str, str] | None:
        return self.customers.get(customer)

    def put_customer(self, customer: str, tenant_id: str, account_id: str) -> str:
        with self._lock:
            existing = self.customer_for_tenant(tenant_id)
            if existing:
                return existing
            self.customers[customer] = (tenant_id, account_id)
            return customer

    def event_seen(self, event_id: str) -> bool:
        return event_id in self.events

    def mark_event(self, event_id: str, etype: str) -> None:
        self.events.setdefault(event_id, etype)

    def upsert_subscription(self, row: dict[str, Any]) -> bool:
        tenant = current_tenant()
        key = (tenant, row["stripe_subscription"])
        with self._lock:
            old = self.subs.get(key)
            if old and (old["last_event_at"] > row["last_event_at"]
                        or (old["last_event_at"] == row["last_event_at"] and old["status"] == "canceled")):
                return False
            self.subs[key] = {**row, "tenant_id": tenant}
            return True

    def subscription(self, tenant_id: str) -> dict[str, Any] | None:
        rows = [r for (t, _), r in self.subs.items() if t == tenant_id]
        return max(rows, key=lambda r: (r["status"] in ACTIVE_STATUSES, r["last_event_at"])) if rows else None


class PgBilling:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _admin(self):
        from psycopg.rows import dict_row

        from nokker.store.conn import connect_admin

        return connect_admin(self.dsn, row_factory=dict_row, autocommit=True)

    def _tenant(self):
        from psycopg.rows import dict_row

        from nokker.store.conn import connect

        return connect(self.dsn, row_factory=dict_row, autocommit=True)

    def customer_for_tenant(self, tenant_id: str) -> str | None:
        with self._admin() as conn:
            row = conn.execute("SELECT stripe_customer FROM billing_customers WHERE tenant_id = %s", (tenant_id,)).fetchone()
        return row["stripe_customer"] if row else None

    def tenant_for_customer(self, customer: str) -> tuple[str, str] | None:
        with self._admin() as conn:
            row = conn.execute("SELECT tenant_id, account_id FROM billing_customers WHERE stripe_customer = %s",
                               (customer,)).fetchone()
        return (row["tenant_id"], row["account_id"]) if row else None

    def put_customer(self, customer: str, tenant_id: str, account_id: str) -> str:
        with self._admin() as conn:
            conn.execute("INSERT INTO billing_customers (stripe_customer, tenant_id, account_id) VALUES (%s,%s,%s) "
                         "ON CONFLICT DO NOTHING", (customer, tenant_id, account_id))
            row = conn.execute("SELECT stripe_customer FROM billing_customers WHERE tenant_id = %s", (tenant_id,)).fetchone()
        return row["stripe_customer"]

    def event_seen(self, event_id: str) -> bool:
        with self._admin() as conn:
            return conn.execute("SELECT 1 FROM billing_events WHERE event_id = %s", (event_id,)).fetchone() is not None

    def mark_event(self, event_id: str, etype: str) -> None:
        with self._admin() as conn:
            conn.execute("INSERT INTO billing_events (event_id, type) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                         (event_id, etype))

    def upsert_subscription(self, row: dict[str, Any]) -> bool:
        tenant = current_tenant()
        with self._tenant() as conn:
            r = conn.execute(
                "INSERT INTO subscriptions AS s (tenant_id, stripe_subscription, stripe_customer, plan, status, "
                "current_period_end, cancel_at_period_end, last_event_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id, stripe_subscription) DO UPDATE SET stripe_customer = EXCLUDED.stripe_customer, "
                "plan = EXCLUDED.plan, status = EXCLUDED.status, current_period_end = EXCLUDED.current_period_end, "
                "cancel_at_period_end = EXCLUDED.cancel_at_period_end, last_event_at = EXCLUDED.last_event_at, "
                "updated_at = now() WHERE s.last_event_at < EXCLUDED.last_event_at "
                "OR (s.last_event_at = EXCLUDED.last_event_at AND s.status <> 'canceled') RETURNING 1",
                (tenant, row["stripe_subscription"], row["stripe_customer"], row["plan"], row["status"],
                 row["current_period_end"], row["cancel_at_period_end"], row["last_event_at"])).fetchone()
        return r is not None

    def subscription(self, tenant_id: str) -> dict[str, Any] | None:
        from nokker.tenancy.context import tenant_scope

        with tenant_scope(tenant_id), self._tenant() as conn:
            return conn.execute("SELECT stripe_subscription, stripe_customer, plan, status, current_period_end, "
                                "cancel_at_period_end, last_event_at FROM subscriptions "
                                "ORDER BY (status = ANY(%s)) DESC, last_event_at DESC LIMIT 1",
                                (sorted(ACTIVE_STATUSES),)).fetchone()


def ts(value: float | int | None) -> datetime | None:
    from datetime import timezone

    return datetime.fromtimestamp(value, tz=timezone.utc) if value else None
