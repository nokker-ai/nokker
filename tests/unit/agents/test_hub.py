"""`nokker/agents/hub.py::AgentsHub` — CRUD + événement `agent.created` (docs/BUBBLES.md §9.4)."""
from __future__ import annotations

from nokker.agents.hub import AgentsHub
from nokker.agents.memory import InMemoryAgents
from nokker.agents.types import agent_to_json


class Recorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event_type, data):
        self.events.append((event_type, data))


def test_create_agent_persists_and_publishes_agent_created():
    rec = Recorder()
    hub = AgentsHub(store=InMemoryAgents(), publish=rec)
    a = hub.create_agent(name="Veille", role="scout", model="ship", tools=["web_search"], created_by="alice")
    assert hub.list_agents() == [a]
    assert rec.events == [("agent.created", {"agent": agent_to_json(a)})]
    assert a.status == "armed"  # pas d'exécution réelle (P2)


def test_create_agent_without_publisher_does_not_raise():
    hub = AgentsHub(store=InMemoryAgents(), publish=None)
    a = hub.create_agent(name="x", role="y", model="z", tools=[], created_by="nina")
    assert hub.list_agents() == [a]
