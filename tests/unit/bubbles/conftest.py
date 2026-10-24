from __future__ import annotations

import pytest

from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.threads.memory import InMemoryThreads


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Recorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event_type, data):
        self.events.append((event_type, data))

    def types(self):
        return [t for t, _ in self.events]

    def of(self, event_type):
        return [d for t, d in self.events if t == event_type]


def scrub(text: str) -> str:
    return text.replace("S3CR3T-VALUE", "[secret:TEST]")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def rec():
    return Recorder()


@pytest.fixture
def hub(clock, rec):
    return BubbleHub(threads=InMemoryThreads(clock=clock), bubbles=InMemoryBubbles(clock=clock), publish=rec,
                     scrub=scrub, known_users=frozenset({"alice", "bruno"}), clock=clock,
                     spawn=lambda fn: fn())
