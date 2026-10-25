"""Délai d'attente de l'amont AVANT le premier morceau : un essai de plus (lot fix-batch-1 ; banc X9 §5.4, tour 110),
jamais après un morceau — et, depuis la revue du train du 2.11 (défaut 8), jamais au-delà du budget de l'appel
(`call_budget_s`, défaut = `timeout_s`, 180 s) : le temps total, réessais compris, y tient (avant : 180 s + 180 s +
génération, plus de 6 min, et une double facturation possible si l'amont finit la génération abandonnée). Un essai de
plus ne part que s'il reste au moins `MIN_RETRY_S`, avec un délai d'attente réduit à ce qui reste."""
from __future__ import annotations

import json

import httpx
import pytest

from nokker.providers import sokkan as sokkan_mod
from nokker.providers.sokkan import SokkanProvider, StreamInterrupted

OK = {"model": "m", "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
      "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(sokkan_mod.time, "monotonic", c)
    monkeypatch.setattr(sokkan_mod.time, "sleep", lambda s: None)
    return c


def provider(handler):
    p = SokkanProvider(base_url="https://amont.invalid/v1", api_key="k", model="m", retry_backoff_s=0.0)
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    return p


def timing_out(clock, then, n=1):
    """Les `n` premiers essais attendent tout leur délai de lecture (celui de la requête), puis lèvent."""
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) <= n:
            clock.t += request.extensions["timeout"]["read"]
            raise httpx.ReadTimeout("The read operation timed out", request=request)
        return then(request)

    return handler, calls


def test_read_timeout_that_spent_the_whole_call_budget_is_not_retried(clock):
    handler, calls = timing_out(clock, lambda r: httpx.Response(200, json=OK))
    t0 = clock.t
    with pytest.raises(RuntimeError, match="timed out"):
        provider(handler).complete([{"role": "user", "content": "x"}], [], reasoning=False, session_key="t")
    assert len(calls) == 1 and clock.t - t0 <= 180.0  # plus de 180 s + 180 s


def test_read_timeout_is_retried_within_what_is_left_of_the_call_budget(clock):
    handler, calls = timing_out(clock, lambda r: httpx.Response(200, json=OK))
    p = provider(handler)
    p.timeout_s = 60.0  # délai de lecture d'un essai ; budget de l'appel : 180 s
    p.call_budget_s = 180.0
    comp = p.complete([{"role": "user", "content": "x"}], [], reasoning=False, session_key="t")
    assert comp.message.content == "pong" and comp.attempts == 2 and len(calls) == 2
    assert calls[0].extensions["timeout"]["read"] == 60.0
    assert calls[1].extensions["timeout"]["read"] <= 120.0


def test_repeated_timeouts_never_exceed_the_call_budget(clock):
    handler, calls = timing_out(clock, lambda r: httpx.Response(200, json=OK), n=10)
    p = provider(handler)
    p.timeout_s, p.call_budget_s, p.timeout_retries = 100.0, 180.0, 5
    t0 = clock.t
    with pytest.raises(RuntimeError, match="timed out"):
        p.complete([], [], reasoning=False, session_key="t")
    assert clock.t - t0 <= 180.0 and len(calls) == 2  # 100 s, puis les 80 s qui restent, rien de plus


def test_only_one_extra_try_on_repeated_timeouts(clock):
    handler, calls = timing_out(clock, lambda r: httpx.Response(200, json=OK), n=5)
    p = provider(handler)
    p.timeout_s, p.call_budget_s = 90.0, 300.0  # chaque essai dépasse le budget des réessais normaux (80 s)
    with pytest.raises(RuntimeError, match="timed out"):
        p.complete([], [], reasoning=False, session_key="t")
    assert len(calls) == 2


def test_other_network_errors_keep_the_budget_rule(clock):
    calls = []

    def handler(request):
        calls.append(request)
        clock.t += 181.0
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(RuntimeError):
        provider(handler).complete([], [], reasoning=False, session_key="t")
    assert len(calls) == 1


def _sse(*pieces: str) -> bytes:
    chunks = [{"choices": [{"index": 0, "delta": {"content": p}, "finish_reason": None}]} for p in pieces]
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


def test_stream_timeout_before_the_first_piece_is_retried_once_within_the_budget(clock):
    handler, calls = timing_out(clock, lambda r: httpx.Response(200, content=_sse("po", "ng")))
    seen = []
    p = provider(handler)
    p.timeout_s, p.call_budget_s = 60.0, 180.0
    comp = p.complete([], [], reasoning=False, session_key="t", on_delta=seen.append)
    assert comp.message.content == "pong" and "".join(seen) == "pong" and len(calls) == 2
    assert calls[1].extensions["timeout"]["read"] <= 120.0


def test_stream_timeout_that_spent_the_whole_call_budget_is_not_retried(clock):
    handler, calls = timing_out(clock, lambda r: httpx.Response(200, content=_sse("po", "ng")))
    t0 = clock.t
    with pytest.raises(RuntimeError, match="timed out"):
        provider(handler).complete([], [], reasoning=False, session_key="t", on_delta=lambda s: None)
    assert len(calls) == 1 and clock.t - t0 <= 180.0


def test_stream_timeout_after_the_first_piece_is_never_retried(clock):
    calls = []

    class Stalls(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices": [{"index": 0, "delta": {"content": "po"}}]}\n\n'
            clock.t += 181.0
            raise httpx.ReadTimeout("The read operation timed out")

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=Stalls())

    with pytest.raises(StreamInterrupted):
        provider(handler).complete([], [], reasoning=False, session_key="t", on_delta=lambda s: None)
    assert len(calls) == 1
