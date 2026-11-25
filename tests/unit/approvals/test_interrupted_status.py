"""Statut `interrupted` : une carte `executing` depuis plus de 15 min (processus mort pendant l'outil) passe `interrupted`,
statut FINAL, jamais relancé (au plus une exécution), au démarrage comme au balayage. Idempotent, atomique, événement sans
donnée de la carte, locataires séparés, réglage borné."""
from __future__ import annotations

import pytest

from nokker.approvals.base import EXECUTING_STALE_S, MIN_EXECUTING_STALE_S
from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.ports import APPROVED_LIVE
from nokker.core.turn import Decision, Gate, InputCheck, ToolResult, Turn, Verdict
from nokker.tenancy.context import scoped_thread_id, tenant_scope
from tests.unit.test_loop_approvals import make_kernel, tc  # noqa: F401

TA, TB = "tn_aaaaaaaa", "tn_bbbbbbbb"


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def _card(backend, thread="fil", n=0):
    turn = Turn(thread_id=thread, index=n, input=InputCheck(text="x"))
    return backend.request(turn, tc("infra_lvextend", size="1G"),
                           Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:write",
                                   strength="standard"), None, notify=False)


def _executing(backend, thread="fil", n=0, alive=False):
    """Carte `executing` ; par défaut son processus est « mort » (plus en vol), `alive=True` : l'outil tourne ici."""
    card = _card(backend, thread, n)
    backend.decide_local(card.id, "approve", "alice")
    out = backend.mark_executing(card.id)
    if not alive:
        backend.end_executing(card.id)
    return out


def test_a_stale_executing_card_becomes_interrupted_and_a_recent_one_is_left_alone():
    clock = Clock()
    b = InMemoryApprovals(clock=clock)
    old = _executing(b, n=0)
    clock.t += 600
    recent = _executing(b, n=1)
    clock.t += 400  # old : 1000 s, recent : 400 s
    assert b.interrupt_stale_executing() == [old.id]
    assert b.get(old.id).status == "interrupted" and b.get(recent.id).status == "executing"
    assert b.get(old.id).executing_at == old.executing_at


def test_idempotent_and_one_event_without_card_data():
    clock = Clock()
    events: list = []
    b = InMemoryApprovals(clock=clock, on_event=lambda t, d: events.append((t, d)))
    card = _executing(b)
    clock.t += EXECUTING_STALE_S + 5
    assert b.interrupt_stale_executing() == [card.id]
    assert b.interrupt_stale_executing() == [] and b.interrupt_stale_executing(now=clock.t + 10_000) == []
    [(_, data)] = [e for e in b._load_events(card.id) if e[0] == "interrupted"]
    assert data == {"from": "executing", "age_s": int(EXECUTING_STALE_S) + 5}  # ancienneté et ancien statut, rien d'autre
    assert [d["approval_id"] for t, d in events if t == "approval.interrupted"] == [card.id]
    assert "1G" not in repr(data) and "infra_lvextend" not in repr(data)


def test_the_delay_is_floored_and_overridable_with_now():
    clock = Clock()
    b = InMemoryApprovals(clock=clock)
    card = _executing(b)
    clock.t += MIN_EXECUTING_STALE_S - 1
    assert b.interrupt_stale_executing(older_than_s=1) == []  # plancher : jamais sous 60 s
    assert b.interrupt_stale_executing(older_than_s=1, now=clock.t + 5) == [card.id]


def test_interrupted_is_final_never_rerun_and_not_live():
    clock = Clock()
    b = InMemoryApprovals(clock=clock)
    k, tools = make_kernel([], approvals=b)
    ran: list = []
    tools.call = lambda n, a, ctx: ran.append(n) or ToolResult(call_id="", name=n, content="ok", is_error=False)
    executor = ApprovalExecutor(kernel=k, approvals=b)
    card = _executing(b)
    clock.t += 1000
    b.interrupt_stale_executing()
    dead = b.get(card.id)
    assert dead.status == "interrupted" and "interrupted" not in APPROVED_LIVE
    assert executor.run(dead) is None and ran == []
    assert b.mark_executing(card.id).status == "interrupted"  # plus `approved` : intouchée
    with pytest.raises(Exception):  # une décision ne la rouvre pas (elle n'est plus `pending`)
        b.decide_local(card.id, "approve", "alice")
    assert b.get(card.id).status == "interrupted" and ran == []


def test_final_status_is_recognised_by_every_status_list():
    from nokker.approvals.suggestions import APPROVED
    from nokker.local.remote_approvals import _FINAL
    from nokker.traces.export import APPROVAL_STATES

    assert "interrupted" in APPROVED and _FINAL["interrupted"] == "approved" and APPROVAL_STATES["interrupted"] == "accepted"
    from nokker.bubbles.hub import BubbleHub

    assert BubbleHub._card_bubble_status("interrupted") == "failed"


def test_host_json_carries_the_status():
    from nokker.api.server import approval_to_json

    clock = Clock()
    b = InMemoryApprovals(clock=clock)
    card = _executing(b)
    clock.t += 1000
    b.interrupt_stale_executing()
    j = approval_to_json(b.get(card.id))
    assert j["status"] == "interrupted" and j["executing_at"]


def test_a_sweep_in_one_tenant_leaves_the_other_tenant_untouched():
    clock = Clock()
    b = InMemoryApprovals(clock=clock)
    with tenant_scope(TA):
        a = _executing(b, scoped_thread_id(TA, "fa"))
    with tenant_scope(TB):
        other = _executing(b, scoped_thread_id(TB, "fb"))
    clock.t += 1000
    with tenant_scope(TA):
        assert b.interrupt_stale_executing() == [a.id]
    with tenant_scope(TB):
        assert b.get(other.id).status == "executing"
    assert b.interrupt_stale_executing() == [other.id]  # balayeur de fond : tous les locataires


def test_build_approvals_reads_and_floors_the_config_delay():
    from nokker.api.server import build_approvals
    from nokker.config import NokkerConfig

    assert build_approvals(NokkerConfig()).executing_stale_s == EXECUTING_STALE_S
    assert build_approvals(NokkerConfig(approvals={"executing_stale_s": 1800})).executing_stale_s == 1800
    assert build_approvals(NokkerConfig(approvals={"executing_stale_s": 5})).executing_stale_s == MIN_EXECUTING_STALE_S


def test_the_background_sweeper_interrupts_and_stops():
    from nokker.api.server import _NotifySweeper

    clock = Clock()
    b = InMemoryApprovals(clock=clock)
    card = _executing(b)
    clock.t += 1000
    sw = _NotifySweeper(None, 0.01, b)
    sw.sweep()
    assert b.get(card.id).status == "interrupted"
    sw.start()
    sw.stop()
    sw.stop()
    assert not sw._thread.is_alive()


def test_a_slow_tool_in_flight_is_never_interrupted_and_is_released_after_success_and_exception():
    clock = Clock()
    b = InMemoryApprovals(clock=clock)
    k, tools = make_kernel([], approvals=b)
    executor = ApprovalExecutor(kernel=k, approvals=b)
    card = _card(b)
    decided = b.decide_local(card.id, "approve", "alice")
    seen: dict = {}

    def slow(n, a, ctx):
        clock.t += 5 * 3600  # l'outil dure 5 h (horloge simulée)
        seen["swept"] = b.interrupt_stale_executing()
        seen["status"] = b.get(ctx.approval_id).status
        seen["flying"] = set(b._in_flight)
        return ToolResult(call_id="", name=n, content="ok", is_error=False)

    tools.call = slow
    executor.run(decided)
    assert seen == {"swept": [], "status": "executing", "flying": {card.id}}
    assert b.get(card.id).status == "executed" and b._in_flight == set()

    card2 = _card(b, n=1)
    d2 = b.decide_local(card2.id, "approve", "alice")

    def boom(n, a, ctx):
        raise RuntimeError("boom")

    tools.call = boom
    executor.run(d2)
    assert b.get(card2.id).status == "failed" and b._in_flight == set()


def test_the_flight_set_is_released_even_when_the_tool_raises_a_base_exception():
    clock = Clock()
    b = InMemoryApprovals(clock=clock)
    k, tools = make_kernel([], approvals=b)
    card = _card(b)
    d = b.decide_local(card.id, "approve", "alice")

    def cancel(n, a, ctx):
        raise KeyboardInterrupt

    tools.call = cancel
    with pytest.raises(KeyboardInterrupt):
        ApprovalExecutor(kernel=k, approvals=b)._call_approved(d)
    assert b._in_flight == set()


def test_a_late_result_never_overwrites_interrupted():
    clock = Clock()
    b = InMemoryApprovals(clock=clock)
    card = _executing(b)
    clock.t += 1000
    b.interrupt_stale_executing()
    assert b.mark_executed(card.id, "tard").status == "interrupted"
    assert b.mark_failed(card.id, "tard").status == "interrupted"
    got = b.get(card.id)
    assert got.status == "interrupted" and got.result_summary is None and got.error is None
    assert [t for t, _ in b._load_events(card.id)][-1] == "interrupted"


def test_at_startup_the_set_is_empty_old_cards_go_and_recent_ones_stay():
    clock = Clock()
    b = InMemoryApprovals(clock=clock)
    old = _executing(b, n=0)
    clock.t += 800
    recent = _executing(b, n=1)
    clock.t += 200
    b2 = InMemoryApprovals(clock=clock)  # « redémarrage » : ensemble vide, même stockage
    b2._approvals = b._approvals
    assert b2._in_flight == set()
    assert b2.interrupt_stale_executing() == [old.id]
    assert b2.get(recent.id).status == "executing"
