"""`api/server.py` — contrat SSE/non-stream, auth, /healthz. Aucun réseau externe : un `Kernel` complet
avec des fakes (même esprit que `tests/unit/test_loop_contract.py`) et une vraie requête HTTP en boucle
locale (`127.0.0.1`, port éphémère) pour exercer le VRAI code HTTP (pas une simulation en mémoire).

La requête envoyée reproduit EXACTEMENT ce que `gateway/run_turn.py::_run_agent_via_proxy` (hermes-nokker)
émet : `POST /v1/chat/completions`, `X-Hermes-Session-Id`, corps `{"model": "hermes-agent", "messages": [...],
"stream": true}` — et la lit avec le même protocole SSE que `nokker_bench.py::chat_nokker_api`
(dépôt interne). Les deux sources sont citées dans `nokker/api/server.py`.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import ThreadingHTTPServer

from nokker.api.server import make_handler
from nokker.core.loop import Kernel
from nokker.core.ports import Assembled, Completion
from nokker.core.turn import Message, Tier, Usage
from nokker.ledger.costs import InMemoryLedger
from nokker.store.pg import MemoryStore


@dataclass
class FixedContext:
    def assemble(self, turn, history, recall, loaded, tier):
        return Assembled(messages=[{"role": "user", "content": turn.input.text}], tools=[],
                         prefix_sha256="fixed")

    def admit_result(self, r):
        return r


@dataclass
class NullMem:
    def recall(self, q, k=5):
        return []

    def propose(self, t, f):
        pass


@dataclass
class AllowInvariants:
    def check_input(self, text):
        from nokker.core.turn import InputCheck
        return InputCheck(text=text)

    def check_action(self, call, spec, turn):
        from nokker.core.turn import Decision, Gate, Verdict
        return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="allow-all-test")

    def redact_result(self, result, spec):
        return result

    def scrub_output(self, text):
        return text


@dataclass
class AllowGates:
    def action(self, call, spec, turn):
        return []

    def switch(self, reply, turn, tier):
        return []


@dataclass
class NoEscalator:
    def escalate(self, turn, reason):
        return f"escaladé: {reason}"


@dataclass
class ShipOnlyPolicy:
    def choose(self, state):
        from nokker.core.ports import RouteDecision
        return RouteDecision(tier=Tier.SHIP, reasoning=False, reason="test")


@dataclass
class EmptyTools:
    def core(self):
        return []

    def spec(self, name):
        return None

    def find(self, query, limit=5):
        return []

    def index(self):
        return []

    def call(self, name, arguments, ctx):
        raise AssertionError("aucun outil ne devrait être appelé dans ces tests")


@dataclass
class ScriptedShip:
    reply: str = "2 rendez-vous aujourd'hui."

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        return Completion(message=Message(role="assistant", content=self.reply),
                          usage=Usage(prompt=12, completion=4), model="qwen/qwen3.8-27b", latency_s=0.05)


def make_kernel(reply="2 rendez-vous aujourd'hui.") -> Kernel:
    return Kernel(store=MemoryStore(), context=FixedContext(), memory=NullMem(), invariants=AllowInvariants(),
                 gates=AllowGates(), escalator=NoEscalator(), policy=ShipOnlyPolicy(),
                 providers={Tier.SHIP: ScriptedShip(reply)}, tools=EmptyTools(), ledger=InMemoryLedger())


@dataclass
class _Server:
    httpd: ThreadingHTTPServer
    thread: threading.Thread
    port: int

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)


def start_server(kernel: Kernel, api_keys: dict[str, str] | None = None, ledger=None) -> _Server:
    handler = make_handler(kernel, api_keys or {}, ledger)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return _Server(httpd=httpd, thread=thread, port=httpd.server_address[1])


def parse_sse(raw_lines: list[str]) -> tuple[str, dict, bool]:
    """Reproduit `nokker_bench.py::chat_nokker_api` : concatène `delta.content`, garde le dernier `usage`."""
    content, usage, saw_done = "", {}, False
    for line in raw_lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            saw_done = True
            break
        ev = json.loads(payload)
        if ev.get("usage"):
            usage = ev["usage"]
        for ch in ev.get("choices", []):
            delta = ch.get("delta") or {}
            if delta.get("content"):
                content += delta["content"]
    return content, usage, saw_done


def test_healthz():
    srv = start_server(make_kernel())
    try:
        with urllib.request.urlopen(srv.url("/healthz"), timeout=5) as resp:
            assert resp.status == 200
            assert json.loads(resp.read())["status"] == "ok"
    finally:
        srv.stop()


def test_stream_matches_gateway_proxy_request_shape():
    srv = start_server(make_kernel("Je transmets à Alice : TVA à vérifier."))
    try:
        body = json.dumps({"model": "hermes-agent",
                           "messages": [{"role": "user", "content": "quel taux de TVA ?"}],
                           "stream": True}).encode()
        req = urllib.request.Request(srv.url("/v1/chat/completions"), data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-Hermes-Session-Id": "session-42",
                                              "Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == "text/event-stream"
            lines = [line.decode("utf-8") for line in resp]
        content, usage, saw_done = parse_sse(lines)
        assert content == "Je transmets à Alice : TVA à vérifier."
        assert saw_done is True
        assert usage["prompt_tokens"] == 12 and usage["completion_tokens"] == 4
    finally:
        srv.stop()


def test_thread_id_comes_from_x_hermes_session_id():
    kernel = make_kernel()
    srv = start_server(kernel)
    try:
        for i in range(2):
            body = json.dumps({"model": "hermes-agent",
                               "messages": [{"role": "user", "content": f"msg {i}"}],
                               "stream": False}).encode()
            req = urllib.request.Request(srv.url("/v1/chat/completions"), data=body, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "X-Hermes-Session-Id": "fil-stable"})
            urllib.request.urlopen(req, timeout=10).read()
        history = kernel.store.history("fil-stable")
        assert [t.index for t in history] == [0, 1]
    finally:
        srv.stop()


def test_non_stream_response_shape():
    srv = start_server(make_kernel("ok"))
    try:
        body = json.dumps({"model": "hermes-agent", "messages": [{"role": "user", "content": "x"}],
                           "stream": False}).encode()
        req = urllib.request.Request(srv.url("/v1/chat/completions"), data=body, method="POST",
                                     headers={"X-Hermes-Session-Id": "s1"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["content"] == "ok"
        assert data["choices"][0]["finish_reason"] == "stop"
    finally:
        srv.stop()


def test_auth_rejects_missing_bearer():
    srv = start_server(make_kernel(), api_keys={"secret123": "alice"})
    try:
        body = json.dumps({"model": "hermes-agent", "messages": [], "stream": False}).encode()
        req = urllib.request.Request(srv.url("/v1/chat/completions"), data=body, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            raised = False
        except urllib.error.HTTPError as e:
            raised = e.code == 401
        assert raised
    finally:
        srv.stop()


def test_auth_rejects_unknown_bearer():
    srv = start_server(make_kernel(), api_keys={"secret123": "alice"})
    try:
        body = json.dumps({"model": "hermes-agent", "messages": [], "stream": False}).encode()
        req = urllib.request.Request(srv.url("/v1/chat/completions"), data=body, method="POST",
                                     headers={"Authorization": "Bearer pas-la-bonne-cle"})
        try:
            urllib.request.urlopen(req, timeout=5)
            raised = False
        except urllib.error.HTTPError as e:
            raised = e.code == 401
        assert raised
    finally:
        srv.stop()


def test_auth_accepts_correct_bearer():
    srv = start_server(make_kernel("ok"), api_keys={"secret123": "alice"})
    try:
        body = json.dumps({"model": "hermes-agent", "messages": [{"role": "user", "content": "x"}],
                           "stream": False}).encode()
        req = urllib.request.Request(srv.url("/v1/chat/completions"), data=body, method="POST",
                                     headers={"Authorization": "Bearer secret123"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
    finally:
        srv.stop()


def test_unknown_path_is_404():
    srv = start_server(make_kernel())
    try:
        try:
            urllib.request.urlopen(srv.url("/nope"), timeout=5)
            raised = False
        except urllib.error.HTTPError as e:
            raised = e.code == 404
        assert raised
    finally:
        srv.stop()


def _post(srv: _Server, thread_id: str, *, bearer=None, user=None, header=None) -> None:
    """`user`/`header` : si fournis, on vérifie justement qu'ils sont IGNORÉS (sécurité du sender)."""
    body: dict = {"model": "hermes-agent", "messages": [{"role": "user", "content": "x"}], "stream": False}
    if user is not None:
        body["user"] = user
    headers = {"X-Hermes-Session-Id": thread_id}
    if header is not None:
        headers["X-Hermes-User-Id"] = header
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(srv.url("/v1/chat/completions"), data=json.dumps(body).encode(),
                                 method="POST", headers=headers)
    urllib.request.urlopen(req, timeout=10).read()


def test_sender_is_none_on_open_server_no_api_keys_configured():
    # Pas de api_keys configurés = serveur ouvert (dev) : AUCUNE identité de confiance, jamais de GO.
    kernel = make_kernel()
    srv = start_server(kernel)
    try:
        _post(srv, "fil-1")
        assert kernel.store.history("fil-1")[0].sender is None
    finally:
        srv.stop()


def test_sender_comes_from_the_bearer_that_authenticated_the_request():
    kernel = make_kernel()
    srv = start_server(kernel, api_keys={"key-alice": "alice", "key-bruno": "bruno"})
    try:
        _post(srv, "fil-2", bearer="key-bruno")
        assert kernel.store.history("fil-2")[0].sender == "bruno"
        _post(srv, "fil-2b", bearer="key-alice")
        assert kernel.store.history("fil-2b")[0].sender == "alice"
    finally:
        srv.stop()


def test_sender_ignores_client_controlled_body_user_field():
    # Sécurité : body["user"] est un champ CLIENT — ne doit JAMAIS devenir Turn.sender.
    kernel = make_kernel()
    srv = start_server(kernel, api_keys={"key-alice": "alice"})
    try:
        _post(srv, "fil-3", bearer="key-alice", user="quelquun-d-autre")
        assert kernel.store.history("fil-3")[0].sender == "alice"
    finally:
        srv.stop()


def test_sender_ignores_arbitrary_client_header():
    # Sécurité : un en-tête que n'importe quel appelant peut poser ne doit pas non plus devenir sender.
    kernel = make_kernel()
    srv = start_server(kernel, api_keys={"key-alice": "alice"})
    try:
        _post(srv, "fil-3b", bearer="key-alice", header="1000000001")
        assert kernel.store.history("fil-3b")[0].sender == "alice"
    finally:
        srv.stop()


def test_channel_is_api_for_http_requests():
    kernel = make_kernel()
    srv = start_server(kernel)
    try:
        _post(srv, "fil-4")
        assert kernel.store.history("fil-4")[0].channel == "api"
    finally:
        srv.stop()


def test_metrics_endpoint_renders_ledger():
    from nokker.ledger.costs import InMemoryLedger as RealLedger

    ledger = RealLedger()
    kernel = Kernel(store=MemoryStore(), context=FixedContext(), memory=NullMem(),
                    invariants=AllowInvariants(), gates=AllowGates(), escalator=NoEscalator(),
                    policy=ShipOnlyPolicy(), providers={Tier.SHIP: ScriptedShip("ok")},
                    tools=EmptyTools(), ledger=ledger)
    srv = start_server(kernel, ledger=ledger)
    try:
        _post(srv, "fil-metrics")
        with urllib.request.urlopen(srv.url("/metrics"), timeout=5) as resp:
            assert resp.status == 200
            text = resp.read().decode()
        assert "nokker" in text  # au moins une métrique nommée nokker_* (format Prometheus)
    finally:
        srv.stop()


def test_metrics_404_without_ledger():
    srv = start_server(make_kernel(), ledger=None)
    try:
        try:
            urllib.request.urlopen(srv.url("/metrics"), timeout=5)
            raised = False
        except urllib.error.HTTPError as e:
            raised = e.code == 404
        assert raised
    finally:
        srv.stop()


def test_session_thread_by_sender_fixes_thread_and_closes_episode_on_session_change():
    closed = []

    class Ctx(FixedContext):
        def new_episode(self, thread_id, reason="marker"):
            closed.append((thread_id, reason))

    kernel = make_kernel()
    kernel.context = Ctx()
    handler = make_handler(kernel, {"tok": "gw"}, None, session_thread_by_sender={"gw": "gw-fixed"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        for sid in ("s1", "s1", "s2"):
            body = json.dumps({"model": "hermes-agent", "messages": [{"role": "user", "content": "x"}],
                               "stream": False}).encode()
            req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions",
                                         data=body, method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer tok",
                                                  "X-Hermes-Session-Id": sid})
            urllib.request.urlopen(req, timeout=10).read()
        assert closed == [("gw-fixed", "marker")]
        assert set(kernel.store.threads()) == {"gw-fixed"} if hasattr(kernel.store, "threads") else True
    finally:
        httpd.shutdown()
