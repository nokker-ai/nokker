"""Zones de l'app (075) sur un vrai Postgres sous RLS, connexion `nokker_app` : un locataire ne lit ni n'écrit les zones
d'un autre, l'idempotence rend le résultat gardé, l'ordre des transitions est tenu, et un remplacement garde la
dernière transition d'une zone conservée. Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401  (pg : fixture)

pytestmark = pytest.mark.integration
T0 = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)


@requires_pg
def test_pg_zone_store_rls_idempotent_ordered(pg):  # noqa: F811
    from nokker.tenancy.context import tenant_scope
    from nokker.zones.store import PgZoneStore, clean_zones

    st = PgZoneStore(pg["app"])
    zones = clean_zones({"zones": [{"id": "maison", "name": "Maison", "on_enter": ["nokker_entree_1"]},
                                   {"id": "travail", "name": "Travail"}]})
    with tenant_scope(A):
        assert [z.id for z in st.replace("us_a", zones)] == ["maison", "travail"]
        got = st.event("us_a", "maison", "enter", T0, "ev-000001", T0)
        assert got.kind == "ok" and got.zone.state == "inside" and got.zone.on_enter == ("nokker_entree_1",)
        st.set_result("us_a", "ev-000001", {"queued": True, "scenarios": [{"id": "nokker_entree_1", "status": "started"}]})
        dup = st.event("us_a", "maison", "enter", T0, "ev-000001", T0)
        assert dup.kind == "duplicate" and dup.result["scenarios"][0]["status"] == "started"
        assert st.event("us_a", "maison", "exit", T0 - timedelta(minutes=1), "ev-000002", T0).kind == "out_of_order"
        assert st.event("us_a", "absente", "enter", T0, "ev-000003", T0).kind == "unknown_zone"
        kept = st.replace("us_a", zones[:1])  # « travail » retirée, « maison » garde sa transition
        assert [(z.id, z.state) for z in kept] == [("maison", "inside")]
        assert [(acc, z.id) for acc, z in st.states()] == [("us_a", "maison")]
    with tenant_scope(B):
        assert st.zones("us_a") == [] and st.states() == []  # rien de A
        assert st.event("us_a", "maison", "enter", T0, "ev-000004", T0).kind == "unknown_zone"
    with psycopg.connect(pg["su"]) as su:
        cols = {r[0] for r in su.execute("SELECT column_name FROM information_schema.columns "
                                         "WHERE table_name IN ('member_zones', 'zone_events')").fetchall()}
    assert not cols & {"lat", "lon", "latitude", "longitude", "radius", "gps"}  # jamais de coordonnées
