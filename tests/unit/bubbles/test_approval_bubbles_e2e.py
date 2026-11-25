"""Bulle `approval` suivie jusqu'au bout PAR LE VRAI SERVEUR (`make_handler`) : carte posée -> bulle `working` ; décision
par l'API (approuvée, rejetée), expiration, échec d'exécution -> la bulle quitte `working` (`done` ou `failed`). Une bulle
par carte, même à plusieurs cartes dans un tour ; une mise à jour de bulle en panne n'empêche jamais la décision."""
from __future__ import annotations

import threading
import time
from http.server import ThreadingHTTPServer

from nokker.api.events import EventBus
from nokker.api.server import build_hub, make_handler
from nokker.approvals.memory import InMemoryApprovals
from nokker.config import NokkerConfig
from nokker.core.ports import ToolSpec
from nokker.core.turn import Decision, Effect, Gate, InputCheck, ToolCall, ToolResult, Turn, Verdict
from nokker.local.api import LOCAL_OWNER
from nokker.tenancy.context import tenant_of_thread, tenant_scope
from nokker.tools.registry import Registry
from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
from tests.unit.test_local_api import _api, _req


class _Src:
    """Un outil à effet qui réussit, un qui rend une erreur, un qui lève."""

    def __init__(self):
        self.calls: list[str] = []

    def specs(self):
        return [ToolSpec(name=n, description=n, parameters={"type": "object"}, effect=Effect.DESTRUCTIVE, source="test")
                for n in ("sites_connect", "sites_fail", "sites_boom")]

    def call(self, name, arguments, ctx):
        self.calls.append(name)
        if name == "sites_boom":
            raise RuntimeError("panne")
        return ToolResult(call_id="", name=name, content="ok" if name == "sites_connect" else "refus distant",
                          is_error=name == "sites_fail")


def _setup(tmp_path, *, clock=None):
    approvals = InMemoryApprovals(**({"clock": clock} if clock else {}))
    kernel, _ = make_kernel_with_approvals(approvals)
    kernel.tools = Registry(sources=[_Src()])
    hub = build_hub(NokkerConfig(web_search_enabled=False, approvals_enabled=False), EventBus())
    hub.approval_bubbles = True  # comme `serve` quand `approval_bubbles` est allumé
    approvals.card_observer = hub
    hub.card_lookup = approvals.get
    api = _api(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, approvals=approvals,
                                                               local_api=api, hub=hub))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    thread = "th_bubble_card"
    with tenant_scope(tenant_of_thread(thread)):
        hub.threads.ensure(thread, LOCAL_OWNER, "connecte")
    return httpd, port, api, approvals, hub, thread


def _card(approvals, thread, tool="sites_connect", call_id="c1"):
    turn = Turn(thread_id=thread, index=0, input=InputCheck(text="connecte"), channel="api", sender=LOCAL_OWNER)
    return approvals.request(turn, ToolCall(id=call_id, name=tool, arguments={"site": "a"}),
                             Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="permissions:default",
                                     strength="standard"), None)


def _bubbles(hub, thread):
    with tenant_scope(tenant_of_thread(thread)):
        return {(b.state or {}).get("approval_id"): b for b in hub.bubbles.list_thread(thread) if b.kind == "approval"}


def _until(fn, what, t=5.0):
    end = time.time() + t
    while time.time() < end:
        if fn():
            return
        time.sleep(0.02)
    raise AssertionError(what)


def _decide(port, api, card, decision):
    return _req(port, f"/v1/local/approvals/{card.id}/decide", token=api.token, body={"decision": decision})


def test_card_posed_then_approved_through_the_server_ends_done(tmp_path):
    httpd, port, api, approvals, hub, th = _setup(tmp_path)
    try:
        card = _card(approvals, th)
        assert _bubbles(hub, th)[card.id].status == "working"
        status, _ = _decide(port, api, card, "approve")
        assert status in (200, 202)
        _until(lambda: _bubbles(hub, th)[card.id].status != "working", "bulle restée working")
        b = _bubbles(hub, th)[card.id]
        assert b.status == "done"
        _until(lambda: approvals.get(card.id).status == "executed", "carte non exécutée")
        assert _bubbles(hub, th)[card.id].status == "done" and _bubbles(hub, th)[card.id].state["status"] == "executed"
    finally:
        httpd.shutdown()


def test_rejected_card_ends_done_not_failed(tmp_path):
    httpd, port, api, approvals, hub, th = _setup(tmp_path)
    try:
        card = _card(approvals, th)
        assert _decide(port, api, card, "reject")[0] in (200, 202)
        b = _bubbles(hub, th)[card.id]
        assert b.status == "done" and b.state["status"] == "rejected"
    finally:
        httpd.shutdown()


def test_expired_card_ends_failed_through_the_server(tmp_path):
    class Clock:
        t = 1_000_000.0

        def __call__(self):
            return self.t

    clock = Clock()
    httpd, port, api, approvals, hub, th = _setup(tmp_path, clock=clock)
    try:
        card = _card(approvals, th)
        clock.t += 10 * 24 * 3600
        status, _ = _decide(port, api, card, "approve")
        assert status == 409
        b = _bubbles(hub, th)[card.id]
        assert b.status == "failed" and b.state["status"] == "expired"
    finally:
        httpd.shutdown()


def test_expired_card_seen_by_the_listing_ends_failed(tmp_path):
    class Clock:
        t = 1_000_000.0

        def __call__(self):
            return self.t

    clock = Clock()
    httpd, port, api, approvals, hub, th = _setup(tmp_path, clock=clock)
    try:
        card = _card(approvals, th)
        clock.t += 10 * 24 * 3600
        assert _req(port, "/v1/local/approvals", token=api.token)[1]["approvals"] == []
        assert _bubbles(hub, th)[card.id].status == "failed"
    finally:
        httpd.shutdown()


def test_failed_execution_ends_failed(tmp_path):
    httpd, port, api, approvals, hub, th = _setup(tmp_path)
    try:
        cards = [_card(approvals, th, tool=t, call_id=f"c{i}") for i, t in enumerate(("sites_fail", "sites_boom"))]
        for card in cards:
            assert _decide(port, api, card, "approve")[0] in (200, 202)
            _until(lambda c=card: approvals.get(c.id).status == "failed", "carte non en échec")
            _until(lambda c=card: _bubbles(hub, th)[c.id].status != "working", "bulle restée working")
            b = _bubbles(hub, th)[card.id]
            assert b.status == "failed" and b.state["status"] == "failed"
    finally:
        httpd.shutdown()


def test_several_cards_one_turn_one_bubble_each(tmp_path):
    httpd, port, api, approvals, hub, th = _setup(tmp_path)
    try:
        a, b, c = (_card(approvals, th, call_id=f"c{i}") for i in range(3))
        bubbles = _bubbles(hub, th)
        assert len(bubbles) == 3 and {x.status for x in bubbles.values()} == {"working"}
        assert _decide(port, api, b, "reject")[0] in (200, 202)
        now = _bubbles(hub, th)
        assert (now[a.id].status, now[b.id].status, now[c.id].status) == ("working", "done", "working")
        assert _decide(port, api, a, "approve")[0] in (200, 202)
        _until(lambda: approvals.get(a.id).status == "executed", "carte a non exécutée")
        now = _bubbles(hub, th)
        assert (now[a.id].status, now[b.id].status, now[c.id].status) == ("done", "done", "working")
        assert len(now) == 3  # jamais de seconde bulle
    finally:
        httpd.shutdown()


def test_a_broken_bubble_never_stops_the_decision(tmp_path):
    httpd, port, api, approvals, hub, th = _setup(tmp_path)
    try:
        card = _card(approvals, th)

        def boom(*a, **k):
            raise RuntimeError("bulles en panne")

        hub.update_from_kernel = boom
        assert _decide(port, api, card, "reject")[0] in (200, 202)
        assert approvals.get(card.id).status == "rejected"
    finally:
        httpd.shutdown()


def _listed(port, api, th):
    status, body = _req(port, f"/v1/threads/{th}/bubbles", token=api.token)
    assert status == 200
    return {(b.get("state") or {}).get("approval_id"): b for b in body if b["kind"] == "approval"}


def test_a_lost_bubble_update_is_recalled_when_the_thread_bubbles_are_read(tmp_path):
    httpd, port, api, approvals, hub, th = _setup(tmp_path)
    try:
        card = _card(approvals, th)
        real = hub.update_from_kernel

        def boom(*a, **k):
            raise RuntimeError("bulles en panne")

        hub.update_from_kernel = boom
        assert _decide(port, api, card, "reject")[0] in (200, 202)
        assert _bubbles(hub, th)[card.id].status == "working"  # la mise à jour a été perdue, la décision a abouti
        hub.update_from_kernel = real
        assert _listed(port, api, th)[card.id]["status"] == "done"
        assert _listed(port, api, th)[card.id]["status"] == "done"  # idempotent
        assert len(_bubbles(hub, th)) == 1
    finally:
        httpd.shutdown()


def test_an_expired_card_nobody_read_is_recalled_by_the_bubble_listing(tmp_path):
    class Clock:
        t = 1_000_000.0

        def __call__(self):
            return self.t

    clock = Clock()
    httpd, port, api, approvals, hub, th = _setup(tmp_path, clock=clock)
    try:
        card = _card(approvals, th)
        clock.t += 10 * 24 * 3600  # personne ne lit la carte : l'expiration est paresseuse
        assert _bubbles(hub, th)[card.id].status == "working"
        listed = _listed(port, api, th)[card.id]
        assert listed["status"] == "failed" and listed["state"]["status"] == "expired"
    finally:
        httpd.shutdown()
