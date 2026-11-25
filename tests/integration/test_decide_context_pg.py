"""Décideur de l'hôte (`on_behalf_of`, `via`) relu au journal `approval_events` sur un vrai Postgres, store connecté en
`nokker_app` (sous RLS) : aucune colonne ni migration, la carte garde `decided_by` = le jeton local. Ignoré sans
`NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import pytest

from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
from tests.integration.test_rls import A, B, pg, pod_env, requires_pg  # noqa: F401  (fixtures)

pytestmark = pytest.mark.integration


@requires_pg
def test_host_decider_is_journaled_and_read_back_under_rls(pg, pod_env, monkeypatch):  # noqa: F811
    from nokker.approvals.pg import PgApprovals
    from nokker.tenancy.context import scoped_thread_id, tenant_scope

    monkeypatch.setenv("NOKKER_PG_OWNER_DSN", pg["owner"])
    approvals = PgApprovals(dsn=pg["app"])
    try:
        with tenant_scope(A):
            turn = Turn(thread_id=scoped_thread_id(A, "dc"), index=0, input=InputCheck(text="x"))
            card = approvals.request(
                turn, ToolCall(id="c1", name="patch", arguments={"path": "a"}),
                Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="permissions:default",
                        strength="standard"), None, notify=False)
            plain = approvals.request(
                turn, ToolCall(id="c2", name="patch", arguments={"path": "b"}),
                Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="permissions:default",
                        strength="standard"), None, notify=False)
            done = approvals.decide_local(card.id, "approve", "local-owner", on_behalf_of="us_marie", via="teams")
            assert done.decided_by == "local-owner" and done.decided_via == "local-token"
            assert approvals.decision_context(approvals.get(card.id)) == {"on_behalf_of": "us_marie", "via": "teams"}
            approvals.decide_local(plain.id, "reject", "local-owner")
            assert approvals.decision_context(approvals.get(plain.id)) == {}
        with tenant_scope(B):  # un autre locataire ne voit rien de ce journal
            assert approvals._load_events(card.id) == []
    finally:
        approvals.close()
