"""Relais d'inférence du lien nokker.ai (`cloud/http.py::_relay_inference`) : les en-têtes `X-Model-Served` et
`X-Sokkan-Upstream` du Router sont relayés au Lokkal (liste fermée, valeurs bornées) ; `X-Nokker-Client` reçu est relayé
au Router s'il est valide `[a-z0-9-]{1,32}`, sinon ignoré sans erreur. Faux Router HTTP local."""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nokker.api.server import make_handler
from nokker.cloud import http as cloud_http
from nokker.cloud.http import CloudLink
from nokker.cloud.link import LinkService, Signer
from tests.unit.api.test_server import make_kernel


class Router(BaseHTTPRequestHandler):
    seen: list = []
    extra: dict = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        Router.seen.append({k.lower(): v for k, v in self.headers.items()})
        hdrs = {"X-Model-Served": "qwen/qwen3.8-27b", "X-Sokkan-Upstream": "aws-ship", "X-Secret-Upstream": "nope",
                "Set-Cookie": "a=b", **Router.extra}
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            for k, v in hdrs.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\ndata: [DONE]\n\n')
            return
        out = json.dumps({"model": "autre", "choices": [{"message": {"content": "ok"}}],
                          "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def relay():
    Router.seen, Router.extra = [], {}
    router = ThreadingHTTPServer(("127.0.0.1", 0), Router)
    threading.Thread(target=router.serve_forever, daemon=True).start()
    links = LinkService()
    cloud_http.configure(CloudLink(links=links, signer=Signer.generate(), identity=lambda h: None,
                                   tenant_of=lambda a: None, router_base=f"http://127.0.0.1:{router.server_address[1]}/v1",
                                   router_key=lambda: "rk", record_usage=lambda *a: None))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    d = links.device("atelier")
    links.approve(d["user_code"], "us_p", "tn_p")
    yield httpd.server_address[1], links.token(d["device_code"])["machine_token"]
    cloud_http.configure(None)
    httpd.shutdown()
    router.shutdown()


def _post(port, token, stream=False, headers=None):
    body = json.dumps({"messages": [{"role": "user", "content": "salut"}], "stream": stream}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/inference/chat/completions", data=body,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                                          **(headers or {})})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()
        return {k.lower(): v for k, v in r.headers.items()}


@pytest.mark.parametrize("stream", [False, True])
def test_served_headers_are_relayed_and_nothing_else(relay, stream):
    port, token = relay
    got = _post(port, token, stream)
    assert got["x-model-served"] == "qwen/qwen3.8-27b" and got["x-sokkan-upstream"] == "aws-ship"
    assert "x-secret-upstream" not in got and "set-cookie" not in got


@pytest.mark.parametrize("stream", [False, True])
def test_relayed_values_are_bounded(relay, stream):
    port, token = relay
    Router.extra = {"X-Model-Served": "m" * 900, "X-Sokkan-Upstream": "  "}
    got = _post(port, token, stream)
    assert got["x-model-served"] == "m" * 200 and "x-sokkan-upstream" not in got


def test_no_served_headers_when_the_router_sends_none(relay):
    port, token = relay
    Router.extra = {"X-Model-Served": "", "X-Sokkan-Upstream": ""}
    got = _post(port, token)
    assert "x-model-served" not in got and "x-sokkan-upstream" not in got


@pytest.mark.parametrize("stream", [False, True])
def test_client_tag_is_relayed_to_the_router_when_valid(relay, stream):
    port, token = relay
    _post(port, token, stream, {"X-Nokker-Client": "canary-2"})
    assert Router.seen[-1]["x-nokker-client"] == "canary-2"
    assert Router.seen[-1]["authorization"] == "Bearer rk"  # la clé de nokker.ai, pas celle de la machine


@pytest.mark.parametrize("bad", ["Canary", "a" * 33, "a b", "tenant@x.ch", "a_b"])
def test_invalid_client_tag_is_ignored_without_error(relay, bad):
    port, token = relay
    _post(port, token, False, {"X-Nokker-Client": bad})
    assert "x-nokker-client" not in Router.seen[-1]


def test_absent_client_tag_sends_none(relay):
    port, token = relay
    _post(port, token)
    assert "x-nokker-client" not in Router.seen[-1]
