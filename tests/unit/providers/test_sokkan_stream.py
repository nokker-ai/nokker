"""`SokkanProvider` en streaming (`on_delta`) — même `Completion` qu'en non-streamé, réessais avant le 1er octet
seulement. Aucun réseau : `httpx.MockTransport`."""
from __future__ import annotations

import json

import httpx
import pytest

from nokker.providers.sokkan import SokkanProvider, StreamInterrupted


def _provider(handler) -> SokkanProvider:
    p = SokkanProvider(base_url="https://router.sokkan.ch/v1", api_key="k", model="qwen/qwen3.8-27b",
                       retry_backoff_s=0.0)
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    return p


def _sse(chunks: list[dict | str]) -> bytes:
    out = b": ping amont\n\n"
    for c in chunks:
        payload = c if isinstance(c, str) else json.dumps(c)
        out += f"data: {payload}\n\n".encode()
    return out


def _chunk(delta: dict | None = None, finish: str | None = None, usage: dict | None = None,
           model: str = "qwen/qwen3.8-27b") -> dict:
    obj: dict = {"id": "x", "object": "chat.completion.chunk", "model": model,
                 "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        obj["usage"] = usage
    return obj


USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 64},
         "completion_tokens_details": {"reasoning_tokens": 0}}

TOOL_STREAM = [
    _chunk({"role": "assistant", "content": ""}),
    _chunk({"content": "Je regarde "}),
    _chunk({"content": "ça."}),
    _chunk({"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                            "function": {"name": "calendar_list", "arguments": ""}}]}),
    _chunk({"tool_calls": [{"index": 1, "id": "call_b", "type": "function",
                            "function": {"name": "mail_search", "arguments": "{\"q\":"}}]}),
    _chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{\"day\":"}}]}),
    _chunk({"tool_calls": [{"index": 0, "function": {"arguments": " \"2026-10-19\"}"}}]}),
    _chunk({"tool_calls": [{"index": 1, "function": {"arguments": " \"facture\"}"}}]}),
    _chunk({}, finish="tool_calls"),
    _chunk(None, usage=USAGE),
    "[DONE]",
]

TOOL_JSON = {
    "model": "qwen/qwen3.8-27b",
    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "Je regarde ça.",
        "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "calendar_list", "arguments": "{\"day\": \"2026-10-19\"}"}},
            {"id": "call_b", "type": "function",
             "function": {"name": "mail_search", "arguments": "{\"q\": \"facture\"}"}},
        ]}}],
    "usage": USAGE,
}


def test_stream_rebuilds_the_same_completion_as_non_stream():
    seen_bodies = []

    def stream_handler(request):
        seen_bodies.append(json.loads(request.content))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=_sse(TOOL_STREAM))

    def json_handler(request):
        return httpx.Response(200, json=TOOL_JSON)

    pieces: list[str] = []
    streamed = _provider(stream_handler).complete([{"role": "user", "content": "x"}], [{"type": "function"}],
                                                  reasoning=False, session_key="t", on_delta=pieces.append)
    plain = _provider(json_handler).complete([{"role": "user", "content": "x"}], [{"type": "function"}],
                                             reasoning=False, session_key="t")
    assert seen_bodies[0]["stream"] is True
    assert seen_bodies[0]["stream_options"] == {"include_usage": True}
    assert pieces == ["Je regarde ", "ça."]
    assert streamed.message == plain.message
    assert streamed.usage == plain.usage
    assert streamed.model == plain.model and streamed.finish_reason == plain.finish_reason == "tool_calls"
    assert [tc.arguments for tc in streamed.message.tool_calls] == [{"day": "2026-10-19"}, {"q": "facture"}]
    assert streamed.attempts == 1


def test_without_on_delta_the_body_has_no_stream_field():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=TOOL_JSON)

    _provider(handler).complete([], [], reasoning=False, session_key="t")
    assert "stream" not in seen[0] and "stream_options" not in seen[0]


def test_retry_allowed_before_first_byte():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="occupé")
        return httpx.Response(200, content=_sse([_chunk({"content": "ok"}, finish="stop"), "[DONE]"]))

    pieces: list[str] = []
    comp = _provider(handler).complete([], [], reasoning=False, session_key="t", on_delta=pieces.append)
    assert calls["n"] == 2 and comp.attempts == 2 and pieces == ["ok"]


class _BreaksAfter(httpx.SyncByteStream):
    def __init__(self, first: bytes) -> None:
        self.first = first

    def __iter__(self):
        if self.first:
            yield self.first
        raise httpx.ReadError("connexion coupée")


def test_retry_forbidden_after_first_byte():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, stream=_BreaksAfter(_sse([_chunk({"content": "Début de réponse "})])))

    pieces: list[str] = []
    with pytest.raises(StreamInterrupted):
        _provider(handler).complete([], [], reasoning=False, session_key="t", on_delta=pieces.append)
    assert calls["n"] == 1  # jamais rejoué : le client a déjà vu « Début de réponse »
    assert pieces == ["Début de réponse "]


def test_break_before_any_content_is_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            # rôle seul (aucun contenu remis à on_delta), puis coupure
            return httpx.Response(200, stream=_BreaksAfter(_sse([_chunk({"role": "assistant"})])))
        return httpx.Response(200, content=_sse([_chunk({"content": "ok"}, finish="stop"), "[DONE]"]))

    comp = _provider(handler).complete([], [], reasoning=False, session_key="t", on_delta=lambda p: None)
    assert calls["n"] == 2 and comp.message.content == "ok"


def test_stream_ending_without_done_nor_finish_is_an_error():
    def handler(request):
        return httpx.Response(200, content=_sse([_chunk({"role": "assistant"})]))

    p = _provider(handler)
    p.max_retries = 0
    with pytest.raises(RuntimeError):
        p.complete([], [], reasoning=False, session_key="t", on_delta=lambda x: None)


def test_non_retryable_status_raises_immediately():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, text="mauvaise requête")

    with pytest.raises(RuntimeError, match="HTTP 400"):
        _provider(handler).complete([], [], reasoning=False, session_key="t", on_delta=lambda x: None)
    assert calls["n"] == 1
