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


def test_card_and_3ds_store_runs_under_the_agent_role(pg):  # noqa: F811
    """Émetteur `wise` (064) : une carte par mandat, défis 3-D Secure uniques par référence, aucun chiffre de carte."""
    import psycopg
    from psycopg.conninfo import make_conninfo

    from payagent.store import Card, Link, Method, PgStore, ThreeDS

    pw = secrets.token_hex(12)
    with psycopg.connect(pg["su"], autocommit=True) as su:
        su.execute(f"ALTER ROLE nokker_pay_agent PASSWORD '{pw}'")
    store = PgStore(make_conninfo(pg["su"], user="nokker_pay_agent", password=pw))
    store.put_link(Link("us_bob", "wise:29751964", None, "NEW", 1.0, 1.0))
    store.put_link(Link("us_bob", "wise:29751964", "rev-1", "PASSED", 1.0, 2.0, required_by=None))
    assert store.get_link("us_bob").state == "PASSED" and store.link_by_profile("wise:29751964").review_id == "rev-1"
    store.put_method(Method("us_bob", "wise:29751964", "wise_card", "VISA", None, None, None, "dev_bob", 1.0,
                            issuer="wise", holder_name="Bob Example"))
    m = store.get_method("us_bob")
    assert m.issuer == "wise" and m.holder_name == "Bob Example"
    c = Card("cd_1", "md_0000000000000002", "z" * 64, "us_bob", "wise", "wise:29751964", "card-tok-1", "vc_1",
             "shop.example.ch", "CHF", 12990, 2_000_000_000, "active", 1.0, 1.0, kernel_ref="tn_b",
             vault_sealed={"number": "ev:encrypted:x"})
    assert store.insert_card(c) is True
    assert store.insert_card(Card(**{**c.__dict__, "id": "cd_2", "card_ref": "card-tok-2"})) is False
    got = store.card_by_ref("card-tok-1")
    assert got.vault_sealed == {"number": "ev:encrypted:x"} and got.kernel_ref == "tn_b"
    t = ThreeDS("3ds_1", "cd_1", c.mandate_id, "us_bob", "148990660", "wise:29751964", 12990, "CHF", "BOUTIQUE",
                "https://shop.example.ch", "CH", "host", 2_000_000_000.0, "pending", 1.0)
    assert store.put_threeds(t) is True and store.put_threeds(ThreeDS(**{**t.__dict__, "id": "3ds_2"})) is False
    assert store.pending_threeds("cd_1").id == "3ds_1"
    store.update_threeds(ThreeDS(**{**t.__dict__, "status": "approved", "decided_at": 2.0, "device_id": "dev_bob"}))
    assert store.pending_threeds("cd_1") is None
    store.update_card(Card(**{**c.__dict__, "status": "deactivated", "used": True, "vault_sealed": {}}))
    assert store.live_cards("us_bob") == [] and store.card_by_mandate(c.mandate_id).vault_sealed == {}
    with psycopg.connect(pg["su"], autocommit=True) as su:
        cols = {r[0] for r in su.execute("SELECT column_name FROM information_schema.columns "
                                         "WHERE table_name IN ('pay_agent_cards', 'pay_agent_3ds', "
                                         "'pay_agent_links')")}
    assert not cols & {"pan", "cvv", "cvc", "expiry", "expiry_date", "last4", "first_name", "date_of_birth", "email"}
