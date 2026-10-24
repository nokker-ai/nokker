"""`PolicyV0` — règles fixes de PLAN-P1 §2.6, aucune dépendance externe."""
from __future__ import annotations

from nokker.core.ports import RouteState
from nokker.core.turn import Decision, Gate, Tier, Turn, Verdict
from nokker.route.policy import PolicyV0


def turn():
    from nokker.core.turn import InputCheck
    return Turn(thread_id="t", index=0, input=InputCheck(text="x"))


def test_default_is_ship_without_reasoning():
    d = PolicyV0().choose(RouteState(turn=turn(), call_index=0))
    assert d.tier is Tier.SHIP and d.reasoning is False and d.reason


def test_redo_deep_verdict_routes_to_deep():
    state = RouteState(turn=turn(), call_index=1,
                       switch_verdicts=[Verdict(gate=Gate.SWITCH, decision=Decision.REDO_DEEP, layer=2,
                                                rule="doute")])
    d = PolicyV0().choose(state)
    assert d.tier is Tier.DEEP and "REDO_DEEP" in d.reason


def test_tool_failures_threshold_routes_to_deep():
    state = RouteState(turn=turn(), call_index=4, tool_failures=3)
    d = PolicyV0(deep_tool_failures=3).choose(state)
    assert d.tier is Tier.DEEP


def test_tool_failures_below_threshold_stays_ship():
    state = RouteState(turn=turn(), call_index=1, tool_failures=2)
    d = PolicyV0(deep_tool_failures=3).choose(state)
    assert d.tier is Tier.SHIP


def test_repeated_error_routes_to_deep():
    state = RouteState(turn=turn(), call_index=2, repeated_error=True)
    d = PolicyV0().choose(state)
    assert d.tier is Tier.DEEP


def test_deep_reasoning_configurable():
    state = RouteState(turn=turn(), call_index=2, repeated_error=True)
    d = PolicyV0(deep_reasoning=False).choose(state)
    assert d.reasoning is False


def test_reason_always_set():
    for state in (RouteState(turn=turn(), call_index=0),
                  RouteState(turn=turn(), call_index=1, tool_failures=5)):
        assert PolicyV0().choose(state).reason
