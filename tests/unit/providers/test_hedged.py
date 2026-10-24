import threading
import time

import pytest

from nokker.core.ports import StreamInterrupted
from nokker.providers.hedged import HedgedProvider


class Fake:
    def __init__(self, model, delay=0.0, text="ok", error=None, pieces=()):
        self.model, self.delay, self.text, self.error, self.pieces = model, delay, text, error, pieces
        self.calls = 0

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048, on_delta=None):
        self.calls += 1
        for p in self.pieces:
            if on_delta:
                on_delta(p)
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return f"{self.model}:{self.text}"


def call(h, on_delta=None):
    return h.complete([], [], reasoning=False, session_key="s", on_delta=on_delta)


def test_fast_primary_wins():
    p, f = Fake("p"), Fake("f")
    assert call(HedgedProvider(p, f, 0.5)) == "p:ok"
    assert f.calls == 0


def test_slow_primary_falls_back():
    p, f = Fake("p", delay=1.0), Fake("f")
    h = HedgedProvider(p, f, 0.1)
    t = time.monotonic()
    assert call(h) == "f:ok"
    assert time.monotonic() - t < 0.8 and h.hedged_calls == 1


def test_primary_error_before_output_falls_back():
    assert call(HedgedProvider(Fake("p", error=RuntimeError("x")), Fake("f"), 0.5)) == "f:ok"


def test_stream_first_piece_commits_to_primary():
    got = []
    p = Fake("p", delay=0.5, pieces=["a"])
    assert call(HedgedProvider(p, Fake("f"), 0.1), got.append) == "p:ok" and got == ["a"]


def test_error_after_piece_is_not_replayed():
    got = []
    p = Fake("p", pieces=["a"], error=StreamInterrupted("cut"))
    f = Fake("f")
    with pytest.raises(StreamInterrupted):
        call(HedgedProvider(p, f, 0.5), got.append)
    assert f.calls == 0


def test_abandoned_primary_deltas_are_dropped():
    got = []

    class Late(Fake):
        def complete(self, *a, on_delta=None, **k):
            time.sleep(0.4)
            on_delta("late")
            return "p:late"

    h = HedgedProvider(Late("p"), Fake("f", pieces=["fast"]), 0.1)
    assert call(h, got.append) == "f:ok"
    time.sleep(0.5)
    assert got == ["fast"]
    assert threading.active_count() >= 1


def test_abandoned_primary_is_closed_and_shared_one_never():
    """20.10.2026 : le primaire abandonné est fermé (plus de réessais qui tiennent une place de la clé) ; le
    provider partagé, lui, n'est jamais fermé par un appel."""
    class Detachable(Fake):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.twins = []
            self.closed = False

        def detached(self):
            twin = Detachable(self.model, delay=self.delay)
            self.twins.append(twin)
            return twin

        def close(self):
            self.closed = True

    p, f = Detachable("p", delay=0.5), Fake("f")
    assert call(HedgedProvider(p, f, 0.05)) == "f:ok"
    assert p.twins and p.twins[0].closed and not p.closed
