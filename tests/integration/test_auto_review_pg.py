"""Relecteur automatique (docs/APPROVALS.md §8) sur un vrai Postgres : la relecture et sa révocation sont rangées sur
la carte (colonne `auto_review`, migration 051) sous la RLS du locataire. Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import pytest

from nokker.approvals.errors import ApprovalNotFound
from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
from tests.integration.test_rls import A, B, pg, pod_env, requires_pg  # noqa: F401  (fixtures)


@requires_pg
def test_auto_review_round_trip_and_isolation(pg, pod_env, monkeypatch):  # noqa: F811
    from nokker.approvals.pg import PgApprovals
    from nokker.tenancy.context import scoped_thread_id, tenant_scope

    monkeypatch.setenv("NOKKER_PG_OWNER_DSN", pg["owner"])
    approvals = PgApprovals(dsn=pg["app"])
    approvals.account_tenant = lambda u: {"us_a": A, "us_b": B}.get(u)
    try:
        with tenant_scope(A):
            turn = Turn(thread_id=scoped_thread_id(A, "t"), index=0, input=InputCheck(text="x"))
            card = approvals.request(turn, ToolCall(id="c", name="patch", arguments={"path": "a"}),
                                     Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1,
                                             rule="permissions:default", strength="standard"), None, notify=False)
            approvals.decide_auto(card.id, "reject", "hors sujet", model="m", effect="write")
            got = approvals.get(card.id)
            assert got.status == "rejected" and got.decided_via == "auto-review"
            assert got.auto_review["decision"] == "reject" and got.auto_review["model"] == "m"
            assert approvals.revoke_auto_review(card.id, "us_a").status == "pending"
            assert approvals.get(card.id).auto_review["revoked_by"] == "us_a"
            approvals.record_rollback(card.id, "us_a", checkpoint_id=card.id, restored=["a"], conflicts=[])
        import psycopg

        with psycopg.connect(pg["su"]) as conn:
            row = conn.execute("SELECT data->>'by', data->'restored', data->'conflicts' FROM approval_events "
                               "WHERE approval_id = %s AND event_type = 'auto_review_rollback'", (card.id,)).fetchone()
        assert row == ("us_a", ["a"], [])
        with tenant_scope(B):
            assert approvals.get(card.id) is None
            with pytest.raises(ApprovalNotFound):
                approvals.decide_auto(card.id, "approve", "x")
            with pytest.raises(ApprovalNotFound):  # un autre locataire n'écrit rien au journal de la carte
                approvals.record_rollback(card.id, "us_b", checkpoint_id=card.id, restored=[], conflicts=[])
    finally:
        approvals.close()
