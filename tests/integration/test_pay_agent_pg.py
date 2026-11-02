"""Agent de paiement sur un vrai Postgres (docs/PAY-AGENT.md §6). Ignoré sans `NOKKER_TEST_PG_DSN`.

Prouve : le rôle `nokker_pay_agent` fait tourner le stockage de l'agent (moyen, jeton unique par mandat, événements
vus une fois, clés des passkeys) ; l'état du jeton côté noyau (`pay_mandates.agent`) reste sous la RLS du locataire."""
from __future__ import annotations

import secrets

import pytest

from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401  (pg : fixture)

pytestmark = [pytest.mark.integration, requires_pg]


def test_agent_store_runs_under_its_own_role(pg):  # noqa: F811
    import psycopg
    from psycopg.conninfo import make_conninfo

    from payagent.store import Method, PgDevices, PgStore, Token

    pw = secrets.token_hex(12)
    with psycopg.connect(pg["su"], autocommit=True) as su:
        su.execute(f"ALTER ROLE nokker_pay_agent PASSWORD '{pw}'")
        su.execute("INSERT INTO devices (id, code, \"user\", name, public_key_spki, status, created_at) "
                   "VALUES ('dev_bob', 'c-bob', 'us_bob', 'bob', 'spki-bob', 'approved', 1.0) ON CONFLICT DO NOTHING")
    dsn = make_conninfo(pg["su"], user="nokker_pay_agent", password=pw)
    store, devices = PgStore(dsn), PgDevices(dsn)
    assert devices.get("dev_bob").public_key_spki == "spki-bob" and devices.get("nope") is None
    store.put_method(Method("us_bob", "pm_1", "card", "visa", "4242", 12, 2030, "dev_bob", 1.0))
    store.put_method(Method("us_bob", "pm_2", "card", "visa", "1111", 1, 2031, "dev_bob", 2.0))
    assert store.get_method("us_bob").payment_method == "pm_2"
    t = Token("tk_1", "md_0000000000000001", "x" * 64, "us_bob", "spt_1", "active", "profile_test000000", "CHF",
              1000, 2_000_000_000, 1.0, 1.0, kernel_ref="tn_b")
    assert store.insert_token(t) is True
    assert store.insert_token(Token(**{**t.__dict__, "id": "tk_2", "spt_id": "spt_2"})) is False  # un jeton par mandat
    store.update_token(Token(**{**t.__dict__, "used": True, "updated_at": 2.0}))
    got = store.token_by_spt("spt_1")
    assert got.used is True and got.kernel_ref == "tn_b" and store.token_by_mandate(t.mandate_id).id == "tk_1"
    assert [x.id for x in store.live_tokens("us_bob")] == ["tk_1"]
    assert store.first_event("evt_9") is True and store.first_event("evt_9") is False
    assert store.delete_method("us_bob") is True and store.get_method("us_bob") is None


def test_kernel_agent_state_stays_in_its_tenant(pg):  # noqa: F811
    from nokker.payments.store import MandateRecord, PgPaymentStore
    from nokker.tenancy.context import tenant_scope

    store = PgPaymentStore(pg["app"])
    rec = MandateRecord(id="md_agent", approval_id="ap-agent", account="us_alice", approver="us_alice",
                        status="signed", amount_minor=1000, currency="CHF", merchant="Boutique Example",
                        created_at=10.0, valid_until=10.0 + 86400, mandate={"m": 1}, mandate_sha256="y" * 64)
    with tenant_scope(A):
        store.put_mandate(rec)
        store.put_agent("md_agent", {"id": "tk_1", "status": "active"})
        assert store.get_agent("md_agent") == {"id": "tk_1", "status": "active"}
    with tenant_scope(B):
        assert store.get_agent("md_agent") is None
        store.put_agent("md_agent", {"status": "deactivated"})  # aucune ligne chez B : sans effet
    with tenant_scope(A):
        assert store.get_agent("md_agent")["status"] == "active"
