"""Statut `executing` d'une carte (migration 091) : posé entre `approved` et `executed`/`failed` PENDANT que l'outil tourne,
visible dans l'API de l'hôte (JSON de la carte, journal d'événements, flux `approval.executing`), jamais deux fois."""
from __future__ import annotations

from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.turn import ToolResult
from tests.unit.test_loop_approvals import make_kernel, tc  # noqa: F401


def _setup(fail=False):
    backend = InMemoryApprovals()
    seen: list = []
    events: list = []
    backend.on_event = lambda t, d: events.append((t, d))
    k, tools = make_kernel([], approvals=backend)

    def call(n, a, ctx):
        seen.append((backend.get(ctx.approval_id).status, backend.get(ctx.approval_id).executing_at))
        return ToolResult(call_id="", name=n, content="boom" if fail else "ok", is_error=fail)

    tools.call = call
    return k, tools, backend, ApprovalExecutor(kernel=k, approvals=backend), seen, events


def _card(k, backend):
    from nokker.core.turn import Decision, Gate, InputCheck, Turn, Verdict

    turn = Turn(thread_id="fil", index=0, input=InputCheck(text="x"))
    return backend.request(turn, tc("infra_lvextend", size="1G"),
                           Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:write",
                                   strength="standard"), None, notify=False)


def test_tool_runs_while_the_card_is_executing_then_executed():
    k, tools, backend, executor, seen, events = _setup()
    card = _card(k, backend)
    executor.run(backend.decide_local(card.id, "approve", "alice"))
    assert seen[0][0] == "executing" and seen[0][1] is not None  # l'outil voit la carte `executing`
    done = backend.get(card.id)
    assert done.status == "executed" and done.executing_at == seen[0][1]
    assert [t for t, _ in backend._load_events(card.id)] == ["requested", "approved", "executing", "executed"]
    assert [d["approval_id"] for t, d in events if t == "approval.executing"] == [card.id]


def test_failed_tool_goes_executing_then_failed():
    k, tools, backend, executor, seen, _ = _setup(fail=True)
    card = _card(k, backend)
    executor.run(backend.decide_local(card.id, "approve", "alice"))
    assert seen[0][0] == "executing" and backend.get(card.id).status == "failed"


def test_an_executing_card_is_never_run_twice():
    k, tools, backend, executor, seen, _ = _setup()
    card = _card(k, backend)
    decided = backend.decide_local(card.id, "approve", "alice")
    stuck = backend.mark_executing(card.id)
    assert stuck.status == "executing"
    assert backend.mark_executing(card.id).status == "executing"  # idempotent : pas de 2e événement
    assert sum(1 for t, _ in backend._load_events(card.id) if t == "executing") == 1
    assert executor.run(stuck) is None and executor.run(decided) is None and seen == []


def test_host_json_exposes_the_status_and_its_instant():
    from nokker.api.server import approval_to_json

    k, tools, backend, executor, seen, _ = _setup()
    card = _card(k, backend)
    backend.decide_local(card.id, "approve", "alice")
    j = approval_to_json(backend.mark_executing(card.id))
    assert j["status"] == "executing" and j["executing_at"]
    assert approval_to_json(backend.get(card.id))["executing_at"] == j["executing_at"]
    assert approval_to_json(_card(k, backend))["executing_at"] is None


def test_migration_091_is_idempotent_and_adds_only_a_nullable_column():
    from nokker.store.pg import migration_files

    sql = next(p for p in migration_files() if p.name == "091_approval_executing.sql").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    assert "ADD COLUMN IF NOT EXISTS executing_at DOUBLE PRECISION;" in code
    assert "ENABLE ROW LEVEL SECURITY" not in code and "DROP TABLE" not in code and "DROP COLUMN" not in code


def test_tools_that_recheck_their_card_during_execution_accept_executing():
    from nokker.core.ports import APPROVED_LIVE

    assert APPROVED_LIVE == ("approved", "executing")  # un outil relit sa carte pendant l'appel : elle est `executing`
    k, tools, backend, executor, seen, _ = _setup()
    card = _card(k, backend)
    executor.run(backend.decide_local(card.id, "approve", "alice"))
    assert seen[0][0] in APPROVED_LIVE
