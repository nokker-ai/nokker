"""Règles d'approbation suggérées (docs/APPROVALS.md §7, `nokker/approvals/suggestions.py`) : seuil, fenêtre, refus
qui bloque, familles interdites, rejet qui masque, isolation entre comptes, et routes réelles (accept écrit la règle
dans les réglages de l'utilisateur avec sa provenance)."""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from nokker.approvals import suggestions as sg
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.ports import Approval
from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
from nokker.tenancy.context import scoped_thread_id, tenant_scope

NOW = 1_800_000_000.0
ON = sg.Settings(enabled=True, threshold=3, window_days=30)


def _card(i: int, *, tool="terminal", args=None, status="executed", strength="standard", rule="permissions:default",
          user="alice", age_days=1.0, thread="th_1") -> Approval:
    t = NOW - age_days * 86400
    return Approval(id=f"ap_{i}", thread_id=thread, turn_index=i, channel="api", tool_name=tool,
                    arguments=args if args is not None else {"command": "npm test"}, args_sha256="x",
                    strength=strength, rule=rule, status=status, created_at=t, expires_at=t + 86400,
                    decided_by=user, decided_at=t + 5)


def _compute(history, **kw):
    return sg.compute(history, tenant="tn_a", user=kw.pop("user", "alice"), settings=kw.pop("settings", ON), now=NOW,
                      **kw)


def test_threshold_reached_without_refusal():
    assert _compute([_card(i) for i in range(2)]) == []  # sous le seuil
    out = _compute([_card(i) for i in range(3)])
    assert [s.rule for s in out] == ["terminal(npm test)"] and out[0].approved == 3
    assert out[0].id == sg.suggestion_id("tn_a", "alice", "terminal(npm test)")
    assert out[0].to_json()["rejected"] == 0 and out[0].to_json()["first_at"].endswith("+00:00")


def test_one_refusal_blocks_the_rule():
    cards = [_card(i) for i in range(5)] + [_card(9, status="rejected")]
    assert _compute(cards) == []


def test_window_and_decider():
    old = [_card(i, age_days=40) for i in range(5)]
    assert _compute(old) == []  # hors fenêtre
    other = [_card(i, user="bob") for i in range(5)]
    assert _compute(other) == []  # décidées par quelqu'un d'autre
    assert [s.rule for s in _compute(other, user="bob")] == ["terminal(npm test)"]


def test_broader_rule_only_when_it_adds_evidence():
    cmds = ["npm test --watch", "npm test --ci", "npm test -u"]
    cards = [_card(i, args={"command": c}) for i, c in enumerate(cmds)]
    assert [s.rule for s in _compute(cards)] == ["terminal(npm test*)"]  # aucune commande exacte n'atteint 3
    same = [_card(i, args={"command": "npm test --watch"}) for i in range(3)]
    assert [s.rule for s in _compute(same)] == ["terminal(npm test --watch)"]  # la large n'ajoute rien


def test_existing_and_hidden_rules_are_not_proposed():
    cards = [_card(i) for i in range(3)]
    assert _compute(cards, existing=["terminal(npm test)"]) == []
    sid = sg.suggestion_id("tn_a", "alice", "terminal(npm test)")
    assert _compute(cards, hidden=[sid]) == []


@pytest.mark.parametrize("kw", [
    {"strength": "strong"},
    {"rule": "effect:destructive"},
    {"rule": "effect:send_bulk"},
    {"rule": "ops:prod"},
    {"rule": "ops:prod:db"},
    {"rule": "pages:publish"},
    {"rule": "household:guardian"},
    {"rule": "permissions:relayed_connector"},
    {"rule": "permissions:relayed_strong"},
    {"rule": "permissions:local_connector_strong"},
    {"tool": "ssh_trust", "args": {"host": "h"}},
])
def test_forbidden_families_never_suggested(kw):
    cards = [_card(i, **kw) for i in range(10)]
    assert all(sg.forbidden(c) for c in cards)
    assert _compute(cards, settings=sg.Settings(enabled=True, threshold=2)) == []


def test_settings_from_config_defaults_and_floor():
    s = sg.Settings.from_config(None)
    assert not s.enabled and s.threshold == sg.DEFAULT_THRESHOLD and s.window_days == sg.DEFAULT_WINDOW_DAYS
    assert sg.Settings.from_config({"enabled": True, "threshold": 0}).threshold == 2  # jamais une règle sur 1 carte


# ─────────────────────────── backend : historique et marques, isolation ───────────────────────────

def _request(approvals, thread, command, i):
    turn = Turn(thread_id=thread, index=i, input=InputCheck(text="x"), channel="api", sender="local-owner")
    return approvals.request(turn, ToolCall(id=f"c{i}", name="terminal", arguments={"command": command}),
                             Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="permissions:default",
                                     strength="standard"), None)


def test_history_and_marks_are_isolated_between_accounts():
    approvals = InMemoryApprovals()
    th_a = scoped_thread_id("tn_x_a", "t")
    with tenant_scope("tn_x_a"):
        for i in range(3):
            card = _request(approvals, th_a, "make build", i)
            approvals.decide_local(card.id, "approve", "us_a")
        hist_a = approvals.history(0)
        approvals.mark_suggestion("sg_1", "us_a", "dismissed", "terminal(make build)", {})
        assert "sg_1" in approvals.suggestion_marks("us_a")
    with tenant_scope("tn_x_b"):
        assert approvals.history(0) == []  # les cartes de A n'existent pas chez B
        assert approvals.suggestion_marks("us_a") == {}  # ni ses marques
        out = sg.compute(approvals.history(0), tenant="tn_x_b", user="us_a", settings=ON, now=approvals.clock())
        assert out == []
    out = sg.compute(hist_a, tenant="tn_x_a", user="us_a", settings=ON, now=approvals.clock())
    assert [s.rule for s in out] == ["terminal(make build)"]
    with pytest.raises(ValueError):
        approvals.mark_suggestion("sg_1", "us_a", "maybe", "r", {})


# ─────────────────────────── routes réelles ───────────────────────────

def _server(tmp_path, enabled=True):
    from nokker.api.server import make_handler
    from nokker.local.api import LOCAL_OWNER, LocalApi
    from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals

    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    kernel.gates = PermissionGates(inner=kernel.gates, permissions=LocalPermissions(mode=Mode.DEFAULT))
    root = tmp_path / "projets"
    (root / "app").mkdir(parents=True)
    api = LocalApi.from_config({"token_path": str(tmp_path / "tok"), "code": {"roots": [str(root)],
                                                                             "backend": "local"}}, port=8742)
    api.settings_path = tmp_path / "settings.json"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
        kernel, {api.token: LOCAL_OWNER, "cle-op": "op2"}, approvals=approvals, local_api=api,
        approval_suggestions={"enabled": enabled, "threshold": 3}))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port, api, approvals, kernel


def test_routes_disabled_by_default(tmp_path):
    from tests.unit.test_local_api import _req

    httpd, port, api, _, _ = _server(tmp_path, enabled=False)
    try:
        assert _req(port, "/v1/approvals/suggestions", token=api.token)[0] == 404
    finally:
        httpd.shutdown()


def test_accept_writes_the_rule_with_provenance_and_dismiss_hides(tmp_path):
    from nokker.local.api import LOCAL_OWNER
    from nokker.qa.permissions import Rule
    from tests.unit.test_local_api import _req

    httpd, port, api, approvals, kernel = _server(tmp_path)
    try:
        for i in range(3):
            approvals.decide_local(_request(approvals, "th_1", "npm test", i).id, "approve", LOCAL_OWNER)
            approvals.decide_local(_request(approvals, "th_1", "make lint", 10 + i).id, "approve", LOCAL_OWNER)
        assert _req(port, "/v1/approvals/suggestions")[0] == 401
        status, body = _req(port, "/v1/approvals/suggestions", token=api.token)
        assert status == 200 and body["rule_store"] == "user_settings" and body["threshold"] == 3
        by_rule = {s["rule"]: s for s in body["suggestions"]}
        assert set(by_rule) == {"terminal(npm test)", "terminal(make lint)"}

        # un autre opérateur ne voit pas les propositions du propriétaire et ne peut rien écrire
        status, other = _req(port, "/v1/approvals/suggestions", token="cle-op")
        assert status == 200 and other["suggestions"] == [] and other["rule_store"] is None
        sid = by_rule["terminal(npm test)"]["id"]
        assert _req(port, f"/v1/approvals/suggestions/{sid}/accept", token="cle-op", body={})[0] == 404

        status, acc = _req(port, f"/v1/approvals/suggestions/{sid}/accept", token=api.token, body={})
        assert status == 200 and acc["state"] == "accepted" and acc["provenance"]["source"] == "approval_suggestion"
        saved = json.loads(api.settings_path.read_text())
        assert saved["permissions"]["allow"] == ["terminal(npm test)"]
        assert saved["rule_provenance"]["terminal(npm test)"]["suggestion_id"] == sid
        assert len(saved["rule_provenance"]["terminal(npm test)"]["approval_ids"]) == 3
        assert Rule.parse("terminal(npm test)") in kernel.gates.permissions.allow  # en vigueur tout de suite

        lint = by_rule["terminal(make lint)"]["id"]
        assert _req(port, f"/v1/approvals/suggestions/{lint}/dismiss", token=api.token, body={})[1]["state"] \
            == "dismissed"
        assert _req(port, "/v1/approvals/suggestions", token=api.token)[1]["suggestions"] == []
        assert saved["permissions"]["allow"] == ["terminal(npm test)"]  # rejeter n'écrit rien
        assert _req(port, f"/v1/approvals/suggestions/{lint}/accept", token=api.token, body={})[0] == 404
    finally:
        httpd.shutdown()


def test_accept_without_rule_store_is_refused(tmp_path):
    from tests.unit.test_local_api import _req

    httpd, port, api, approvals, _ = _server(tmp_path)
    try:
        for i in range(3):
            approvals.decide_local(_request(approvals, "th_2", "cargo check", i).id, "approve", "op2")
        status, body = _req(port, "/v1/approvals/suggestions", token="cle-op")
        assert status == 200 and body["rule_store"] is None and len(body["suggestions"]) == 1
        sid = body["suggestions"][0]["id"]
        status, err = _req(port, f"/v1/approvals/suggestions/{sid}/accept", token="cle-op", body={})
        assert status == 409 and err["error"]["code"] == "no_rule_store"
        assert not api.settings_path.exists()
    finally:
        httpd.shutdown()
