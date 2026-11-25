"""Décideur réel d'une carte décidée par l'hôte : `on_behalf_of` / `via` de `POST /v1/local/approvals/{id}/decide` vont
au journal des cartes (événement) et à `approval.decided` ; `decided_by` reste le jeton local ; sans eux, comme avant."""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import pytest

from nokker.api.server import make_handler
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict


@pytest.fixture
def world(tmp_path):
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.test_local_api import _api

    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    api = _api(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: "local-owner"},
                                                               approvals=approvals, local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    seen = []
    approvals.on_event = lambda t, d: seen.append((t, d))
    n = [0]

    def card():
        n[0] += 1
        turn = Turn(thread_id="th_1", index=n[0], input=InputCheck(text="x"), channel="api", sender="local-owner")
        return approvals.request(turn, ToolCall(id=f"c{n[0]}", name="terminal", arguments={"command": f"make {n[0]}"}),
                                 Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="permissions:default",
                                         strength="standard"), None)

    yield api, port, approvals, card, seen
    httpd.shutdown()


def test_host_decider_reaches_journal_and_event_not_decided_by(world):
    from tests.unit.test_local_api import _req

    api, port, approvals, card, seen = world
    c = card()
    status, body = _req(port, f"/v1/local/approvals/{c.id}/decide", token=api.token,
                        body={"decision": "approve", "scope": "once", "on_behalf_of": "us_marie", "via": "teams"})
    assert status in (200, 202), body
    assert body["on_behalf_of"] == "us_marie" and body["via"] == "teams"
    final = approvals.get(c.id)
    assert final.decided_by == "local-owner" and final.decided_via == "local-token"  # le jeton reste ce qu'il est
    assert approvals.decision_context(final) == {"on_behalf_of": "us_marie", "via": "teams"}
    [data] = [d for _, t, d in approvals._events if t == "approved"]  # le journal, tel que stocké
    assert data["decided_by"] == "local-owner" and data["on_behalf_of"] == "us_marie" and data["via"] == "teams"
    decided = [d for t, d in seen if t == "approval.decided"]
    assert decided and decided[-1]["on_behalf_of"] == "us_marie" and decided[-1]["via"] == "teams"


def test_rejection_is_journaled_too_and_via_alone_is_kept(world):
    from tests.unit.test_local_api import _req

    api, port, approvals, card, _ = world
    c = card()
    assert _req(port, f"/v1/local/approvals/{c.id}/decide", token=api.token,
                body={"decision": "reject", "via": "rule"})[0] == 202
    assert approvals.decision_context(approvals.get(c.id)) == {"via": "rule"}


def test_without_the_fields_nothing_changes(world):
    from tests.unit.test_local_api import _req

    api, port, approvals, card, seen = world
    c = card()
    status, body = _req(port, f"/v1/local/approvals/{c.id}/decide", token=api.token,
                        body={"decision": "approve", "scope": "once"})
    assert status in (200, 202) and "on_behalf_of" not in body and "via" not in body
    assert approvals.decision_context(approvals.get(c.id)) == {}
    [data] = [d for _, t, d in approvals._events if t == "approved"]
    assert data == {"decided_by": "local-owner", "decided_via": "local-token"}
    assert all("on_behalf_of" not in d for t, d in seen if t == "approval.decided")


@pytest.mark.parametrize("bad", [{"on_behalf_of": 5}, {"via": ["x"]}, {"on_behalf_of": "u" * 201}])
def test_invalid_fields_are_refused_before_deciding(world, bad):
    from tests.unit.test_local_api import _req

    api, port, approvals, card, _ = world
    c = card()
    status, _ = _req(port, f"/v1/local/approvals/{c.id}/decide", token=api.token,
                     body={"decision": "approve", "scope": "once", **bad})
    assert status == 400 and approvals.get(c.id).status == "pending"
