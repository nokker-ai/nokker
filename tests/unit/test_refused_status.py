"""Refus d'une porte : ce que lit le MODÈLE ne change pas (`is_error=False` : un refus n'est ni un échec d'outil, qui
fait monter le palier, ni une erreur répétée) ; ce que voit l'HÔTE (événement `tool.result`, réponse de `tools/call`)
porte en plus `status: "refused"` et `reason`. Un succès et une erreur d'outil restent tels quels."""
from __future__ import annotations

from nokker.api.events import EventBus
from nokker.api.server import build_hub
from nokker.config import NokkerConfig
from nokker.core.turn import InputCheck, Message, ToolCall, ToolResult, Turn, TurnInput
from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates, Rule
from tests.unit.test_fixes_e2e_contracts import _req
from tests.unit.test_local_tool_call import _server
from tests.unit.test_loop_contract import kernel, tc


def _hub():
    hub = build_hub(NokkerConfig(web_search_enabled=False, approvals_enabled=False), EventBus())
    seen: list[tuple[str, dict]] = []
    hub.publish = lambda name, data: seen.append((name, data))
    return hub, seen


def _run(permissions):
    k, tools, _ship, _ = kernel([Message("assistant", tool_calls=[tc("infra_lvextend")]), Message("assistant", "ok")])
    k.gates = PermissionGates(inner=k.gates, permissions=permissions)
    hub, seen = _hub()
    results = []

    class Obs:
        def __getattr__(self, name):
            return getattr(hub, name)

        def tool_result(self, turn, call, result, ms):
            results.append(result)
            hub.tool_result(turn, call, result, ms)

    k.observer = Obs()
    turn = k.run_turn(TurnInput("t1", "agrandis le disque"))
    assert tools.called == []
    events = [d for n, d in seen if n == "tool.result"]
    return turn, results, events


def _assert_refusal(turn, results, events, reason):
    assert results[0].is_error is False and results[0].refused is True  # le modèle : inchangé
    tool_msg = next(m for m in turn.messages if m.role == "tool")
    assert tool_msg.content.startswith("Refusé : ") and "refused" not in tool_msg.content
    assert len(events) == 1 and events[0]["status"] == "refused" and events[0]["reason"] == reason


def test_plan_mode_refusal_is_a_refusal_for_the_host():
    _assert_refusal(*_run(LocalPermissions(mode=Mode.PLAN)), "plan_mode")


def test_deny_rule_refusal_is_a_refusal_for_the_host():
    _assert_refusal(*_run(LocalPermissions(mode=Mode.AUTO, deny=[Rule.parse("infra_lvextend")])), "deny_rule")


def test_gate_without_a_code_falls_back_to_the_rule_name():
    k, tools, _ship, _ = kernel([Message("assistant", tool_calls=[tc("send_bulk_email")]), Message("assistant", "ok")])
    hub, seen = _hub()
    k.observer = hub
    k.run_turn(TurnInput("t1", "envoie"))
    ev = next(d for n, d in seen if n == "tool.result")
    assert tools.called == [] and ev["status"] == "refused" and ev["reason"]


def test_success_and_tool_error_events_are_unchanged():
    hub, seen = _hub()
    turn, call = Turn(thread_id="th_x", index=0, input=InputCheck(text="x")), ToolCall(id="c1", name="t", arguments={})
    hub.tool_result(turn, call, ToolResult(call_id="c1", name="t", content="ok"), 3)
    hub.tool_result(turn, call, ToolResult(call_id="c1", name="t", content="boum", is_error=True), 3)
    ok, err = seen[0][1], seen[1][1]
    assert ok["ok"] is True and ok["is_error"] is False and "status" not in ok and "reason" not in ok
    assert err["ok"] is False and err["is_error"] is True and "status" not in err


def test_direct_call_refusal_carries_the_status(tmp_path):
    httpd, port, api, kernel_, _, _, _l3, _, thread = _server(tmp_path)
    try:
        kernel_.gates = PermissionGates(inner=kernel_.gates, permissions=LocalPermissions(mode=Mode.PLAN))
        status, body = _req(port, "/v1/local/tools/call", token=api.token,
                            body={"thread_id": thread, "name": "checkpoint_create", "arguments": {"reason": "x"}})
        assert status == 200 and body["is_error"] and body["status"] == "refused" and body["reason"] == "plan_mode"
    finally:
        httpd.shutdown()
