"""Streaming de `/v1/chat/completions` (P1, 19.10.2026) — battement, opt-in, reset, contrat passerelle amont.

Vrai serveur HTTP en boucle locale (mêmes fakes que `test_server.py`). Le lecteur `gateway_read` reproduit
`gateway/run_turn.py::_run_agent_via_proxy` + `_consume_sse_line` (hermes-nokker, `:2827-2880`) : lecture par
morceaux arbitraires, découpe sur `\\n`, seules les lignes `data: ` comptent, concaténation de TOUT
`choices[0].delta.content`, arrêt sur `[DONE]`.
"""
from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass, field

import pytest

from nokker.api import server as server_mod
from nokker.core.loop import Kernel
from nokker.core.ports import Completion
from nokker.core.turn import Message, Tier, ToolCall, Usage
from nokker.ledger.costs import InMemoryLedger
from nokker.store.pg import MemoryStore
from tests.unit.api.test_server import (
    AllowGates, AllowInvariants, EmptyTools, FixedContext, NoEscalator, NullMem, ShipOnlyPolicy, start_server,
)


@dataclass
class SlowShip:
    """Provider SANS `on_delta` (comme tous les faux providers existants), lent."""

    reply: str = "Réponse lente."
    delay_s: float = 0.4

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        time.sleep(self.delay_s)
        return Completion(message=Message(role="assistant", content=self.reply),
                          usage=Usage(prompt=12, completion=4), model="qwen/qwen3.8-27b", latency_s=self.delay_s)


@dataclass
class StreamingShip:
    """Provider qui sait streamer : chaque appel scripté = (morceaux, tool_calls)."""

    script: list[tuple[list[str], list[ToolCall]]]
    seen_on_delta: list[bool] = field(default_factory=list)
    pause_s: float = 0.0

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048, on_delta=None):
        pieces, tool_calls = self.script[len(self.seen_on_delta)]
        self.seen_on_delta.append(on_delta is not None)
        for p in pieces:
            if on_delta is not None:
                on_delta(p)
            time.sleep(self.pause_s)
        return Completion(message=Message(role="assistant", content="".join(pieces), tool_calls=tool_calls),
                          usage=Usage(prompt=10, completion=len(pieces)), model="qwen/qwen3.8-27b", latency_s=0.01)


def kernel_with(provider) -> Kernel:
    return Kernel(store=MemoryStore(), context=FixedContext(), memory=NullMem(), invariants=AllowInvariants(),
                  gates=AllowGates(), escalator=NoEscalator(), policy=ShipOnlyPolicy(),
                  providers={Tier.SHIP: provider}, tools=EmptyTools(), ledger=InMemoryLedger())


def raw_post(port: int, *, tokens: bool, thread: str = "t-stream") -> tuple[bytes, float, float]:
    """POST brut (socket) : renvoie (corps HTTP dé-chunké, délai avant en-têtes, délai total)."""
    body = json.dumps({"model": "hermes-agent", "messages": [{"role": "user", "content": "salut"}],
                       "stream": True}).encode()
    extra = "X-Nokker-Stream-Tokens: 1\r\n" if tokens else ""
    req = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
           f"X-Hermes-Session-Id: {thread}\r\n{extra}Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
           ).encode() + body
    t0 = time.monotonic()
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(req)
    data = b""
    t_headers = None
    while True:
        part = s.recv(65536)
        if not part:
            break
        data += part
        if t_headers is None and b"\r\n\r\n" in data:
            t_headers = time.monotonic() - t0
        if data.endswith(b"\r\n0\r\n\r\n") or data.endswith(b"\r\n\r\n0\r\n\r\n"):
            break  # dernier chunk (le serveur garde la connexion : `Connection: keep-alive`)
    s.close()
    head, _, chunked = data.partition(b"\r\n\r\n")
    assert b" 200 " in head.split(b"\r\n")[0]
    out = b""
    while chunked:
        size_line, _, rest = chunked.partition(b"\r\n")
        n = int(size_line, 16)
        if n == 0:
            break
        out += rest[:n]
        chunked = rest[n + 2:]
    return out, t_headers or 0.0, time.monotonic() - t0


def _writes(body: bytes, piece: int | None) -> list[bytes]:
    """Découpe du flux telle que `resp.content.iter_any()` la rend : une écriture serveur (une trame SSE) par
    morceau, ou des morceaux de `piece` octets coupés seulement sur des octets ASCII. NB : la gateway décode
    CHAQUE morceau à part (`errors="replace"`) — un caractère multi-octet coupé par le réseau y serait abîmé ;
    défaut préexistant de la gateway, indépendant de ce chantier (une trame = une écriture, ici comme avant)."""
    frames = [f + b"\n\n" for f in body.split(b"\n\n") if f]
    if piece is None:
        return frames
    out: list[bytes] = []
    for f in frames:
        cur = b""
        for b in f:
            cur += bytes([b])
            if len(cur) >= piece and b < 0x80:
                out.append(cur)
                cur = b""
        if cur:
            out.append(cur)
    return out


def gateway_read(body: bytes, piece: int | None = None) -> tuple[str, bool]:
    """`_run_agent_via_proxy` : morceaux → lignes → `_consume_sse_line`."""
    full, saw_done, buffer = "", False, ""

    def consume(line: str) -> bool:
        nonlocal full
        line = line.strip()
        if not line.startswith("data: "):
            return False
        data = line[6:]
        if data.strip() == "[DONE]":
            return True
        try:
            choices = json.loads(data).get("choices") or []
            content = choices[0].get("delta", {}).get("content", "") if choices else ""
        except (json.JSONDecodeError, TypeError, AttributeError, IndexError):
            return False
        if content:
            full += content
        return False

    for chunk in _writes(body, piece):
        if saw_done:
            break
        buffer += chunk.decode("utf-8", errors="replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            if consume(line):
                saw_done = True
                break
    if not saw_done and buffer:
        saw_done = consume(buffer)
    return full, saw_done


def data_frames(body: bytes) -> list[str]:
    return [f[len("data: "):] for f in body.decode().split("\n\n") if f.startswith("data: ")]


def web_read(body: bytes) -> str:
    """Ce que fait le web (SDK + store) : concatène les deltas, `nokker.reset` vide le texte en cours AVANT
    d'ajouter le delta du même chunk."""
    text = ""
    for f in data_frames(body):
        if f == "[DONE]":
            break
        obj = json.loads(f)
        if (obj.get("nokker") or {}).get("reset"):
            text = ""
        text += (obj["choices"][0]["delta"].get("content") or "")
    return text


@pytest.fixture
def fast_ping(monkeypatch):
    monkeypatch.setattr(server_mod, "STREAM_PING_S", 0.05)


def test_heartbeat_ping_before_reply_and_headers_immediately(fast_ping):
    srv = start_server(kernel_with(SlowShip(delay_s=0.4)))
    try:
        body, t_headers, total = raw_post(srv.port, tokens=False)
    finally:
        srv.stop()
    assert t_headers < 0.3 < total  # en-têtes partis AVANT la fin du tour
    first_data = body.index(b"data: ")
    assert b": ping\n\n" in body[:first_data]
    assert gateway_read(body) == ("Réponse lente.", True)


def test_without_opt_in_body_is_exactly_the_old_one_except_pings(fast_ping):
    ship = StreamingShip(script=[(["Bonjour ", "Alice, ", "tout va bien."], [])], pause_s=0.03)
    srv = start_server(kernel_with(ship))
    try:
        body, _, _ = raw_post(srv.port, tokens=False)
    finally:
        srv.stop()
    assert ship.seen_on_delta == [False]  # sans opt-in, le provider n'est même pas streamé
    # Pings retirés, le reste est la forme d'avant : rôle, réponse + usage, stop, [DONE] — rien d'autre.
    frames = [f for f in body.decode().split("\n\n") if f]
    assert all(f == ": ping" or f.startswith("data: ") for f in frames)
    data = data_frames(body)
    assert len(data) == 4 and data[3] == "[DONE]"
    role, content, stop = (json.loads(d) for d in data[:3])
    for obj in (role, content, stop):
        assert set(obj) <= {"id", "object", "created", "model", "choices", "usage"}
        assert obj["object"] == "chat.completion.chunk" and obj["model"] == "qwen/qwen3.8-27b"
    assert role["choices"] == [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
    assert content["choices"] == [{"index": 0, "delta": {"content": "Bonjour Alice, tout va bien."},
                                   "finish_reason": None}]
    assert content["usage"]["prompt_tokens"] == 10
    assert stop["choices"] == [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    assert b"nokker" not in body
    assert gateway_read(body, piece=3) == ("Bonjour Alice, tout va bien.", True)


def test_opt_in_streams_tokens_and_final_chunk_is_authoritative():
    long_text = "Voici une réponse assez longue pour dépasser la retenue de quatre-vingts caractères, " \
                "écrite en plusieurs morceaux successifs pour vérifier le flux en direct de bout en bout."
    pieces = [long_text[i:i + 9] for i in range(0, len(long_text), 9)]
    ship = StreamingShip(script=[(pieces, [])])
    srv = start_server(kernel_with(ship))
    try:
        body, _, _ = raw_post(srv.port, tokens=True)
    finally:
        srv.stop()
    assert ship.seen_on_delta == [True]
    data = [json.loads(d) for d in data_frames(body) if d != "[DONE]"]
    live = [d for d in data if not d.get("nokker") and d["choices"][0]["delta"].get("content")]
    assert live, "aucun jeton en direct"
    streamed = "".join(d["choices"][0]["delta"]["content"] for d in live)
    assert long_text.startswith(streamed) and len(streamed) <= len(long_text) - 80  # retenue respectée
    final = [d for d in data if (d.get("nokker") or {}).get("final")]
    assert len(final) == 1 and final[0]["choices"][0]["delta"]["content"] == long_text
    assert final[0]["usage"]["completion_tokens"] == len(pieces)
    assert web_read(body) == long_text


def test_opt_in_tool_call_text_is_reset_for_the_client():
    filler = "Je vais chercher ça dans les outils disponibles avant de te répondre, une petite seconde stp. "
    ship = StreamingShip(script=[
        ([filler, filler], [ToolCall(id="c1", name="tool_find", arguments={"query": "agenda"})]),
        (["C'est fait : rien à l'agenda aujourd'hui, journée libre."], []),
    ])
    srv = start_server(kernel_with(ship))
    try:
        body, _, _ = raw_post(srv.port, tokens=True)
    finally:
        srv.stop()
    data = [json.loads(d) for d in data_frames(body) if d != "[DONE]"]
    kinds = []
    for d in data:
        if (d.get("nokker") or {}).get("final"):
            kinds.append("final")
        elif (d.get("nokker") or {}).get("reset"):
            kinds.append("reset")
        elif d["choices"][0]["delta"].get("content"):
            kinds.append("delta")
    assert "delta" in kinds and kinds.index("delta") < kinds.index("reset") < kinds.index("final")
    assert web_read(body) == "C'est fait : rien à l'agenda aujourd'hui, journée libre."


def test_gateway_never_sees_partial_text_even_with_a_streaming_provider(fast_ping):
    """Passerelle amont (pas d'en-tête opt-in) + provider qui streame + appel en outils : elle ne reçoit QUE la
    réponse finale, une fois (aucun texte de l'appel en outils, aucun reset)."""
    ship = StreamingShip(script=[
        (["Texte intermédiaire qui ne doit jamais atteindre Telegram. " * 3],
         [ToolCall(id="c1", name="tool_find", arguments={"query": "x"})]),
        (["Réponse finale."], []),
    ], pause_s=0.02)
    srv = start_server(kernel_with(ship))
    try:
        body, _, _ = raw_post(srv.port, tokens=False)
    finally:
        srv.stop()
    assert ship.seen_on_delta == [False, False]
    assert gateway_read(body) == ("Réponse finale.", True)
    assert b"interm" not in body


def test_turn_error_after_headers_is_reported_in_stream_without_done(monkeypatch):
    kernel = kernel_with(SlowShip(delay_s=0.0))

    def boom(inp, stream=None):
        raise RuntimeError("panne interne")

    monkeypatch.setattr(kernel, "run_turn", boom)
    srv = start_server(kernel)
    try:
        body, _, _ = raw_post(srv.port, tokens=False)
    finally:
        srv.stop()
    data = data_frames(body)
    assert json.loads(data[-1])["error"]["message"] == "panne interne"
    assert "[DONE]" not in data
    assert gateway_read(body) == ("", False)  # la gateway rapporte « fermé trop tôt », pas un texte vide
