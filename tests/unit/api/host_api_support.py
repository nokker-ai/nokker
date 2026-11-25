"""Appui commun aux tests de l'API hôte (docs/NOKKAN-HOST-API.md) : un noyau à modèle scripté, servi par le VRAI
serveur (`make_handler`) avec l'API locale, le hub et le bus d'événements."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from nokker.api.events import EventBus
from nokker.api.server import build_hub, make_handler
from nokker.config import NokkerConfig
from nokker.ledger.costs import InMemoryLedger
from nokker.ledger.prices import PriceCatalog
from nokker.local.api import LOCAL_OWNER
from nokker.tools.registry import Registry, builtin_core_source
from tests.unit.test_local_api import _api
from tests.unit.test_loop_contract import kernel as scripted_kernel


def host_kernel(script, *, clarify_options: bool = False, prices: PriceCatalog | None = None):
    """Noyau de `test_loop_contract` avec le VRAI registre des outils cœur et un vrai ledger en mémoire."""
    k, _tools, ship, _deep = scripted_kernel(script)
    k.tools = Registry(sources=[builtin_core_source(clarify_options=clarify_options)])
    k.ledger = InMemoryLedger(prices=prices if prices is not None else PriceCatalog(entries={}))
    return k, ship


class Served:
    def __init__(self, tmp_path, kernel):
        self.kernel = kernel
        self.bus = EventBus()
        self.hub = build_hub(NokkerConfig(web_search_enabled=False, approvals_enabled=False), self.bus)
        kernel.observer = self.hub
        self.api = _api(tmp_path)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            kernel, {self.api.token: LOCAL_OWNER}, kernel.ledger, local_api=self.api, hub=self.hub, events=self.bus))
        self.port = self.httpd.server_address[1]
        self.api.allowed_hosts = frozenset({f"127.0.0.1:{self.port}"})
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.cwd = str(tmp_path / "projets" / "app")

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def req(self, path, *, body=None, method=None, token: str | None = "", headers=None, raw: bool = False):
        h = {"Content-Type": "application/json", **(headers or {})}
        tok = self.api.token if token == "" else token
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=h, method=method,
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                data = resp.read()
                return resp.status, (data if raw else json.loads(data or b"{}"))
        except urllib.error.HTTPError as e:
            data = e.read()
            return e.code, (data if raw else json.loads(data or b"{}"))

    def session(self, **extra):
        return self.req("/v1/local/sessions", body={"cwd": self.cwd, **extra})

    def chat(self, thread, text, *, stream_tokens: bool = False):
        """Un tour par `POST /v1/chat/completions`. En flux : la liste des objets `data:` (sans `[DONE]`)."""
        headers = {"X-Session-Id": thread, **({"X-Nokker-Stream-Tokens": "1"} if stream_tokens else {})}
        body = {"messages": [{"role": "user", "content": text}], **({"stream": True} if stream_tokens else {})}
        status, out = self.req("/v1/chat/completions", body=body, headers=headers, raw=stream_tokens)
        if not stream_tokens:
            return status, out
        chunks = [json.loads(line[5:]) for line in out.decode().splitlines()
                  if line.startswith("data:") and line[5:].strip() != "[DONE]"]
        return status, chunks

    def events(self, name: str | None = None):
        return [(e.event_type, {k: v for k, v in e.data.items() if not k.startswith("_")})
                for e in self.bus.replay_since(0) if name is None or e.event_type == name]
