"""Audit des connexions Postgres par locataire : 300 locataires successifs (tenant_scope, écritures ledger, bulles,
connexions courtes de chaque famille de store, chemins d'erreur) sur un vrai Postgres, rôle `nokker_app`. Le nombre de
connexions de l'application (`pg_stat_activity`) et les descripteurs de fichiers du processus doivent rester bornés :
une connexion ouverte par locataire et jamais fermée serait vue ici. Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import gc
import os

import pytest

from tests.integration.test_rls import pg, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

pytestmark = pytest.mark.integration

N = 300
SLACK_FD = 12  # marge : sockets de rappel, caches du processus de test


def _fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def _app_conns(su_dsn: str) -> int:
    import psycopg

    with psycopg.connect(su_dsn, autocommit=True) as su:
        return su.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                          "AND usename = 'nokker_app' AND pid <> pg_backend_pid()").fetchone()[0]


def _one_tenant(tid, ledger, bubbles, persona, egress, usage, call, consol=None, approvals=None):
    from nokker.store.conn import connect, connect_admin, connect_key, connect_tenant
    from nokker.tenancy.context import NoTenant, tenant_scope

    with tenant_scope(tid):
        ledger.record(f"th_{tid}", 0, call)
        b = bubbles.create(thread_id=f"th_{tid}", kind="timer", title="t")
        assert bubbles.get(b.id) is not None
        persona.put(f"us_{tid}", tid, {"name": "Ada", "voice_id": "nina", "avatar_id": "nina", "avatar_version": 1}, 1.0)
        assert persona.get(f"us_{tid}")["name"] == "Ada"
        egress.put_hosts(f"us_{tid}", ["example.org"])
        with connect(persona.dsn) as c:
            c.execute("SELECT 1")
        with pytest.raises(RuntimeError), connect(persona.dsn) as c:  # erreur dans le `with` : fermée quand même
            c.execute("SELECT 1")
            raise RuntimeError("boom")
    assert usage.take(tid, f"us_{tid}", __import__("datetime").date.today(), KIND, 10**6)
    with connect_tenant(persona.dsn, tid) as c:
        c.execute("SELECT 1")
    with connect_key(persona.dsn, "k") as c:
        c.execute("SELECT 1")
    with connect_admin(persona.dsn) as c:
        c.execute("SELECT 1")
    if consol is not None:
        from nokker.memory.consolidation.engine import Actor

        actor = Actor(account_id=f"us_{tid}", tenant_id=tid)
        with tenant_scope(tid):
            with consol.lock(actor) as got:
                assert got
            with consol.transaction(actor) as tx:
                assert tx is not None
            with pytest.raises(RuntimeError), consol.transaction(actor):  # erreur : rollback puis fermeture
                raise RuntimeError("boom")
            assert approvals._list_pending_approvals(None) == []
    with pytest.raises(NoTenant):  # hors scope : rien d'ouvert
        connect(persona.dsn)


KIND = "turns"


@requires_pg
def test_connections_and_fds_stay_bounded_across_300_tenants(pg):  # noqa: F811
    import psycopg

    from nokker.bubbles.pg import PgBubbles
    from nokker.cloud.egress import PgEgressStore
    from nokker.core.turn import LLMCall, Tier, Usage
    from nokker.ledger.costs import PostgresLedger
    from nokker.approvals.pg import PgApprovals
    from nokker.memory.consolidation.stores import PgConsolidationStore
    from nokker.persona import PgPersonaStore
    from nokker.tenancy.limits import PgUsage

    tenants = [f"tn_audit{i:06d}" for i in range(N)]
    with psycopg.connect(pg["su"], autocommit=True) as su:
        with su.cursor() as cur:
            cur.executemany("INSERT INTO tenants (id) VALUES (%s) ON CONFLICT DO NOTHING", [(t,) for t in tenants])
    call = LLMCall(tier=Tier.SHIP, model="m", reasoning=False, prefix_sha256="x", layer_tokens={},
                   usage=Usage(prompt=10, completion=1, cached=0, cached_reported=True), latency_s=0.1,
                   route_reason="audit")
    ledger = PostgresLedger(pg["app"], migrate=False)
    bubbles = PgBubbles(dsn=pg["app"])  # une connexion longue pour TOUS les locataires
    persona, egress, usage = PgPersonaStore(pg["app"]), PgEgressStore(pg["app"]), PgUsage(pg["app"])
    consol = PgConsolidationStore(pg["app"], migrate_now=False)
    approvals = PgApprovals(dsn=pg["app"])  # deux connexions longues (locataire + authentification), pas par locataire
    try:
        for t in tenants[:5]:  # échauffement : caches d'import, connexions longues des stores
            _one_tenant(t, ledger, bubbles, persona, egress, usage, call, consol, approvals)
        gc.collect()
        base_conns, base_fds = _app_conns(pg["su"]), _fds()
        peak_conns, peak_fds = base_conns, base_fds
        for i, t in enumerate(tenants[5:]):
            _one_tenant(t, ledger, bubbles, persona, egress, usage, call, consol, approvals)
            if i % 25 == 0:
                peak_conns, peak_fds = max(peak_conns, _app_conns(pg["su"])), max(peak_fds, _fds())
        gc.collect()
        end_conns, end_fds = _app_conns(pg["su"]), _fds()
    finally:
        bubbles.close()
        approvals.close()
    assert base_conns <= 4  # connexions longues des bulles et des approbations (3), rien par locataire
    assert end_conns == base_conns and peak_conns <= base_conns + 1, (base_conns, peak_conns, end_conns)
    assert end_fds <= base_fds + SLACK_FD and peak_fds <= base_fds + SLACK_FD, (base_fds, peak_fds, end_fds)
