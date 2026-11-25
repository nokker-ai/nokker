"""Statut `interrupted` sur un vrai Postgres, store connecté en `nokker_app` (sous RLS) : carte `executing` ancienne →
`interrupted` (UPDATE conditionnel atomique, événement écrit), carte récente intacte, idempotence, jamais relancée par
l'exécuteur, carte d'un autre locataire hors de portée du balayage du locataire courant, balayeur de fond sur tous les
locataires. Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import threading

import pytest

from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, ToolResult, Turn, Verdict
from tests.integration.test_rls import A, B, pg, pod_env, requires_pg  # noqa: F401  (fixtures)

pytestmark = pytest.mark.integration


class _Clock:
    t = 2_000_000.0

    def __call__(self) -> float:
        return self.t


def _executing(approvals, tenant, name, n, alive=False):
    from nokker.tenancy.context import scoped_thread_id

    turn = Turn(thread_id=scoped_thread_id(tenant, name), index=n, input=InputCheck(text="x"))
    card = approvals.request(
        turn, ToolCall(id=f"c{n}", name="patch", arguments={"path": "a"}),
        Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:write", strength="standard"),
        None, notify=False)
    approvals.decide_local(card.id, "approve", "alice")
    out = approvals.mark_executing(card.id)
    if not alive:  # processus « mort » : plus en vol
        approvals.end_executing(card.id)
    return out


@requires_pg
def test_interrupt_stale_executing_under_rls(pg, pod_env):  # noqa: F811
    from nokker.approvals.pg import PgApprovals
    from nokker.tenancy.context import tenant_scope

    clock = _Clock()
    approvals = PgApprovals(dsn=pg["app"], clock=clock)
    try:
        with tenant_scope(A):
            old = _executing(approvals, A, "ia", 0)
        with tenant_scope(B):
            other = _executing(approvals, B, "ib", 0)
        clock.t += 600
        with tenant_scope(A):
            recent = _executing(approvals, A, "ia", 1)
        clock.t += 400  # old/other : 1000 s ; recent : 400 s

        with tenant_scope(A):  # le balayage du locataire A ne touche que A, et pas la carte récente
            assert approvals.interrupt_stale_executing() == [old.id]
            assert approvals.interrupt_stale_executing() == []  # idempotent
            got = approvals.get(old.id)
            assert got.status == "interrupted" and got.executing_at == old.executing_at
            assert approvals.get(recent.id).status == "executing"
            [(_, data)] = [e for e in approvals._load_events(old.id) if e[0] == "interrupted"]
            assert data == {"from": "executing", "age_s": 1000}
            assert [t for t, _ in approvals._load_events(old.id)] == [
                "requested", "approved", "executing", "interrupted"]
        with tenant_scope(B):
            assert approvals.get(other.id).status == "executing"  # autre locataire : intacte

        assert approvals.interrupt_stale_executing() == [other.id]  # balayeur de fond : un passage par locataire
        with tenant_scope(B):
            assert approvals.get(other.id).status == "interrupted"
        with tenant_scope(A):
            assert approvals.get(recent.id).status == "executing"
    finally:
        approvals.close()


@requires_pg
def test_two_sweepers_never_interrupt_the_same_card_twice(pg, pod_env):  # noqa: F811
    from nokker.approvals.pg import PgApprovals
    from nokker.tenancy.context import tenant_scope

    clock = _Clock()
    approvals = PgApprovals(dsn=pg["app"], clock=clock)
    second = PgApprovals(dsn=pg["app"], clock=clock)
    try:
        with tenant_scope(A):
            card = _executing(approvals, A, "race", 0)
        clock.t += 1000
        out: list[list[str]] = []

        def sweep(store):
            with tenant_scope(A):
                out.append(store.interrupt_stale_executing())

        ts = [threading.Thread(target=sweep, args=(s,)) for s in (approvals, second)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted(sum(out, [])) == [card.id]  # un seul des deux l'a passée
        with tenant_scope(A):
            assert [t for t, _ in approvals._load_events(card.id)].count("interrupted") == 1
    finally:
        approvals.close()
        second.close()


@requires_pg
def test_an_interrupted_card_is_never_rerun_by_the_executor(pg, pod_env):  # noqa: F811
    from nokker.approvals.executor import ApprovalExecutor
    from nokker.approvals.pg import PgApprovals
    from nokker.core.ports import APPROVED_LIVE
    from nokker.tenancy.context import tenant_scope
    from tests.unit.test_loop_approvals import make_kernel

    clock = _Clock()
    approvals = PgApprovals(dsn=pg["app"], clock=clock)
    try:
        k, tools = make_kernel([], approvals=approvals)
        ran: list = []
        tools.call = lambda n, a, ctx: ran.append(n) or ToolResult(call_id="", name=n, content="ok", is_error=False)
        with tenant_scope(A):
            card = _executing(approvals, A, "rerun", 0)
            clock.t += 1000
            approvals.interrupt_stale_executing()
            dead = approvals.get(card.id)
            assert dead.status == "interrupted" and dead.status not in APPROVED_LIVE
            assert ApprovalExecutor(kernel=k, approvals=approvals).run(dead) is None
            assert ran == [] and approvals.get(card.id).status == "interrupted"
            assert approvals.mark_executing(card.id).status == "interrupted"
    finally:
        approvals.close()


@requires_pg
def test_in_flight_card_is_spared_and_a_late_result_never_overwrites_interrupted(pg, pod_env):  # noqa: F811
    from nokker.approvals.pg import PgApprovals
    from nokker.tenancy.context import tenant_scope

    clock = _Clock()
    approvals = PgApprovals(dsn=pg["app"], clock=clock)
    try:
        with tenant_scope(A):
            live = _executing(approvals, A, "live", 0, alive=True)  # en vol dans ce processus
            gone = _executing(approvals, A, "gone", 1)  # processus mort, simulé
            clock.t += 5 * 3600
            swept = approvals.interrupt_stale_executing()  # la base peut garder des cartes d'autres tests du module
            assert gone.id in swept and live.id not in swept
            assert approvals.get(live.id).status == "executing"
            assert approvals.mark_executed(gone.id, "tard").status == "interrupted"
            assert approvals.mark_failed(gone.id, "tard").status == "interrupted"
            assert approvals.get(gone.id).status == "interrupted" and approvals.get(gone.id).result_summary is None
            approvals.mark_executed(live.id, "ok")
            assert approvals.get(live.id).status == "executed" and approvals._in_flight == set()
        # « redémarrage » : un nouveau store a un ensemble vide, la carte ancienne passe
        with tenant_scope(A):
            late = _executing(approvals, A, "late", 2)
        restart = PgApprovals(dsn=pg["app"], clock=clock)
        try:
            clock.t += 1000
            with tenant_scope(A):
                assert late.id in restart.interrupt_stale_executing()
        finally:
            restart.close()
    finally:
        approvals.close()
