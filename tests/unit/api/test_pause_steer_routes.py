"""Routes de pause, reprise et steer (docs/CLIENT-API.md §Pause et steer) : locales (jeton local) et web (membres du
fil, compte de l'appelant) — vraies requêtes HTTP en boucle locale."""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import pytest

from nokker.api.server import make_handler
from nokker.core.controls import ANY_ACCOUNT
from nokker.local.api import LOCAL_OWNER
from nokker.tenancy.context import INTERNAL_TENANT
from tests.unit.api.test_bubbles_routes import call, chat, start
from tests.unit.api.test_server import make_kernel
from tests.unit.test_local_api import _api, _req


@pytest.fixture
def local(tmp_path):
    api = _api(tmp_path)
    kernel = make_kernel()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield api, port, kernel
    httpd.shutdown()
    t.join(timeout=5)


def test_local_pause_resume_need_the_local_token(local):
    api, port, kernel = local
    for path in ("/v1/threads/th_x/pause", "/v1/threads/th_x/resume", "/v1/threads/th_x/steer", "/v1/pause",
                 "/v1/resume"):
        assert _req(port, path, body={})[0] == 401, path
    status, body = _req(port, "/v1/threads/th_x/pause", token=api.token, body={})
    assert status == 202 and body == {"thread_id": "th_x", "paused": True, "running": False}
    assert kernel.controls.thread_paused("th_x") and kernel.controls.thread_paused("th_x/sa-1")
    status, body = _req(port, "/v1/threads/th_x/resume", token=api.token, body={})
    assert status == 200 and body == {"thread_id": "th_x", "was_paused": True, "paused": False}
    # un fil de sous-agent, « / » encodé comme pour cancel
    assert _req(port, "/v1/threads/th_x%2Fsa-ab/pause", token=api.token, body={})[1]["thread_id"] == "th_x/sa-ab"


def test_local_global_pause_covers_every_thread_of_the_machine(local):
    api, port, kernel = local
    status, body = _req(port, "/v1/pause", token=api.token, body={})
    assert status == 202 and body == {"scope": "account", "paused": True}
    assert kernel.controls.is_paused("th_any", INTERNAL_TENANT, None)
    status, body = _req(port, "/v1/threads/th_any/resume", token=api.token, body={})
    assert body["paused"] is True  # la pause de la machine tient toujours le fil
    status, body = _req(port, "/v1/resume", token=api.token, body={})
    assert status == 200 and body == {"scope": "account", "paused": False, "was_paused": True}
    assert not kernel.controls.is_paused("th_any", INTERNAL_TENANT, None)


def test_local_steer_disabled_by_default_then_409_without_a_turn(local):
    api, port, kernel = local
    status, body = _req(port, "/v1/threads/th_x/steer", token=api.token, body={"text": "hop"})
    assert status == 404 and body["error"]["code"] == "steer_disabled"
    kernel.steer_enabled = True
    assert _req(port, "/v1/threads/th_x/steer", token=api.token, body={"text": "  "})[0] == 400
    assert _req(port, "/v1/threads/th_x/steer", token=api.token, body={"text": "x" * 8001})[0] == 400
    status, body = _req(port, "/v1/threads/th_x/steer", token=api.token, body={"text": "hop"})
    assert status == 409 and body["error"]["code"] == "no_running_turn"
    kernel.controls.begin("th_x")  # un tour tourne sur le fil
    try:
        status, body = _req(port, "/v1/threads/th_x/steer", token=api.token, body={"text": "hop"})
        assert status == 202 and body["status"] == "queued" and body["steer_id"].startswith("st_")
        assert [s.text for s in kernel.controls.take_steers("th_x")] == ["hop"]
    finally:
        kernel.controls.end("th_x")


def test_web_routes_members_only_and_global_pause_is_per_account():
    srv, hub, _ = start("ok")
    kernel = hub.kernel
    try:
        assert chat(srv, "salut", "sess-a")[0] == 200  # fil d'alice
        assert call(srv, "POST", "/v1/threads/sess-a/pause", {}, key=None)[0] == 401
        assert call(srv, "POST", "/v1/threads/sess-a/pause", {}, key="key-bruno")[0] == 403  # pas son fil
        assert call(srv, "POST", "/v1/threads/inconnu/pause", {})[0] == 404
        assert not kernel.controls.thread_paused("sess-a")
        st, body = call(srv, "POST", "/v1/threads/sess-a/pause", {})
        assert st == 202 and body["paused"] is True and kernel.controls.thread_paused("sess-a")
        assert call(srv, "POST", "/v1/threads/sess-a/resume", {}, key="key-bruno")[0] == 403
        st, body = call(srv, "POST", "/v1/threads/sess-a/resume", {})
        assert st == 200 and body["was_paused"] is True and body["paused"] is False
        # pause globale : le compte d'alice seulement
        assert call(srv, "POST", "/v1/pause", {}, key=None)[0] == 401
        st, body = call(srv, "POST", "/v1/pause", {})
        assert st == 202 and body == {"scope": "account", "paused": True}
        assert kernel.controls.is_paused("sess-a", INTERNAL_TENANT, "alice")
        assert not kernel.controls.is_paused("sess-b", INTERNAL_TENANT, "bruno")
        assert not kernel.controls.account_paused(INTERNAL_TENANT, ANY_ACCOUNT)
        st, body = call(srv, "POST", "/v1/resume", {}, key="key-bruno")
        assert body["was_paused"] is False and kernel.controls.is_paused("sess-a", INTERNAL_TENANT, "alice")
        st, body = call(srv, "POST", "/v1/resume", {})
        assert st == 200 and body["was_paused"] is True
    finally:
        srv.stop()


def test_web_steer_on_a_member_thread():
    srv, hub, _ = start("ok")
    kernel = hub.kernel
    kernel.steer_enabled = True
    try:
        assert chat(srv, "salut", "sess-a")[0] == 200
        assert call(srv, "POST", "/v1/threads/sess-a/steer", {"text": "hop"})[0] == 409  # tour fini
        assert call(srv, "POST", "/v1/threads/sess-a/steer", {"text": "hop"}, key="key-bruno")[0] == 403
        kernel.controls.begin("sess-a")
        try:
            st, body = call(srv, "POST", "/v1/threads/sess-a/steer", {"text": "hop"})
            assert st == 202 and body["thread_id"] == "sess-a"
        finally:
            kernel.controls.end("sess-a")
    finally:
        srv.stop()


def test_hub_publishes_pause_and_steer_events_to_the_thread_audience():
    from nokker.core.turn import InputCheck, Turn

    srv, hub, bus = start("ok")
    try:
        assert chat(srv, "salut", "sess-a")[0] == 200
        turn = Turn(thread_id="sess-a", index=1, input=InputCheck(text="x"), channel="api", sender="alice")
        hub.turn_paused(turn, {"at": "tool", "tool": "terminal"})
        hub.turn_resumed(turn, {"at": "tool", "tool": "terminal", "paused_ms": 12})
        hub.turn_steered(turn, {"steer_id": "st_1"})
        hub.turn_steer_dropped(turn, {"steer_id": "st_2", "text": "trop tard"})
        wanted = ("turn.paused", "turn.resumed", "turn.steered", "turn.steer_dropped")
        evs = [e for e in bus.replay_since(0) if e.event_type in wanted]
        assert [e.event_type for e in evs] == ["turn.paused", "turn.resumed", "turn.steered", "turn.steer_dropped"]
        assert evs[0].data["turn_id"] == "sess-a:1" and evs[0].data["_audience"] == ["alice"]
        assert evs[1].data["paused_ms"] == 12 and evs[3].data["text"] == "trop tard"
    finally:
        srv.stop()


def test_delegation_opens_pause_resume_steer_on_its_own_thread_only():
    from nokker.local.delegation import DelegatedSessions

    d = DelegatedSessions()
    claims = {"sub": "us_1", "web_session": "w1"}
    d.register("th_mine", claims)
    for op in ("cancel", "pause", "resume", "steer"):
        assert d.allows("POST", f"/v1/threads/th_mine/{op}", None, claims)
        assert not d.allows("POST", f"/v1/threads/th_other/{op}", None, claims)
    assert not d.allows("POST", "/v1/pause", None, claims)  # jamais toute la machine
