"""Routes /v1/guide* en vraies requêtes HTTP (boucle locale) ; drapeau éteint : 404 et tour ordinaire identique à
l'octet (ce que reçoit le modèle d'un tour normal ne change pas, Guide allumé ou non)."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from nokker.api.events import EventBus
from nokker.api.server import make_handler
from nokker.config import NokkerConfig
from nokker.core.ports import Completion
from nokker.core.turn import Message, Tier, Usage
from nokker.guide import http as guide_http
from nokker.guide.service import GuideService
from nokker.guide.store import MemoryUsage
from tests.unit.api.test_server import _Server, make_kernel

KEYS = {"key-a": "owner1"}


class RecordingShip:
    def __init__(self):
        self.seen = []

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.seen.append(json.dumps([m.__dict__ if hasattr(m, "__dict__") else m for m in messages], default=str,
                                    ensure_ascii=False, sort_keys=True))
        return Completion(message=Message(role="assistant", content="ok"), usage=Usage(prompt=1, completion=1),
                          model="m", latency_s=0.01)


class FakeModel:
    def post(self, body):
        return {"choices": [{"message": {"content": "Essaie la recherche web.\nVOIR: web_search"}}], "usage": {}}


def start(guide_on: bool):
    kernel = make_kernel()
    ship = RecordingShip()
    kernel.providers = {Tier.SHIP: ship}
    guide_http.configure(GuideService(config=NokkerConfig(), usage=MemoryUsage(), transport=FakeModel())
                         if guide_on else None)
    handler = make_handler(kernel, KEYS, None, approvals=None, events=EventBus(), hub=None,
                           known_users=frozenset(KEYS.values()))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    return _Server(httpd=httpd, thread=th, port=httpd.server_address[1]), ship


def call(srv, method, path, body=None, key="key-a"):
    h = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(srv.url(path), data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None
        except ValueError:
            return e.code, raw.decode("utf-8", "replace")


def chat(srv, text):
    return call(srv, "POST", "/v1/chat/completions", {"model": "nokker", "messages": [{"role": "user", "content": text}]})


def test_routes_when_on():
    srv, _ = start(True)
    try:
        st, v = call(srv, "GET", "/v1/guide")
        assert st == 200 and v["intro"]["seen"] is False and len(v["intro"]["picks"]) <= 3
        assert call(srv, "POST", "/v1/guide/intro", {})[0] == 200
        assert call(srv, "GET", "/v1/guide")[1]["intro"]["seen"] is True
        st, r = call(srv, "POST", "/v1/guide/ask", {"text": "Qu'est-ce que tu sais faire ?"})
        assert st == 200 and r["reply"] == "Essaie la recherche web." and r["features"][0]["id"] == "web_search"
        assert call(srv, "POST", "/v1/guide/ask", {"text": ""})[0] == 400
        assert call(srv, "DELETE", "/v1/guide")[0] in (404, 405, 501)
        assert call(srv, "GET", "/v1/guide", key=None)[0] == 401
        assert call(srv, "GET", "/v1/guide", key="mauvaise")[0] == 401
    finally:
        srv.stop()
        guide_http.configure(None)


def test_flag_off_is_404_and_a_normal_turn_is_identical():
    srv_off, ship_off = start(False)
    try:
        assert call(srv_off, "GET", "/v1/guide")[0] == 404
        assert call(srv_off, "POST", "/v1/guide/ask", {"text": "x"})[0] == 404
        assert chat(srv_off, "bonjour")[0] == 200
    finally:
        srv_off.stop()
    srv_on, ship_on = start(True)
    try:
        assert chat(srv_on, "bonjour")[0] == 200
    finally:
        srv_on.stop()
        guide_http.configure(None)
    assert ship_off.seen and ship_on.seen == ship_off.seen  # le Guide n'entre jamais dans un tour ordinaire
