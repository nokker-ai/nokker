"""Statut `executing` (migration 091) sur un vrai Postgres, store connecté en `nokker_app` (sous RLS) : migration
rejouable, colonne nullable, carte d'avant → `executing_at` None, `approved` → `executing` → `executed` relus, carte d'un
autre locataire invisible. Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import pytest

from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
from tests.integration.test_rls import A, B, pg, pod_env, requires_pg  # noqa: F401  (fixtures)

pytestmark = pytest.mark.integration


@requires_pg
def test_executing_status_roundtrip_under_rls(pg, pod_env, monkeypatch):  # noqa: F811
    import psycopg

    from nokker.approvals.pg import EXECUTING_SQL, PgApprovals
    from nokker.store.conn import migrate
    from nokker.tenancy.context import scoped_thread_id, tenant_scope

    monkeypatch.setenv("NOKKER_PG_OWNER_DSN", pg["owner"])
    assert migrate(pg["owner"], EXECUTING_SQL, rls=False, strict=True)  # idempotence : deux passages de plus
    assert migrate(pg["owner"], EXECUTING_SQL, rls=False, strict=True)
    with psycopg.connect(pg["su"]) as conn:
        col = conn.execute("SELECT data_type, is_nullable FROM information_schema.columns "
                           "WHERE table_name = 'approvals' AND column_name = 'executing_at'").fetchone()
        rls = conn.execute("SELECT relrowsecurity FROM pg_class WHERE relname = 'approvals'").fetchone()
    assert col == ("double precision", "YES") and rls == (True,)  # RLS inchangée

    approvals = PgApprovals(dsn=pg["app"])
    try:
        with tenant_scope(A):
            turn = Turn(thread_id=scoped_thread_id(A, "ex"), index=0, input=InputCheck(text="x"))
            card = approvals.request(
                turn, ToolCall(id="c1", name="patch", arguments={"path": "a"}),
                Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:write", strength="standard"),
                None, notify=False)
            assert approvals.get(card.id).executing_at is None
            approvals.decide_local(card.id, "approve", "alice")
            assert approvals.mark_executing(card.id).status == "executing"
            got = approvals.get(card.id)
            assert got.status == "executing" and got.executing_at is not None
            assert approvals.mark_executing(card.id).status == "executing"  # idempotent
            approvals.mark_executed(card.id, "ok")
            done = approvals.get(card.id)
            assert done.status == "executed" and done.executing_at == got.executing_at
            assert [t for t, _ in approvals._load_events(card.id)].count("executing") == 1
        with tenant_scope(B):
            assert approvals.get(card.id) is None  # carte d'un autre locataire : invisible
    finally:
        approvals.close()
