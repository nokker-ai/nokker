"""Client du tunnel (nokker/local/tunnel.py) contre un faux relais (websockets.serve) et un faux noyau HTTP local :
une requête JSON, un corps découpé, un flux SSE, un ping, l'état up/down. Ignoré sans `websockets` (CI unitaire)."""
from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

websockets = pytest.importorskip("websockets")

from nokker.local.tunnel import SUBPROTOCOL, Tunnel  # noqa: E402


class Core(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/v1/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(3):
                self.wfile.write(f"data: {i}\n\n".encode())
                self.wfile.flush()
            return
        body = json.dumps({"path": self.path, "host": self.headers.get("Host"),
                           "relay": self.headers.get("X-Nokker-Relay")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data[::-1])


def test_tunnel_relays_json_bodies_and_sse():
    core = ThreadingHTTPServer(("127.0.0.1", 0), Core)
    threading.Thread(target=core.serve_forever, daemon=True).start()
    seen: dict = {"auth": None, "proto": None, "frames": []}
    done = threading.Event()
    states: list[str] = []

    async def relay(ws):
        seen["auth"] = ws.request.headers.get("Authorization")
        seen["proto"] = ws.subprotocol
        hdr = [["Host", "pierre.nokker.app"], ["X-Nokker-Relay", "1"]]
        await ws.send(json.dumps({"t": "req", "id": 1, "method": "GET", "path": "/v1/local/machine", "headers": hdr,
                                  "body": "", "more": False}))
        big = b"a" * 300_000 + b"Z"
        await ws.send(json.dumps({"t": "req", "id": 2, "method": "POST", "path": "/v1/echo", "headers": hdr,
                                  "body": base64.b64encode(big[:200_000]).decode(), "more": True}))
        await ws.send(json.dumps({"t": "body", "id": 2, "data": base64.b64encode(big[200_000:]).decode(),
                                  "more": False}))
        await ws.send(json.dumps({"t": "req", "id": 3, "method": "GET", "path": "/v1/events", "headers": hdr,
                                  "body": "", "more": False}))
        await ws.send(json.dumps({"t": "ping"}))
        async for raw in ws:
            seen["frames"].append(json.loads(raw))
            ends = [f for f in seen["frames"] if f.get("t") in ("res", "body") and not f.get("more")]
            if len(ends) >= 3 and any(f.get("t") == "pong" for f in seen["frames"]):
                done.set()
                return

    async def serve(stop: threading.Event, port_box: list):
        async with websockets.serve(relay, "127.0.0.1", 0, subprotocols=[SUBPROTOCOL]) as srv:
            port_box.append(srv.sockets[0].getsockname()[1])
            while not stop.is_set():
                await asyncio.sleep(0.05)

    stop, port_box = threading.Event(), []
    threading.Thread(target=lambda: asyncio.run(serve(stop, port_box)), daemon=True).start()
    while not port_box:
        time.sleep(0.02)
    t = Tunnel(url=f"ws://127.0.0.1:{port_box[0]}/v1/tunnel", machine_token="mt-1",
               local_base=f"http://127.0.0.1:{core.server_address[1]}", on_up=lambda: states.append("up"),
               on_down=lambda: states.append("down"))
    t.start()
    try:
        assert done.wait(10), seen["frames"]
    finally:
        t.stop()
        stop.set()
        core.shutdown()
    assert seen["auth"] == "Bearer mt-1" and seen["proto"] == SUBPROTOCOL and states[0] == "up"
    f = seen["frames"]
    r1 = next(x for x in f if x.get("t") == "res" and x["id"] == 1)
    body1 = json.loads(base64.b64decode(r1["body"]))
    assert r1["status"] == 200 and body1["host"] == "pierre.nokker.app" and body1["relay"] == "1"
    echo = b"".join(base64.b64decode(x.get("body") or x.get("data") or "") for x in f if x.get("id") == 2)
    assert echo == (b"a" * 300_000 + b"Z")[::-1]  # corps reçu en deux trames, réponse découpée en trames
    sse = b"".join(base64.b64decode(x.get("data") or "") for x in f if x.get("id") == 3 and x["t"] == "body")
    assert b"data: 0" in sse and b"data: 2" in sse
