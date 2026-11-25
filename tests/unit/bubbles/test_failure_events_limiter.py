"""Événements d'échec d'un palier interne : au plus un par minute, par module et par raison ; `repeats` dans le suivant."""
from __future__ import annotations

from nokker.core.internal_tiers import FailureLimiter
from tests.unit.bubbles.conftest import *  # noqa: F401,F403  (fixtures)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _hub(hub, clock):
    hub.failure_limiter = FailureLimiter(monotonic=clock)
    return hub


def _events(rec, name):
    return [e[1] for e in rec.events if e[0] == name]


def test_five_identical_failures_in_ten_seconds_give_one_event_then_repeats(hub, rec):
    clk = Clock()
    _hub(hub, clk)
    for _ in range(5):
        hub.host_event("judge.failed", {"reason": "timeout"})
        clk.t += 2
    assert _events(rec, "judge.failed") == [{"reason": "timeout"}]
    clk.t += 61
    hub.host_event("judge.failed", {"reason": "timeout"})
    assert _events(rec, "judge.failed")[1] == {"reason": "timeout", "repeats": 4}
    clk.t += 61
    hub.host_event("judge.failed", {"reason": "timeout"})
    assert "repeats" not in _events(rec, "judge.failed")[2]  # rien de supprimé entre-temps


def test_different_reasons_and_modules_are_separate(hub, rec):
    clk = Clock()
    _hub(hub, clk)
    hub.host_event("judge.failed", {"reason": "timeout"})
    hub.host_event("judge.failed", {"reason": "error"})
    hub.host_event("gate.failed", {"reason": "timeout"})
    hub.host_event("home.judge_failed", {"reason": "timeout"}, thread_id=None)
    assert len(_events(rec, "judge.failed")) == 2 and len(_events(rec, "gate.failed")) == 1
    assert len(_events(rec, "home.judge_failed")) == 1


def test_other_events_are_not_limited(hub, rec):
    _hub(hub, Clock())
    for _ in range(3):
        hub.host_event("something.else", {"reason": "x"})
    assert len(_events(rec, "something.else")) == 3


def test_memory_is_bounded():
    lim = FailureLimiter(monotonic=Clock(), max_keys=3)
    for i in range(10):
        lim.allow("m", f"r{i}")
    assert len(lim._seen) == 3
