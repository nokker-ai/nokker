"""503 du Router (`upstream_timeout` / `no_upstream` + `Retry-After` + `x-request-id`) : rejoué UNE seule fois,
`max_retries` ou pas ; attente = `Retry-After` (secondes OU date HTTP), au plus 30 s (au-delà : aucun rejeu), 1 s si
absent ou invalide ; aucun rejeu si l'attente ne tient ni dans `total_budget_s` ni dans la marge de l'appel. 502, 504,
429 et le délai d'attente client : inchangés (un seul rejeu au total pour un délai d'attente). POST et SSE."""
from __future__ import annotations

import json
import logging
from email.utils import format_datetime
from datetime import datetime, timezone

import httpx
import pytest

from nokker.providers import sokkan as sokkan_mod
from nokker.providers.sokkan import SokkanProvider

OK = {"model": "m", "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
      "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
BODY = json.dumps({"error": {"code": "upstream_timeout", "message": "x"}})
NOW = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    sleeps: list[float] = []
    monkeypatch.setattr(sokkan_mod.time, "monotonic", c)
    monkeypatch.setattr(sokkan_mod.time, "time", lambda: NOW)

    def fake_sleep(s):
        sleeps.append(s)
        c.t += s

    monkeypatch.setattr(sokkan_mod.time, "sleep", fake_sleep)
    c.sleeps = sleeps
    return c


def _sse() -> bytes:
    chunks = [{"choices": [{"index": 0, "delta": {"content": "pong"}, "finish_reason": None}]},
              {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


def provider(handler, **kw):
    p = SokkanProvider(base_url="https://amont.invalid/v1", api_key="k", model="m", retry_backoff_s=0.0, **kw)
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    return p


def run(p, stream):
    return p.complete([{"role": "user", "content": "x"}], [], reasoning=False, session_key="t",
                      on_delta=(lambda s: None) if stream else None)


def r503(retry_after=None, body=BODY):
    h = {"x-request-id": "req-42"}
    if retry_after is not None:
        h["Retry-After"] = retry_after
    return httpx.Response(503, text=body, headers=h)


def seq(*responses):
    calls = []

    def handler(request):
        i = len(calls)
        calls.append(request)
        r = responses[min(i, len(responses) - 1)]
        return r() if callable(r) else r

    return handler, calls


def ok(stream):
    return lambda: httpx.Response(200, content=_sse()) if stream else httpx.Response(200, json=OK)


@pytest.mark.parametrize("stream", [False, True])
def test_503_then_200_waits_retry_after(clock, stream):
    handler, calls = seq(r503("2"), ok(stream))
    comp = run(provider(handler), stream)
    assert comp.message.content == "pong" and len(calls) == 2 and clock.sleeps == [2.0]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("max_retries", [0, 2, 5])
def test_two_503_in_a_row_is_one_replay_then_error_with_the_code(clock, stream, max_retries):
    handler, calls = seq(r503("1"))
    with pytest.raises(RuntimeError) as e:
        run(provider(handler, max_retries=max_retries), stream)
    assert len(calls) == 2 and "upstream_timeout" in str(e.value) and "HTTP 503" in str(e.value)
    assert getattr(e.value, "error_code", None) == "upstream_timeout"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("ra", ["31", "120"])
def test_retry_after_above_30_s_is_never_replayed(clock, stream, ra):
    handler, calls = seq(r503(ra), ok(stream))
    with pytest.raises(RuntimeError, match="503"):
        run(provider(handler), stream)
    assert len(calls) == 1 and clock.sleeps == []


@pytest.mark.parametrize("stream", [False, True])
def test_retry_after_30_replays_when_the_budget_allows(clock, stream):
    handler, calls = seq(r503("30"), ok(stream))
    p = provider(handler, total_budget_s=80.0)
    assert run(p, stream).message.content == "pong" and len(calls) == 2 and clock.sleeps == [30.0]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("kw", [{"total_budget_s": 29.0}, {"total_budget_s": 30.0},
                                {"call_budget_s": 44.0, "timeout_s": 44.0}])
def test_retry_after_30_is_not_replayed_without_room(clock, stream, kw):
    handler, calls = seq(r503("30"), ok(stream))
    with pytest.raises(RuntimeError, match="503"):
        run(provider(handler, **kw), stream)
    assert len(calls) == 1 and clock.sleeps == []


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("ra", [None, "abc", "-5", "", "1.5"])
def test_absent_or_invalid_retry_after_waits_one_second(clock, stream, ra):
    handler, calls = seq(r503(ra), ok(stream))
    assert run(provider(handler), stream).message.content == "pong"
    assert len(calls) == 2 and clock.sleeps == [1.0]


@pytest.mark.parametrize("stream", [False, True])
def test_http_date_retry_after_is_relative_to_the_clock(clock, stream):
    date = format_datetime(datetime.fromtimestamp(NOW + 7, tz=timezone.utc), usegmt=True)
    handler, calls = seq(r503(date), ok(stream))
    assert run(provider(handler), stream).message.content == "pong"
    assert clock.sleeps == [7.0]


@pytest.mark.parametrize("stream", [False, True])
def test_http_date_beyond_30_s_is_not_replayed(clock, stream):
    date = format_datetime(datetime.fromtimestamp(NOW + 120, tz=timezone.utc), usegmt=True)
    handler, calls = seq(r503(date), ok(stream))
    with pytest.raises(RuntimeError, match="503"):
        run(provider(handler), stream)
    assert len(calls) == 1


@pytest.mark.parametrize("stream", [False, True])
def test_code_goes_to_the_log_and_the_error_and_the_body_is_cut(clock, stream, caplog):
    body = json.dumps({"error": {"code": "no_upstream", "message": "z" * 600}})
    handler, calls = seq(r503("1", body))
    caplog.set_level(logging.WARNING, logger="nokker.providers.sokkan")
    with pytest.raises(RuntimeError) as e:
        run(provider(handler), stream)
    assert "no_upstream" in caplog.text and "req-42" in caplog.text
    msg = str(e.value)
    assert "no_upstream" in msg and msg.count("z") <= 200


@pytest.mark.parametrize("stream", [False, True])
def test_503_replay_that_times_out_gets_no_third_try(clock, stream):
    state = []

    def handler(request):
        state.append(1)
        if len(state) == 1:
            return r503("1")
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(RuntimeError):
        run(provider(handler, max_retries=5), stream)
    assert len(state) == 2


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status", [502, 504])
def test_502_and_504_unchanged(clock, stream, status):
    handler, calls = seq(httpx.Response(status, text="bad"))
    with pytest.raises(RuntimeError, match=str(status)):
        run(provider(handler), stream)
    assert len(calls) == 3  # max_retries=2 : 3 appels, comme avant
    handler, calls = seq(httpx.Response(status, text="bad"), ok(stream))
    assert run(provider(handler), stream).message.content == "pong" and len(calls) == 2


@pytest.mark.parametrize("stream", [False, True])
def test_429_busy_unchanged(clock, stream):
    handler, calls = seq(httpx.Response(429, text="busy"), httpx.Response(429, text="busy"), ok(stream))
    assert run(provider(handler), stream).message.content == "pong"
    assert len(calls) == 3 and clock.sleeps == [0.0, 0.0]


@pytest.mark.parametrize("stream", [False, True])
def test_a_client_timeout_gives_one_replay_in_total(clock, stream):
    """`timeout_retries=1` et le rejeu « normal » (max_retries) ne s'additionnent pas pour un délai d'attente.
    Avant : 2 rejeux « normal » + 1 « délai d'attente » = 4 appels quand les essais sont courts."""
    calls = []

    def handler(request):
        calls.append(request)
        clock.t += 30.0
        raise httpx.ReadTimeout("slow", request=request)

    p = provider(handler)
    p.timeout_s, p.call_budget_s = 30.0, 300.0
    with pytest.raises(RuntimeError, match="slow"):
        run(p, stream)
    assert len(calls) == 2
