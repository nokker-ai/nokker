"""Lien Lokkal côté nokker.ai sur un vrai Postgres (migration 014, nokker/cloud/pg.py), avec le rôle du pod
(`nokker_app`) : flux complet, empreintes seulement en base, révocation, effacement du compte couvre les machines."""
from __future__ import annotations

import psycopg
import pytest

from nokker.cloud.link import LinkService
from nokker.cloud.pg import PgLinkStore
from tests.integration.test_rls import pg, requires_pg  # noqa: F401  (pg : fixture)


@requires_pg
def test_device_flow_on_postgres(pg):  # noqa: F811
    svc = LinkService(store=PgLinkStore(pg["app"]))
    d = svc.device("atelier")
    assert svc.token(d["device_code"]) == {"state": "pending"}
    assert svc.approve(d["user_code"], "us_pg", "tn_pg")["host"] == "atelier.nokker.app"
    t = svc.token(d["device_code"])
    assert t["state"] == "approved" and svc.token(d["device_code"]) == {"state": "expired"}
    m = svc.machine_for_token(t["machine_token"])
    assert m is not None and m.tenant == "tn_pg" and [x.id for x in svc.machines_of("us_pg")] == [m.id]
    with psycopg.connect(pg["su"]) as conn:
        dump = str(conn.execute("SELECT * FROM lokkal_machines").fetchall())
        assert t["machine_token"] not in dump  # jamais en clair
        assert conn.execute("SELECT count(*) FROM lokkal_link_requests").fetchone()[0] == 0
    d2 = svc.device("atelier")
    assert svc.approve(d2["user_code"], "us_pg2", "tn_pg2")["host"] == "atelier-2.nokker.app"
    assert svc.revoke(t["machine_token"]) and svc.machine_for_token(t["machine_token"]) is None


@requires_pg
def test_my_machines_last_seen_and_account_revoke_on_postgres(pg):  # noqa: F811
    import uuid

    store = PgLinkStore(pg["app"])
    svc = LinkService(store=store)
    acc = f"us_seen{uuid.uuid4().hex[:8]}"  # compte propre à ce test : la base est partagée par la session
    d = svc.device("portable")
    svc.approve(d["user_code"], acc, "tn_seen")
    m = svc.machine_for_token(svc.token(d["device_code"])["machine_token"])
    assert m.last_seen_at is None
    store.seen(m.id, 1_792_000_000.0)
    assert svc.machines_of(acc)[0].last_seen_at == 1_792_000_000.0
    assert not svc.revoke_machine("us_autre", m.id) and svc.machines_of(acc)
    assert svc.revoke_machine(acc, m.id) and svc.machines_of(acc) == []


@requires_pg
def test_machine_load_one_row_per_machine_under_tenant_rls(pg):  # noqa: F811
    """Lot X5 : une ligne courante par machine (pas d'historique), lue par « mes machines » du compte, invisible à un
    autre locataire (RLS forcée, rôle nokker_app)."""
    import uuid

    store = PgLinkStore(pg["app"])
    svc = LinkService(store=store)
    acc, tenant = f"us_load{uuid.uuid4().hex[:8]}", f"tn_load{uuid.uuid4().hex[:8]}"
    d = svc.device(f"calc{uuid.uuid4().hex[:6]}")
    svc.approve(d["user_code"], acc, tenant)
    m = svc.machine_for_token(svc.token(d["device_code"])["machine_token"])
    assert svc.machines_with_load(acc, tenant)[0].load is None
    assert svc.report_load(m, {"ram": {"total_mb": 1024, "free_mb": 512, "source": "cgroup"}, "sessions": 1})
    assert svc.report_load(m, {"ram": {"total_mb": 1024, "free_mb": 128, "source": "cgroup"}, "sessions": 3,
                               "pirate": 1})
    [mine] = svc.machines_with_load(acc, tenant)
    assert mine.load["ram"]["free_mb"] == 128 and mine.load["sessions"] == 3 and "pirate" not in mine.load
    assert mine.load_at and mine.last_seen_at is None
    assert store.loads_of("tn_autreautre", [m.id]) == {}  # un autre locataire ne lit rien
    with psycopg.connect(pg["su"]) as conn:
        rows = conn.execute("SELECT tenant_id, load->'ram'->>'free_mb' FROM lokkal_machine_load WHERE machine_id = %s",
                            (m.id,)).fetchall()
    assert rows == [(tenant, "128")]
    with psycopg.connect(pg["app"]) as conn:  # nokker_app sans locataire : rien
        assert conn.execute("SELECT count(*) FROM lokkal_machine_load").fetchone()[0] == 0


@requires_pg
def test_gateway_login_store_on_postgres(pg):  # noqa: F811
    import base64
    import hashlib
    import uuid

    from nokker.cloud.gateway_auth import GatewayAuth, GrantError
    from nokker.cloud.link import Signer
    from nokker.cloud.pg import PgGatewayAuthStore

    svc = LinkService(store=PgLinkStore(pg["app"]))
    acc = f"us_gw{uuid.uuid4().hex[:8]}"
    d = svc.device(f"gw{uuid.uuid4().hex[:6]}")
    host = svc.approve(d["user_code"], acc, "tn_gw")["host"]
    svc.token(d["device_code"])
    ga = GatewayAuth(links=svc, signer=Signer.generate(), store=PgGatewayAuthStore(pg["app"]))
    verifier = "w" * 60
    ch = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = ga.authorize(acc, "tn_gw", host, ch)["code"]
    t1 = ga.token(code, verifier, host)
    with pytest.raises(GrantError):
        ga.token(code, verifier, host)  # usage unique
    t2 = ga.refresh(t1["refresh_token"], host)
    with pytest.raises(GrantError):
        ga.refresh(t1["refresh_token"], host)  # réutilisation → famille révoquée
    with pytest.raises(GrantError):
        ga.refresh(t2["refresh_token"], host)
    with psycopg.connect(pg["su"]) as conn:
        dump = str(conn.execute("SELECT * FROM lokkal_gateway_refresh").fetchall())
    assert t1["refresh_token"] not in dump and t2["refresh_token"] not in dump  # empreintes seulement
