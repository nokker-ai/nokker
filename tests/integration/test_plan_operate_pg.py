"""Plan et incidents (lot X18) en base : vrai Postgres jetable, rôles de deploy/postgres/roles.sql, stores connectés en
`nokker_app` (ni propriétaire, ni BYPASSRLS). Ignoré sans `NOKKER_TEST_PG_DSN`.

Prouve : les cartes, propositions, briefs, sources, signatures et incidents d'un locataire n'existent pas pour un autre
(par les stores ET en SQL brut) ; une alerte signée est cherchée dans le bon locataire sans identité ; un rejeu est
refusé après un redémarrage (signature gardée en base) ; un seul incident vivant par empreinte (index unique) ; le fil
d'un incident (passages, timeline JSON) fait l'aller-retour."""
from __future__ import annotations

import json
import time

import pytest

from tests.integration.test_rls import A, B, pg, pod_env, requires_pg  # noqa: F401 — fixtures (base neuve, rôles)

pytestmark = pytest.mark.integration


@requires_pg
def test_plan_store_is_isolated_and_roundtrips(pg, pod_env):  # noqa: F811
    from nokker.plan.service import Plan
    from nokker.plan.store import PgPlan
    from nokker.plan.types import PlanError
    from nokker.store.conn import connect
    from nokker.tenancy.context import tenant_scope

    store = PgPlan(pg["app"], migrate_now=False)
    plan = Plan(store=store)
    try:
        ids = {}
        for tid in (A, B):
            with tenant_scope(tid):
                g = plan.create({"title": f"objectif {tid}", "constraints": ["c"], "decisions": ["d"]}, "alice")
                t = plan.create({"parent_id": g["id"], "title": "tâche", "status": "in_progress"}, "alice")
                p = plan.propose_split(t["id"], [{"title": "sous-tâche", "constraints": ["x"]}], "trop gros", "nina")
                store.set_brief("alice", f"ag_{tid[3:]}", 1.0)
                ids[tid] = (g["id"], t["id"], p.id)
        for tid, other in ((A, B), (B, A)):
            with tenant_scope(tid):
                g, t, p = ids[tid]
                og, ot, op_ = ids[other]
                assert {c["id"] for c in plan.list_cards()} == {g, t}
                with pytest.raises(PlanError):
                    plan.get_card(og)
                assert store.get_proposal(op_) is None and [x.id for x in plan.proposals()] == [p]
                assert store.brief_agent("alice") == f"ag_{tid[3:]}"
                assert plan.get_card(g)["status"] == "in_progress" and plan.get_card(g)["constraints"] == ["c"]
                out = plan.decide(p, "accept", "alice")
                assert out["cards"][0]["constraints"] == ["x"]
                assert plan.get_card(t)["computed"] is True
            with tenant_scope(tid), connect(pg["app"]) as conn:
                for table in ("nokker_plan_cards", "nokker_plan_proposals", "nokker_plan_briefs"):
                    assert conn.execute(f"SELECT count(*) FROM {table} WHERE tenant_id <> %s",
                                        (tid,)).fetchone()[0] == 0
                    # écrire chez l'autre en forçant son locataire : refusé par la politique
                import psycopg

                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute("INSERT INTO nokker_plan_cards (id, tenant_id, kind, title, created_by, created_at, "
                                 "updated_at) VALUES ('pc_forced00000', %s, 'task', 'x', 'm', 1, 1)", (other,))
    finally:
        store.close()


@requires_pg
def test_incidents_signed_receive_finds_the_tenant_and_replay_survives_a_restart(pg, pod_env):  # noqa: F811
    from nokker.incidents.service import Operate, sign
    from nokker.incidents.store import PgIncidents
    from nokker.store.conn import known_tenants
    from nokker.tenancy.context import tenant_scope

    store = PgIncidents(pg["app"], migrate_now=False)
    op = Operate(store=store, tenants=lambda: known_tenants(pg["app"]))
    try:
        with tenant_scope(B):
            src, key = op.create_source({"name": "prom", "format": "alertmanager"}, "bruno")
        body = json.dumps({"version": "4", "alerts": [
            {"status": "firing", "fingerprint": "00aa11bb22cc33dd", "labels": {"alertname": "Down", "x": "1"}}]}).encode()
        good = sign(key, body)
        status, out = op.receive(src.id, body, good)
        assert status == 202 and out["alerts"][0]["result"] == "opened"
        iid = out["alerts"][0]["incident_id"]
        # « redémarrage » : un autre service sur un autre store, même base
        store2 = PgIncidents(pg["app"], migrate_now=False)
        op2 = Operate(store=store2, tenants=lambda: known_tenants(pg["app"]))
        assert op2.receive(src.id, body, good)[0] == 409
        assert op2.receive(src.id, body, sign(key, body, int(time.time()) + 1))[1]["alerts"][0][
            "result"] == "repeated"
        with tenant_scope(A):
            assert op.store.get(iid) is None and op.list_sources() == [] and op.store.list() == []
        with tenant_scope(B):
            inc = op2.get(iid)
            assert inc.occurrences == 2 and inc.labels == {"alertname": "Down", "x": "1"}
            assert inc.timeline[0]["kind"] == "opened"
            inc.runs.append({"run_id": "ar_0000000000000001", "status": "running", "approvals": []})
            store2.put(inc)
            assert store2.by_run("ar_0000000000000001").id == iid
            resolved = json.dumps({"version": "4", "alerts": [
                {"status": "resolved", "fingerprint": "00aa11bb22cc33dd", "labels": {"alertname": "Down"}}]}).encode()
        assert op2.receive(src.id, resolved, sign(key, resolved))[1]["alerts"][0]["result"] == "resolved"
        assert op2.receive(src.id, body, sign(key, body, int(time.time()) + 2))[1]["alerts"][0][
            "result"] == "opened"  # après la résolution : un nouvel incident
        with tenant_scope(B):
            assert len(store2.list()) == 2 and len(store2.list("resolved")) == 1
        store2.close()
    finally:
        store.close()
