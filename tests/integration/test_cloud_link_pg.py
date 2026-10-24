"""Lien Lokkal côté nokker.ai sur un vrai Postgres (migration 014, nokker/cloud/pg.py), avec le rôle du pod
(`nokker_app`) : flux complet, empreintes seulement en base, révocation, effacement du compte couvre les machines."""
from __future__ import annotations

import psycopg

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
