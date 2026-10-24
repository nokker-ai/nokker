"""`nokker/agents/base.py` + `memory.py` — Crew minimal (docs/BUBBLES.md §9.4)."""
from __future__ import annotations

import pytest

from nokker.agents.memory import InMemoryAgents
from nokker.agents.types import AgentError, AgentNotFound


def test_create_sets_id_prefix_default_status_and_trims_fields():
    store = InMemoryAgents()
    a = store.create(name="  Veille  ", role="scout", model="ship", tools=["web_search"])
    assert a.id.startswith("ag_") and len(a.id) == 15
    assert a.name == "Veille" and a.status == "armed"
    assert a.tools == ["web_search"]


def test_create_rejects_unknown_status():
    store = InMemoryAgents()
    with pytest.raises(AgentError):
        store.create(name="x", role="y", model="z", status="bogus")


def test_require_raises_not_found():
    store = InMemoryAgents()
    with pytest.raises(AgentNotFound):
        store.require("ag_inconnu0001")


def test_list_all_sorted_by_created_at():
    store = InMemoryAgents(clock=lambda: 1.0)
    a = store.create(name="A", role="r", model="m")
    store.clock = lambda: 2.0
    b = store.create(name="B", role="r", model="m")
    assert [x.id for x in store.list_all()] == [a.id, b.id]


def test_tools_list_capped_at_40():
    store = InMemoryAgents()
    a = store.create(name="x", role="y", model="z", tools=[f"t{i}" for i in range(50)])
    assert len(a.tools) == 40
