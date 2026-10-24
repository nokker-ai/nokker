"""Routes de nokker.ai pour Lokkal (nokker/cloud/http.py) sur le vrai `make_handler` : flux d'appareil complet, machine
vue par le relais, jeton d'accès, JWKS, inférence relayée (JSON et SSE) et comptée au locataire, révocation."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nokker.api.server import make_handler
from nokker.cloud import http as cloud_http
from nokker.cloud.http import CloudLink
from nokker.cloud.link import LinkService, Signer, verify_token
from tests.unit.api.test_server import make_kernel


class FakeRouter(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        FakeRouter.seen.append((self.headers.get("Authorization"), body.get("model")))
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"bon"}}]}\n\n')
            self.wfile.write(b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")
            return
        out = json.dumps({"choices": [{"message": {"content": "ok"}}],
                          "usage": {"prompt_tokens": 10, "completion_tokens": 3}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def cloud():
    router = ThreadingHTTPServer(("127.0.0.1", 0), FakeRouter)
    threading.Thread(target=router.serve_forever, daemon=True).start()
    usage = []
    signer = Signer.generate()
    cloud_http.configure(CloudLink(
        links=LinkService(), signer=signer,
        identity=lambda h: "us_pierre" if h.headers.get("Authorization") == "Bearer sess-pierre" else None,
        tenant_of=lambda acc: "tn_pierre" if acc == "us_pierre" else None,
        router_base=f"http://127.0.0.1:{router.server_address[1]}/v1", router_key=lambda: "rk-nokker",
        record_usage=lambda *a: usage.append(a)))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], signer, usage
    cloud_http.configure(None)
    httpd.shutdown()
    router.shutdown()


def _call(port, path, body=None, token=None, raw=False):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
            return r.status, (data if raw else json.loads(data or b"{}"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_link_gateway_and_inference_end_to_end(cloud):
    port, signer, usage = cloud
    status, dev = _call(port, "/v1/link/device", {"machine": "atelier"})
    assert status == 200 and dev["user_code"]
    assert _call(port, "/v1/link/token", {"device_code": dev["device_code"]})[1] == {"state": "pending"}
    assert _call(port, "/v1/link/approve", {"user_code": dev["user_code"]})[0] == 401  # sans session du compte
    status, ok = _call(port, "/v1/link/approve", {"user_code": dev["user_code"]}, token="sess-pierre")
    assert status == 200 and ok["host"] == "atelier.nokker.app"
    tok = _call(port, "/v1/link/token", {"device_code": dev["device_code"]})[1]
    mt = tok["machine_token"]
    assert tok["state"] == "approved" and tok["relay_host"] == "atelier.nokker.app"

    status, m = _call(port, "/v1/gateway/machine", token=mt)
    assert status == 200 and m["tenant"] == "tn_pierre" and m["inference"] is True
    assert _call(port, "/v1/gateway/machine", token="faux")[0] == 401
    jwks = _call(port, "/v1/.well-known/jwks.json")[1]
    acc = _call(port, "/v1/gateway/token", {}, token="sess-pierre")[1]["access_token"]
    assert verify_token(acc, jwks)["tenant"] == "tn_pierre"

    status, out = _call(port, "/v1/inference/chat/completions", {"model": "m", "messages": []}, token=mt)
    assert status == 200 and out["choices"][0]["message"]["content"] == "ok"
    assert FakeRouter.seen[-1] == ("Bearer rk-nokker", "m")  # la clé du Router est la nôtre, jamais celle du client
    status, sse = _call(port, "/v1/inference/chat/completions", {"model": "m", "messages": [], "stream": True},
                        token=mt, raw=True)
    assert status == 200 and b"bon" in sse and b"[DONE]" in sse
    assert [(u[0], u[2]) for u in usage] == [("tn_pierre", {"prompt_tokens": 10, "completion_tokens": 3}),
                                             ("tn_pierre", {"prompt_tokens": 7, "completion_tokens": 2})]

    assert _call(port, "/v1/link/revoke", {}, token=mt)[0] == 200
    assert _call(port, "/v1/inference/chat/completions", {"messages": []}, token=mt)[0] == 401


def test_pending_shows_the_machine_before_approval(cloud):
    port, _, _ = cloud
    dev = _call(port, "/v1/link/device", {"machine": "Portable de Jean"})[1]
    assert _call(port, f"/v1/link/pending?code={dev['user_code']}")[0] == 401
    status, body = _call(port, f"/v1/link/pending?code={dev['user_code'].lower()}", token="sess-pierre")
    assert status == 200 and body["machine_name"] == "Portable de Jean" and 0 < body["expires_in"] <= 600
    assert _call(port, "/v1/link/pending?code=ZZZZ-ZZZZ", token="sess-pierre")[0] == 404
    assert _call(port, "/v1/link/token", {"device_code": dev["device_code"]})[1] == {"state": "pending"}  # sans effet


def test_lokkal_session_tool_goes_through_the_gateway_with_a_delegation():
    import httpx

    from nokker.cloud.link import LinkService, Signer, verify_token
    from nokker.cloud.lokkal_session import LokkalSessionTools
    from nokker.core.ports import ToolContext

    svc, signer = LinkService(), Signer.generate()
    d = svc.device("atelier")
    svc.approve(d["user_code"], "us_pierre", "tn_pierre")
    svc.token(d["device_code"])
    seen = []

    def gateway(req: httpx.Request) -> httpx.Response:
        claims = verify_token(req.headers["Authorization"][7:], signer.jwks(), machine=svc.machines_of("us_pierre")[0].id)
        seen.append((req.url.host, req.url.path, claims["scope"], req.headers.get("X-Session-Id")))
        if req.url.path == "/v1/local/sessions":
            return httpx.Response(201, json={"thread_id": "th_l1", "cwd": "/home/p/app"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "tests verts"}}]})

    tools = LokkalSessionTools(links=svc, signer=signer, client=httpx.Client(transport=httpx.MockTransport(gateway)))
    assert tools.specs()[0].effect.value == "destructive"
    r = tools.call("lokkal_session", {"cwd": "/home/p/app", "task": "corrige"},
                   ToolContext(thread_id="th_web", turn_index=0, channel="api", sender="us_pierre"))
    assert not r.is_error and json.loads(r.content)["reply"] == "tests verts"
    assert seen == [("atelier.nokker.app", "/v1/local/sessions", "lokkal:session", None),
                    ("atelier.nokker.app", "/v1/chat/completions", "lokkal:session", "th_l1")]
    other = tools.call("lokkal_session", {"cwd": "/x", "task": "t"},
                       ToolContext(thread_id="t", turn_index=0, channel="api", sender="us_autre"))
    assert other.is_error and "aucune machine" in other.content


def _turn(thread, index, text):
    from nokker.core.turn import InputCheck, Message, Turn

    return Turn(thread_id=thread, index=index, input=InputCheck(text=text), channel="api", sender="us_pierre",
                reply=f"réponse à {text}", messages=[Message("assistant", f"réponse à {text}")])


def test_mirror_push_and_pull_between_machine_and_web():
    from nokker.local.mirror import MirroringStore, MirrorWorker
    from nokker.store.pg import MemoryStore, turn_to_dict

    cloud_store = MemoryStore()
    router = ThreadingHTTPServer(("127.0.0.1", 0), FakeRouter)
    links = LinkService()
    cloud_http.configure(CloudLink(
        links=links, signer=Signer.generate(), identity=lambda h: "us_pierre", tenant_of=lambda a: "tn_pierre",
        router_base="http://127.0.0.1:1", router_key=lambda: "x", store=cloud_store,
        mirror_allowed=lambda acc: acc == "us_pierre"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        dev = _call(port, "/v1/link/device", {"machine": "atelier"})[1]
        _call(port, "/v1/link/approve", {"user_code": dev["user_code"]}, token="s")
        mt = _call(port, "/v1/link/token", {"device_code": dev["device_code"]})[1]["machine_token"]
        local, state = MirroringStore(MemoryStore()), {}
        worker = MirrorWorker(store=local, base_url=f"http://127.0.0.1:{port}", token=lambda: mt, state=state,
                              save=lambda: None)
        local.append(_turn("th_local", 0, "corrige le test"))
        assert worker.push_once() == 1 and not local.outbox
        mid = links.machines_of("us_pierre")[0].id
        copies = [t for t, turns in cloud_store._by_thread.items() if f"lokkal-{mid}-th_local" in t]
        assert len(copies) == 1 and cloud_store._by_thread[copies[0]][0].calls == []
        local.outbox.append({"thread_id": "th_local", "turn": turn_to_dict(local.inner._by_thread["th_local"][0])})
        worker.push_once()
        assert len(cloud_store._by_thread[copies[0]]) == 1  # renvoi : sauté, pas de doublon
        cloud_store.append(_turn("web1", 0, "planifie la semaine"))
        assert worker.pull_once() == 1  # la copie lokkal-* n'est jamais retirée
        assert local.inner._by_thread["mirror-web-web1"][0].input.text == "planifie la semaine"
        assert worker.pull_once() == 0 and state["mirror_cursor"]
        assert not local.outbox  # une copie web n'est jamais renvoyée
    finally:
        cloud_http.configure(None)
        httpd.shutdown()
        router.server_close()
