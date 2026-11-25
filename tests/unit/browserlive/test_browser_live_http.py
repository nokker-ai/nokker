"""Routes de la vue navigateur en direct PAR LE SERVEUR HTTP RÉEL (make_handler), chaque méthode : frame signée (en-têtes,
403, 410), état de session, resume (titulaire, idempotent), takeover (cookie, usage unique, titulaire), écran par cookie,
carte d'approbation avec `rule_label`/`rule_source`/`context.browser`, événements `browser.*` sur /v1/events avec `id:` et
`event:`."""
from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from nokker.api.events import EventBus
from nokker.api.server import make_handler
from nokker.approvals.memory import InMemoryApprovals
from nokker.browserlive import live as live_mod
from nokker.browserlive.frames import FrameStore
from nokker.browserlive.live import BrowserLive
from nokker.core.ports import ToolCall
from nokker.core.turn import Decision, Gate, InputCheck, Turn, Verdict
from nokker.tenancy.context import INTERNAL_TENANT
from tests.unit.browserlive.test_browser_live import act, img


@pytest.fixture
def srv():
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals

    bus = EventBus()
    approvals = InMemoryApprovals(on_event=bus.publish)
    kernel, _ = make_kernel_with_approvals(approvals)
    lv = BrowserLive(publish=bus.publish, frames=FrameStore())
    lv.screen_ticket = lambda pool_sid: f"https://pool.example/vnc?t={pool_sid}"
    live_mod.configure(lv)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {"tok-a": "alice", "tok-b": "bob"},
                                                               approvals=approvals, events=bus))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address[1], lv, bus, approvals
    finally:
        httpd.shutdown()
        live_mod.configure(None)


def req(port, method, path, token=None, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, dict(r.getheaders()), raw


def _session(lv, block=None):
    meta = act("browser_click", url="https://shop.example/p")
    if block:
        meta["nokker_block"] = {"kind": block}
    lv.observe(INTERNAL_TENANT, "th1", "alice", "browser_click", {"ref": "e1"}, "ok", False,
               {"images": img(), "meta": meta}, pool_session_id="s1")
    return lv.current(INTERNAL_TENANT, "th1")


def test_frame_route_headers_and_errors(srv):
    port, lv, bus, _ = srv
    s = _session(lv)
    url = bus_frames(bus)[-1]["frame_url"]
    status, headers, raw = req(port, "GET", url)  # sans Authorization
    assert status == 200 and raw.startswith(b"RIFF")
    assert headers["Content-Type"] == "image/webp" and headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert req(port, "GET", url.replace("sig=", "sig=x"))[0] == 403
    lv.end(s, "done")
    assert req(port, "GET", url)[0] == 410  # purgée à la fin


def bus_frames(bus):
    return [e.data for e in bus.replay_since(0) if e.event_type == "browser.frame"]


def test_session_get_resume_and_takeover_by_http(srv):
    port, lv, bus, _ = srv
    s = _session(lv, block="2fa")
    status, _, raw = req(port, "GET", f"/v1/browser/sessions/{s.id}", token="tok-a")
    st = json.loads(raw)
    assert status == 200 and st["state"] == "handoff" and st["last_frame"]["frame_url"].startswith("/v1/browser/frames/")
    assert len(st["actions"]) == 1 and st["handoff"]["reason"] == "2fa"
    assert req(port, "GET", f"/v1/browser/sessions/{s.id}", token="tok-b")[0] == 404  # pas son fil
    assert req(port, "GET", f"/v1/browser/sessions/{s.id}")[0] == 401
    code = st["handoff"]["takeover_url"].rsplit("/", 1)[1]
    assert req(port, "POST", f"/v1/browser/takeover/{code}", token="tok-b", body={})[0] == 403
    status, headers, raw = req(port, "POST", f"/v1/browser/takeover/{code}", token="tok-a", body={})
    assert status == 200 and json.loads(raw)["screen_url"] == f"/v1/browser/sessions/{s.id}/screen"
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and f"Path=/v1/browser/sessions/{s.id}/screen" in cookie
    assert req(port, "GET", f"/v1/browser/takeover/{code}", token="tok-a")[0] == 410  # rejouée
    token = cookie.split(";", 1)[0]
    status, headers, _ = req(port, "GET", f"/v1/browser/sessions/{s.id}/screen", headers={"Cookie": token})
    assert status == 302 and headers["Location"] == "https://pool.example/vnc?t=s1"
    assert req(port, "GET", f"/v1/browser/sessions/{s.id}/screen")[0] == 403
    assert req(port, "POST", f"/v1/browser/sessions/{s.id}/resume", token="tok-b", body={})[0] == 404
    first = req(port, "POST", f"/v1/browser/sessions/{s.id}/resume", token="tok-a", body={})
    again = req(port, "POST", f"/v1/browser/sessions/{s.id}/resume", token="tok-a", body={})
    assert first[0] == again[0] == 200 and json.loads(again[2])["state"] == "running"
    assert len([e for e in bus.replay_since(0) if e.event_type == "browser.resumed"]) == 1


def test_approval_card_carries_rule_label_source_and_browser_context(srv):
    port, lv, bus, approvals = srv
    s = _session(lv)
    turn = Turn(thread_id="th1", index=0, input=InputCheck(text="paie"), channel="api", sender="alice")
    card = approvals.request(turn, ToolCall(id="c1", name="browser_click", arguments={"ref": "e1"}),
                             Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:spend",
                                     strength="strong"), None)
    status, _, raw = req(port, "GET", f"/v1/approvals/{card.id}", token="tok-a", headers={"Accept-Language": "de"})
    out = json.loads(raw)
    assert status == 200 and out["rule"] == "effect:spend" and out["rule_label"] == "Ausgabe — braucht dein OK"
    assert out["rule_labels"]["fr"] == "Dépense — ton accord est requis" and out["rule_source"] == {"kind": "system"}
    assert out["context"] == {"browser": {"session_id": s.id, "seq": s.seq}}


def test_browser_events_on_the_sse_stream_with_id_and_event(srv):
    port, lv, bus, _ = srv
    _session(lv)
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/v1/events", headers={"Authorization": "Bearer tok-a", "Last-Event-ID": "0"})
    r = c.getresponse()
    buf = b""
    while b"event: browser.frame" not in buf:
        buf += r.fp.read1(4096) if hasattr(r.fp, "read1") else r.read(1)
    c.close()
    text = buf.decode()
    assert "event: browser.action\ndata: " in text and "\nid: " in "\n" + text
    assert "_audience" not in text and "_tenant" not in text
