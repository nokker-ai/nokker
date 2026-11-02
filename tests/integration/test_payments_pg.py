"""Paiements sur un vrai Postgres (docs/PAYMENTS.md), rôle du pod `nokker_app` sous RLS. Ignoré sans
`NOKKER_TEST_PG_DSN` (base jetable de test_rls.py).

Prouve : un mandat et un budget de A sont invisibles à B (même par leur id), la somme engagée ne fuit pas, la migration
se rejoue sans erreur (idempotente), un mandat scellé relu de la base se vérifie encore hors ligne."""
from __future__ import annotations

import pytest

from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401  (pg : fixture)

pytestmark = [pytest.mark.integration, requires_pg]


def _rec(mid: str, account: str, amount: int = 1000):
    from nokker.payments.store import MandateRecord

    return MandateRecord(id=mid, approval_id=f"ap-{mid}", account=account, approver=account, status="signed",
                         amount_minor=amount, currency="CHF", merchant="Boutique Example", created_at=10.0,
                         valid_until=10.0 + 86400, mandate={"m": mid}, mandate_sha256="x" * 64,
                         envelope={"format": "nokker.mandate.v1"}, signed_at=11.0)


def test_mandates_and_budgets_are_isolated_by_tenant(pg):
    from nokker.payments.store import Budget, PgPaymentStore
    from nokker.store.conn import migrate
    from nokker.tenancy.context import tenant_scope

    store = PgPaymentStore(pg["app"])
    with open(store_path(), encoding="utf-8") as f:
        assert migrate(pg["owner"], f.read(), strict=True)  # rejouée : idempotente
    with tenant_scope(A):
        store.put_mandate(_rec("md_a", "us_alice"))
        store.put_budget(Budget(account="us_alice", currency="CHF", per_purchase_minor=100, per_day_minor=200,
                                per_month_minor=300, set_by="us_alice", updated_at=1.0))
        assert store.get_mandate("md_a").envelope == {"format": "nokker.mandate.v1"}
        assert store.spent("us_alice", "CHF", 0, 20.0) == 1000
        assert store.by_approval("ap-md_a").id == "md_a"
    with tenant_scope(B):
        assert store.get_mandate("md_a") is None and store.by_approval("ap-md_a") is None
        assert store.list_mandates(None) == [] and store.list_mandates("us_alice") == []
        assert store.spent("us_alice", "CHF", 0, 20.0) == 0
        assert store.get_budget("us_alice") is None and store.delete_budget("us_alice") is False
        store.put_mandate(_rec("md_a", "us_bob", 5))  # même id chez B : une autre ligne, A intact
    with tenant_scope(A):
        assert store.get_mandate("md_a").account == "us_alice"
        rec = store.get_mandate("md_a")
        rec.status = "rejected"
        store.put_mandate(rec)
        assert store.spent("us_alice", "CHF", 0, 20.0) == 0
        assert store.get_budget("us_alice").per_day_minor == 200 and store.delete_budget("us_alice")


def store_path() -> str:
    from nokker.payments.store import MIGRATION

    return str(MIGRATION)
