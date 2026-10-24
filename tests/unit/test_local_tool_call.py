"""`POST /v1/local/tools/call` (docs/LOKKAL.md §7.4.4) : appel d'outil du propriétaire sans tour modèle — liste
blanche, fil local seulement, mêmes portes que la boucle, carte sans relance de la boucle, ligne « [hors tour] » au
tour suivant, 409 pendant un tour."""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

from nokker.api.events import EventBus
from nokker.api.server import build_hub, make_handler
from nokker.approvals.memory import InMemoryApprovals
from nokker.config import NokkerConfig
from nokker.core.loop import DIRECT_CHANNEL
from nokker.core.ports import ToolSpec
from nokker.core.turn import Effect, ToolResult, TurnInput
from nokker.local.api import LOCAL_OWNER
from nokker.tools.registry import Registry
from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
from tests.unit.test_local_api import _api, _req

_EFFECTS = {"checkpoint_list": Effect.READ, "checkpoint_create": Effect.WRITE,
            "checkpoint_restore": Effect.DESTRUCTIVE, "todo": Effect.READ, "terminal": Effect.DESTRUCTIVE}


class FakeL3:
    def __init__(self):
        self.calls = []

    def specs(self):
        return [ToolSpec(name=n, description=n, parameters={"type": "object"}, effect=e, source="test")
                for n, e in _EFFECTS.items()]

    def call(self, name, arguments, ctx):
        self.calls.append((name, arguments, ctx.channel))
        return ToolResult(call_id="", name=name, content=f'{{"{name}": "ok"}}')


def _server(tmp_path):
    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    l3 = FakeL3()
    kernel.tools = Registry(sources=[l3])
    hub = build_hub(NokkerConfig(web_search_enabled=False, approvals_enabled=False), EventBus())
    published = []
    hub.direct_result = lambda *a, **k: published.append((*a, k.get("owner")))
    api = _api(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, approvals=approvals,
                                                               local_api=api, hub=hub))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    thread = _req(port, "/v1/local/sessions", token=api.token, body={"cwd": str(tmp_path / "projets" / "app")})[1][
        "thread_id"]
    return httpd, port, api, kernel, approvals, hub, l3, published, thread


def test_whitelist_local_thread_and_arguments(tmp_path):
    httpd, port, api, kernel, _, _, l3, _, thread = _server(tmp_path)
    try:
        call = "/v1/local/tools/call"
        assert _req(port, call, body={"thread_id": thread, "name": "checkpoint_list"})[0] == 401
        assert _req(port, call, token=api.token, body={"thread_id": "th_web", "name": "checkpoint_list"})[0] == 404
        assert _req(port, call, token=api.token, body={"thread_id": thread, "name": "terminal",
                                                       "arguments": {"command": "ls"}})[0] == 403
        assert _req(port, call, token=api.token, body={"thread_id": thread, "name": "todo",
                                                       "arguments": {"todos": []}})[0] == 400
        status, body = _req(port, call, token=api.token, body={"thread_id": thread, "name": "checkpoint_list"})
        assert status == 200 and body == {"content": '{"checkpoint_list": "ok"}', "is_error": False}
        assert l3.calls == [("checkpoint_list", {}, DIRECT_CHANNEL)]
    finally:
        httpd.shutdown()


def test_effect_call_leaves_a_note_for_the_next_turn_and_no_model_call(tmp_path):
    httpd, port, api, kernel, _, _, _, _, thread = _server(tmp_path)
    try:
        before = len(kernel.ledger.calls) if hasattr(kernel.ledger, "calls") else None
        status, _ = _req(port, "/v1/local/tools/call", token=api.token,
                         body={"thread_id": thread, "name": "checkpoint_create", "arguments": {"reason": "avant"}})
        assert status == 200
        if before is not None:
            assert len(kernel.ledger.calls) == before  # aucun appel modèle
        turn = kernel.run_turn(TurnInput(thread_id=thread, user_text="continue", sender=LOCAL_OWNER))
        assert any(b.startswith("[hors tour] le propriétaire a appelé checkpoint_create") for b in turn.injected)
        again = kernel.run_turn(TurnInput(thread_id=thread, user_text="et ensuite", sender=LOCAL_OWNER))
        assert not any(b.startswith("[hors tour]") for b in again.injected)  # une seule fois
    finally:
        httpd.shutdown()


def test_card_of_a_direct_call_does_not_relaunch_the_loop(tmp_path):
    httpd, port, api, kernel, approvals, _, l3, published, thread = _server(tmp_path)
    try:
        status, body = _req(port, "/v1/local/tools/call", token=api.token,
                            body={"thread_id": thread, "name": "checkpoint_restore", "arguments": {"hash": "abc"}})
        assert status == 202 and l3.calls == []
        card = approvals.get(body["approval_id"])
        assert card.channel == DIRECT_CHANNEL and card.status == "pending"
        runs = []
        kernel.run_turn = lambda *a, **k: runs.append(a)  # la boucle ne doit pas être relancée
        status, _ = _req(port, f"/v1/local/approvals/{card.id}/decide", token=api.token,
                         body={"decision": "approve", "scope": "once"})
        assert status == 200 and runs == []
        assert l3.calls == [("checkpoint_restore", {"hash": "abc"}, DIRECT_CHANNEL)]
        assert published == [(thread, card.id, "checkpoint_restore", '{"checkpoint_restore": "ok"}', False, LOCAL_OWNER)]
        assert any("[hors tour]" in n for n in kernel._owner_notes[thread])
    finally:
        httpd.shutdown()


def test_busy_thread_is_409(tmp_path):
    httpd, port, api, _, _, hub, l3, _, thread = _server(tmp_path)
    try:
        held, release = threading.Event(), threading.Event()

        def turn():
            with hub.thread_lock(thread):
                held.set()
                release.wait(5)

        threading.Thread(target=turn, daemon=True).start()
        held.wait(5)
        assert _req(port, "/v1/local/tools/call", token=api.token,
                    body={"thread_id": thread, "name": "checkpoint_list"})[0] == 409
        release.set()
        assert l3.calls == []
    finally:
        httpd.shutdown()


def test_checkpoint_create_needs_no_card_in_accept_edits():
    from nokker.core.turn import Decision, ToolCall
    from nokker.qa.permissions import LocalPermissions, Mode

    perms = LocalPermissions(mode=Mode.ACCEPT_EDITS)
    create = ToolCall(id="1", name="checkpoint_create", arguments={"reason": "avant"})
    restore = ToolCall(id="2", name="checkpoint_restore", arguments={"hash": "abc"})
    assert perms.decide(create, Effect.WRITE, "th")[0] is Decision.ALLOW
    assert perms.decide(restore, Effect.DESTRUCTIVE, "th")[0] is None  # les invariants posent la carte forte
    assert LocalPermissions(mode=Mode.DEFAULT).decide(create, Effect.WRITE, "th")[0] is Decision.HITL


def test_direct_result_reaches_the_owner_on_a_thread_without_turns():
    """Vu par features sur lokkal-g1 (24.10) : fil ouvert, aucun tour, carte directe décidée → audience vide au hub,
    l'événement ne partait chez personne."""
    events = []
    hub = build_hub(NokkerConfig(web_search_enabled=False, approvals_enabled=False), EventBus())
    hub.publish = lambda kind, payload: events.append((kind, payload))
    hub.direct_result("th_sans_tour", "ap1", "checkpoint_restore", '{"success": true}', False, owner=LOCAL_OWNER)
    kind, payload = events[-1]
    assert kind == "tool.result" and payload["_audience"] == [LOCAL_OWNER]
    assert payload["approval_id"] == "ap1" and payload["content"] == '{"success": true}' and payload["ok"]
