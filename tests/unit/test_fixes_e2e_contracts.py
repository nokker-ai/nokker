"""Contrats demandés par le web après l'e2e du 1.11 (branche p2/fixes-e2e) — tous en champs EN PLUS côté API et
événements ; le texte vu par le modèle (résultats d'outils, raisons des points de retour) ne change pas (gel de la
session d'entraînement) :

- D7 : `turn.done` porte `error: {code, message}` quand le tour échoue, `refused: true` quand un effet a été refusé
  d'office (règle deny, mode plan, carte rejetée par un client sans surveillance), `cancelled: true` sur annulation ;
- D8 : `reason_code` stable à côté du texte des raisons de points de retour (événement, réponses d'appel direct) ;
- (a) mode d'une session locale : `GET|PUT /v1/local/sessions/{fil}/mode`, événement `session.mode` ;
- (b) `todo.updated {thread_id, items}` quand le modèle écrit le plan de travail ;
- (c) refus du mode plan : `reason: "plan_mode"` sur `tool.result` et la réponse de `tools/call`.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from nokker.api.events import EventBus
from nokker.api.server import build_hub
from nokker.approvals.memory import InMemoryApprovals
from nokker.config import NokkerConfig
from nokker.core.ports import ToolContext
from nokker.core.turn import (
    Decision,
    Effect,
    Gate,
    InputCheck,
    Message,
    Tier,
    ToolCall,
    ToolResult,
    Turn,
    TurnInput,
    Verdict,
)
from nokker.local.api import LOCAL_OWNER
from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates
from nokker.tools import checkpoints as ck
from tests.unit.test_local_tool_call import _server
from tests.unit.test_loop_contract import kernel, tc

PLAN_TEXT = "REFUSÉ par le noyau (permissions:plan: mode plan : lecture seule). Ne pas réessayer autrement."


def _req(port, path, *, token=None, body=None, method=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _hub():
    hub = build_hub(NokkerConfig(web_search_enabled=False, approvals_enabled=False), EventBus())
    seen: list[tuple[str, dict]] = []
    hub.publish = lambda name, data: seen.append((name, data))
    return hub, seen


def _turn(**kw) -> Turn:
    return Turn(thread_id="th_x", index=0, input=InputCheck(text="x"), **kw)


# ─────────────── (c) refus du mode plan ───────────────


def test_plan_refusal_keeps_the_model_text_and_adds_reason_for_clients():
    k, tools, ship, _ = kernel([Message("assistant", tool_calls=[tc("infra_lvextend")]), Message("assistant", "ok")])
    k.gates = PermissionGates(inner=k.gates, permissions=LocalPermissions(mode=Mode.PLAN))
    results = []

    class Obs:
        def __getattr__(self, name):
            return lambda *a, **kw: None

        def tool_result(self, turn, call, result, ms):
            results.append(result)

    k.observer = Obs()
    turn = k.run_turn(TurnInput("t1", "agrandis le disque"))
    assert tools.called == []
    tool_msg = next(m for m in turn.messages if m.role == "tool")
    assert tool_msg.content == PLAN_TEXT  # texte vu par le modèle : identique à avant
    assert all("plan_mode" not in json.dumps(m) for m in ship.seen[1])  # le code ne part jamais au modèle
    assert results[0].content == PLAN_TEXT and results[0].reason == "plan_mode"
    v = next(v for v in turn.verdicts if v.decision is Decision.DENY)
    assert v.rule == "permissions:plan" and v.reason == "mode plan : lecture seule" and v.code == "plan_mode"


def test_tool_result_event_carries_the_reason_only_on_a_coded_refusal():
    hub, seen = _hub()
    turn, call = _turn(), ToolCall(id="c1", name="terminal", arguments={})
    hub.tool_result(turn, call, ToolResult(call_id="c1", name="terminal", content=PLAN_TEXT, reason="plan_mode"), 3)
    hub.tool_result(turn, call, ToolResult(call_id="c1", name="terminal", content="ok"), 3)
    assert seen[0][1]["reason"] == "plan_mode" and seen[0][1]["preview"] == PLAN_TEXT
    assert "reason" not in seen[1][1]


def test_deny_rule_and_hardline_codes():
    perms = LocalPermissions(mode=Mode.AUTO, deny=[ck_rule("terminal(rm *)")])
    gates = PermissionGates(inner=_Inner(), permissions=perms)
    turn = _turn()
    v = gates.action(ToolCall(id="1", name="terminal", arguments={"command": "rm x"}), None, turn)[0]
    assert v.decision is Decision.DENY and v.code == "deny_rule" and v.reason == "règle deny terminal(rm *)"
    ok = gates.action(ToolCall(id="2", name="read_file", arguments={"path": "a"}), None, turn)[0]
    assert ok.code is None


def ck_rule(text):
    from nokker.qa.permissions import Rule

    return Rule.parse(text)


class _Inner:
    class invariants:
        @staticmethod
        def check_action(call, spec, turn):
            return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="ok")

    def action(self, call, spec, turn):
        return [self.invariants.check_action(call, spec, turn)]


def test_direct_call_refused_by_plan_mode_says_why(tmp_path):
    httpd, port, api, kernel_, _, _, l3, _, thread = _server(tmp_path)
    try:
        perms = LocalPermissions(mode=Mode.PLAN)
        kernel_.gates = PermissionGates(inner=kernel_.gates, permissions=perms)
        status, body = _req(port, "/v1/local/tools/call", token=api.token,
                            body={"thread_id": thread, "name": "checkpoint_create", "arguments": {"reason": "x"}})
        assert status == 200 and body["is_error"] and body["reason"] == "plan_mode"
        assert body["content"] == "refusé par le noyau (permissions:plan: mode plan : lecture seule)"
        assert l3.calls == []
    finally:
        httpd.shutdown()


# ─────────────── (a) mode d'une session locale ───────────────


def test_local_session_mode_get_put_and_event(tmp_path):
    httpd, port, api, kernel_, _, _, l3, _, thread = _server(tmp_path)
    events: list = []
    api.emit = lambda name, data: events.append((name, data))
    try:
        url = f"/v1/local/sessions/{thread}/mode"
        assert _req(port, url, token=api.token)[0] == 404  # permissions non posées
        perms = LocalPermissions(mode=Mode.ACCEPT_EDITS)
        api.permissions = perms
        kernel_.gates = PermissionGates(inner=kernel_.gates, permissions=perms)
        assert _req(port, url)[0] == 401
        assert _req(port, "/v1/local/sessions/th_inconnu/mode", token=api.token)[0] == 404
        status, body = _req(port, url, token=api.token)
        assert status == 200 and body == {"mode": "accept_edits", "modes": ["default", "accept_edits", "auto", "plan"],
                                          "effective": "accept_edits"}
        status, body = _req(port, url, token=api.token, body={"mode": "plan"}, method="PUT")
        assert status == 200 and body["mode"] == "plan" and body["effective"] == "plan"
        assert events == [("session.mode", {"thread_id": thread, "mode": "plan", "effective": "plan"})]
        assert perms.mode is Mode.ACCEPT_EDITS  # les autres sessions gardent le mode de l'utilisateur
        # le mode compte pour la session : un appel à effet est maintenant refusé
        status, out = _req(port, "/v1/local/tools/call", token=api.token,
                           body={"thread_id": thread, "name": "checkpoint_create", "arguments": {}})
        assert out["reason"] == "plan_mode" and l3.calls == []
        status, err = _req(port, url, token=api.token, body={"mode": "yolo"}, method="PUT")
        assert status == 400 and err["error"]["code"] == "bad_mode"
        perms.restrict_with(thread, {"mode": "default"})  # le dépôt / une équipe impose au moins `default`
        status, err = _req(port, url, token=api.token, body={"mode": "auto"}, method="PUT")
        assert status == 400 and err["error"]["code"] == "mode_above_project"
        status, body = _req(port, url, token=api.token, body={"mode": "default"}, method="PUT")
        assert status == 200 and body["effective"] == "default"
        perms.thread_mode[thread] = Mode.AUTO  # choisi avant que le projet ne restreigne
        assert _req(port, url, token=api.token)[1] == {"mode": "auto", "modes": ["default", "accept_edits", "auto",
                                                                                  "plan"], "effective": "default"}
    finally:
        httpd.shutdown()


def test_thread_mode_follows_subagents_and_project_restriction():
    perms = LocalPermissions(mode=Mode.AUTO)
    perms.set_thread_mode("th_a", "plan")
    assert perms.mode_for("th_a") is Mode.PLAN and perms.mode_for("th_a/sa-1") is Mode.PLAN
    assert perms.mode_for("th_b") is Mode.AUTO


# ─────────────── D7 : turn.done ───────────────


class _Failing:
    model = "m"

    def complete(self, *a, **kw):
        raise RuntimeError("échec réseau après 3 tentative(s) : _ssl.c:1064: The handshake operation timed out")


def test_turn_failure_codes_from_the_loop():
    k, *_ = kernel([])
    k.providers = {Tier.SHIP: _Failing()}
    turn = k.run_turn(TurnInput("t1", "bonjour"))
    assert turn.failure["code"] == "tier_unavailable" and "handshake" in turn.failure["message"]
    assert turn.reply  # le tour garde sa réponse (escalade), comme avant

    k2, *_ = kernel([])
    k2.context.assemble = lambda *a, **kw: (_ for _ in ()).throw(ValueError("boum"))
    turn2 = k2.run_turn(TurnInput("t1", "bonjour"))
    assert turn2.failure == {"code": "internal_error", "message": "ValueError: boum"}

    k3, *_ = kernel([Message("assistant", "bonjour")])
    assert k3.run_turn(TurnInput("t1", "salut")).failure is None


def test_turn_done_error_refused_cancelled_fields():
    hub, seen = _hub()
    hub.turn_done(_turn(failure={"code": "tier_unavailable", "message": "palier ship indisponible : HTTP 502"}))
    hub.turn_done(_turn(verdicts=[Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule="permissions:plan",
                                          code="plan_mode")]))
    hub.turn_done(_turn(error="cancelled"))
    hub.turn_done(_turn(verdicts=[Verdict(gate=Gate.SWITCH, decision=Decision.DENY, layer=1, rule="porte")]))
    done = [d for n, d in seen if n == "turn.done"]
    assert done[0]["error"] == {"code": "tier_unavailable", "message": "palier ship indisponible : HTTP 502"}
    assert "refused" not in done[0] and "cancelled" not in done[0]
    assert done[1]["refused"] is True and "error" not in done[1]
    assert done[2]["cancelled"] is True and "error" not in done[2]
    assert "refused" not in done[3]  # une porte bascule n'est pas un effet refusé


def test_turn_done_refused_when_a_card_of_the_turn_was_rejected():
    """Client sans surveillance (`nokker -p`) : il rejette toute carte ; le tour le sait à sa fin."""
    hub, seen = _hub()
    approvals = InMemoryApprovals()
    hub.kernel = type("K", (), {"approvals": approvals})()
    turn = _turn()
    card = approvals.request(turn, ToolCall(id="c1", name="terminal", arguments={"command": "make"}),
                             Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="r", strength="standard"),
                             None)
    turn.approval_ids.append(card.id)
    hub.turn_done(turn)
    approvals.decide_local(card.id, "reject", LOCAL_OWNER)
    hub.turn_done(turn)
    done = [d for n, d in seen if n == "turn.done"]
    assert "refused" not in done[0] and done[1]["refused"] is True


# ─────────────── (b) todo.updated ───────────────


def test_todo_updated_on_write_only():
    hub, seen = _hub()
    items = [{"id": "1", "content": "lire", "status": "in_progress"}]
    content = json.dumps({"todos": items, "revision": 1, "summary": {"total": 1}})
    turn = _turn()
    hub.tool_executed(turn, ToolCall(id="c", name="todo", arguments={"todos": items}), None,
                      ToolResult(call_id="c", name="todo", content=content))
    hub.tool_executed(turn, ToolCall(id="d", name="todo", arguments={}), None,
                      ToolResult(call_id="d", name="todo", content=content))  # lecture : pas d'événement
    hub.tool_executed(turn, ToolCall(id="e", name="todo", arguments={"todos": "x"}), None,
                      ToolResult(call_id="e", name="todo", content='{"error": "bad"}', is_error=True))
    todo = [d for n, d in seen if n == "todo.updated"]
    assert len(todo) == 1 and todo[0]["thread_id"] == "th_x" and todo[0]["items"] == items


# ─────────────── D8 : reason_code des points de retour ───────────────


def test_reason_codes_are_stable_next_to_the_unchanged_text():
    assert ck.reason_code("avant terminal") == "before_terminal"
    assert ck.reason_code("avant write_file") == "before_write_file"
    assert ck.reason_code("avant retour à abc1234") == "before_rollback"
    assert ck.reason_code("pre-rollback snapshot (restoring to abc12345)") == "before_rollback"
    assert ck.reason_code("manuel") == "manual" and ck.reason_code("automatique") == "auto"
    assert ck.reason_code("début de session") == "session_start" and ck.reason_code("refacto risquée") == "other"
    assert ck.reason_code("avant refacto", tool="checkpoint_create") == "manual"  # le lien au tour prime
    assert ck.reason_code("avant terminal", tool="terminal") == "before_terminal"
    assert ck.fr_reason("pre-rollback snapshot (restoring to abc12345)") == "avant retour à abc1234"  # inchangé


class _Mgr:
    """Gestionnaire factice : deux points, raisons brutes du gestionnaire amont."""

    def __init__(self):
        self._checkpointed_dirs: set = set()
        self.points = [{"hash": "h2", "reason": "avant terminal"},
                       {"hash": "h1", "reason": "pre-rollback snapshot (restoring to abc12345)"}]

    def list_checkpoints(self, root):
        return [dict(p) for p in self.points]

    def ensure_checkpoint(self, root, reason):
        self.points.insert(0, {"hash": f"h{len(self.points) + 1}", "reason": reason})
        return True

    def tip(self, root):
        return self.points[0]["hash"]


def test_checkpoint_tool_text_is_unchanged_and_codes_ride_alongside(tmp_path):
    events: list = []
    src = ck.CheckpointToolSource(lambda tid: tmp_path, manager=_Mgr(), emit=lambda n, d: events.append((n, d)))
    ctx = ToolContext(thread_id="th", turn_index=2, channel="cli")
    listing = src.call("checkpoint_list", {}, ctx)
    # texte vu par le modèle : exactement celui d'avant (raison en français, pas de reason_code)
    assert listing.content == json.dumps([{"hash": "h2", "reason": "avant terminal"},
                                          {"hash": "h1", "reason": "avant retour à abc1234"}], ensure_ascii=False)
    body = ck.direct_result_body("checkpoint_list", listing)
    assert body["content"] == listing.content
    assert body["reason_codes"] == {"h2": "before_terminal", "h1": "before_rollback"}
    created = src.call("checkpoint_create", {"reason": "avant gros changement"}, ctx)
    assert "reason_code" not in created.content
    assert events == [("checkpoint.created", {"thread_id": "th", "turn_index": 2, "hash": "h3",
                                              "tool": "checkpoint_create", "reason_code": "manual"})]
    assert ck.direct_result_body("skill_list", ToolResult(call_id="", name="skill_list", content="[]")) == {
        "content": "[]", "is_error": False}


def test_checkpoint_created_before_a_tool_has_its_code(tmp_path):
    events: list = []

    class Inner:
        def specs(self):
            from nokker.core.ports import ToolSpec

            return [ToolSpec(name="terminal", description="t", parameters={}, effect=Effect.DESTRUCTIVE)]

        def call(self, name, arguments, ctx):
            return ToolResult(call_id="", name=name, content="ok")

    mgr = _Mgr()
    mgr.new_turn = lambda: None
    wrap = ck.CheckpointingToolSource(Inner(), lambda tid: tmp_path, manager=mgr,
                                      emit=lambda n, d: events.append((n, d)))
    wrap.call("terminal", {"command": "make"}, ToolContext(thread_id="th", turn_index=1, channel="cli"))
    assert events[0][1]["tool"] == "terminal" and events[0][1]["reason_code"] == "before_terminal"
    assert mgr.points[0]["reason"] == "avant terminal"  # la raison écrite au point ne change pas
