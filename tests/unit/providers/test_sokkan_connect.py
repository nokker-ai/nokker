"""Erreurs de CONNEXION (e2e du 1.11, D5) : délai de connexion court et distinct du délai de lecture, nouvel essai réel
sur refus / DNS / TLS / poignée de main trop longue (avant tout octet de réponse), journal qui dit ce qui suit vraiment.

Avant : « _ssl.c:1064: The handshake operation timed out » après 180 s (le délai de connexion était celui de lecture),
aucun nouvel essai (budget de l'appel épuisé) et un journal « tentative 1/3 »."""
from __future__ import annotations

import json
import logging
import socket
import threading
import time

import httpx
import pytest

from nokker.providers import sokkan as sokkan_mod
from nokker.providers.sokkan import SokkanProvider

OK = {"model": "m", "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
      "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
HANDSHAKE = "_ssl.c:1064: The handshake operation timed out"


def _sse(*pieces: str) -> bytes:
    chunks = [{"choices": [{"index": 0, "delta": {"content": p}, "finish_reason": None}]} for p in pieces]
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


def provider(handler, **kw) -> SokkanProvider:
    p = SokkanProvider(base_url="https://amont.invalid/v1", api_key="k", model="m", retry_backoff_s=0.0, **kw)
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    return p


def failing_then_ok(n: int, exc, then):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) <= n:
            raise exc(HANDSHAKE, request=request)
        return then(request)

    return handler, calls


def test_timeouts_are_split_connect_short_read_long():
    p = SokkanProvider(base_url="https://amont.invalid/v1", api_key="k", model="m")
    t = p._client.timeout
    assert (t.connect, t.read, t.write, t.pool) == (10.0, 180.0, 180.0, 180.0)
    assert p.detached()._client.timeout.connect == 10.0


@pytest.mark.parametrize("exc", [httpx.ConnectTimeout, httpx.ConnectError])
def test_connect_error_is_really_retried(exc):
    handler, calls = failing_then_ok(1, exc, lambda r: httpx.Response(200, json=OK))
    comp = provider(handler).complete([{"role": "user", "content": "x"}], [], reasoning=False, session_key="t")
    assert comp.message.content == "pong" and comp.attempts == 2 and len(calls) == 2
    for c in calls:  # chaque essai : connexion courte, lecture longue
        assert c.extensions["timeout"]["connect"] == 10.0 and c.extensions["timeout"]["read"] > 100


def test_connect_retries_do_not_need_the_response_budget(monkeypatch):
    """`total_budget_s` borne l'attente d'une RÉPONSE : des échecs de connexion (10 s chacun) ne l'épuisent pas."""
    clock = [1000.0]
    monkeypatch.setattr(sokkan_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sokkan_mod.time, "sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) <= 2:
            clock[0] += request.extensions["timeout"]["connect"]
            raise httpx.ConnectTimeout(HANDSHAKE, request=request)
        return httpx.Response(200, json=OK)

    p = provider(handler, total_budget_s=5.0)
    comp = p.complete([], [], reasoning=False, session_key="t")
    assert comp.attempts == 3 and len(calls) == 3


def test_connect_errors_stop_after_max_retries_and_say_so(caplog):
    handler, calls = failing_then_ok(10, httpx.ConnectTimeout, lambda r: httpx.Response(200, json=OK))
    with caplog.at_level(logging.WARNING, logger="nokker.providers.sokkan"), \
            pytest.raises(RuntimeError, match="3 tentative"):
        provider(handler).complete([], [], reasoning=False, session_key="t")
    assert len(calls) == 3
    lines = [r.getMessage() for r in caplog.records]
    assert "nouvel essai 2" in lines[0] and "nouvel essai 3" in lines[1]
    assert "abandon (3 échecs de connexion)" in lines[2]
    assert not any("1/3" in ln for ln in lines)  # plus d'annonce d'essais qui ne partent pas


def test_log_says_abandon_when_no_retry_will_follow(caplog, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sokkan_mod.time, "monotonic", lambda: clock[0])

    def handler(request):
        clock[0] += 175.0  # le budget de l'appel (180 s) n'a plus de place pour un essai
        raise httpx.ConnectError(HANDSHAKE, request=request)

    with caplog.at_level(logging.WARNING, logger="nokker.providers.sokkan"), pytest.raises(RuntimeError):
        provider(handler).complete([], [], reasoning=False, session_key="t")
    assert [r.getMessage() for r in caplog.records][-1].endswith("abandon (budget de l'appel épuisé)")


def test_stream_connect_error_is_retried_before_any_piece():
    handler, calls = failing_then_ok(2, httpx.ConnectTimeout, lambda r: httpx.Response(200, content=_sse("po", "ng")))
    seen: list[str] = []
    comp = provider(handler).complete([], [], reasoning=False, session_key="t", on_delta=seen.append)
    assert comp.message.content == "pong" and "".join(seen) == "pong" and len(calls) == 3


def test_read_errors_keep_their_rules_after_connect_retries():
    """Les réessais de connexion ne mangent pas ceux des autres erreurs (5xx ici)."""
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError(HANDSHAKE, request=request)
        if len(calls) <= 3:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json=OK)

    comp = provider(handler).complete([], [], reasoning=False, session_key="t")
    assert comp.message.content == "pong" and len(calls) == 4


class _SilentTLSServer:
    """Faux amont réel : accepte la connexion TCP et ne répond jamais à la poignée de main TLS."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self.conns: list[socket.socket] = []
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while not self._stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            self.conns.append(c)

    def close(self) -> None:
        self._stop = True
        for c in self.conns:
            c.close()
        self.sock.close()


def test_real_tls_handshake_timeout_is_short_and_retried(monkeypatch):
    monkeypatch.setattr(sokkan_mod, "MIN_RETRY_S", 0.1)
    srv = _SilentTLSServer()
    try:
        p = SokkanProvider(base_url=f"https://127.0.0.1:{srv.port}/v1", api_key="k", model="m",
                           retry_backoff_s=0.0, connect_timeout_s=0.3, timeout_s=30.0)
        t0 = time.monotonic()
        with pytest.raises(RuntimeError, match="(?i)handshake|timed out"):
            p.complete([], [], reasoning=False, session_key="t")
        took = time.monotonic() - t0
    finally:
        srv.close()
    assert srv.accepted == 3  # trois vraies connexions : l'essai initial et deux nouveaux essais
    assert took < 5.0  # 3 × 0,3 s de poignée de main, pas le délai de lecture (30 s)
