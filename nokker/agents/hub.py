"""`AgentsHub` — Crew minimal (docs/BUBBLES.md §9.4) : CRUD d'agent + événement `agent.created`.

Pas d'exécution réelle (P2, hors de ce chantier) : un agent créé reste `armed`. Deux chemins y mènent :
`POST /v1/agents` (direct, `nokker/api/server.py`) et l'activation d'une bulle `agent_draft`
(`nokker/bubbles/hub.py::BubbleHub.action`, qui ferme la bulle en plus). `agent.created` est diffusé à
TOUTE identité vérifiée (pas de filtre par membre de fil — même principe que `approval.*`,
docs/CLIENT-API.md : Crew est une vue partagée de l'équipe, pas propre à un fil)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nokker.agents.base import BaseAgents
from nokker.agents.types import Agent, agent_to_json


@dataclass
class AgentsHub:
    store: BaseAgents
    publish: Callable[[str, dict], Any] | None = None  # EventBus.publish

    def list_agents(self) -> list[Agent]:
        return self.store.list_all()

    def create_agent(self, *, name: str, role: str, model: str, tools: list[str] | None = None,
                     created_by: str = "nina") -> Agent:
        a = self.store.create(name=name, role=role, model=model, tools=tools, created_by=created_by)
        if self.publish is not None:
            self.publish("agent.created", {"agent": agent_to_json(a)})
        return a


__all__ = ["AgentsHub"]
