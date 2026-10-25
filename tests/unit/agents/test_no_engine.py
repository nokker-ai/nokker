"""Agents sans moteur (la bêta : `agents_enabled` vrai, pas de moteur Crew). Revue du train du 2.11, défaut 6 :
un agent `schedule`/`once`/`event` s'armait puis ne tournait jamais, et armer une règle d'écriture répondait 503
« approbations non configurées » (libellé faux : c'est le moteur qui manque).

Arbitrage dev : sans moteur, créer ou modifier un agent dont le déclencheur n'est pas `manual` → 400 « les agents
programmés ne sont pas disponibles sur cette instance (pas de moteur) » ; armer un agent manuel avec écriture → 503 au
libellé juste ; un agent manuel se crée comme avant (`armed` sans règle d'écriture : l'onglet Crew de nokker-apps
`main` ne connaît pas `paused`)."""
from __future__ import annotations

import pytest

from nokker.agents.hub import NO_ENGINE_SCHEDULED, AgentsHub, EngineUnavailable
from nokker.agents.memory import InMemoryAgents
from nokker.agents.types import AgentError
from nokker.core.turn import Effect
from tests.unit.api.test_bubbles_routes import call, start

PROGRAMMED = [{"kind": "schedule", "cron": "0 7 * * *"}, {"kind": "once", "at": "2099-01-01T07:00:00Z"},
              {"kind": "event", "event": "alert:disque"}]


def _hub():
    return AgentsHub(store=InMemoryAgents(), rule_effect={"web_search": Effect.READ}.get)


def test_manual_agent_is_created_armed_as_before():
    hub = _hub()
    a = hub.create_from_body({"name": "Veille", "role": "scout", "model": "ship", "tools": ["web_search"]}, "alice")
    assert a.status == "armed" and a.trigger == {"kind": "manual"}
    assert hub.create_from_body({"name": "B", "role": "r", "auto_approve": ["web_search"]}, "alice").status == "armed"


@pytest.mark.parametrize("trigger", PROGRAMMED)
def test_programmed_agent_is_refused_without_an_engine(trigger):
    hub = _hub()
    with pytest.raises(AgentError) as e:
        hub.create_from_body({"name": "A", "role": "r", "trigger": trigger}, "alice")
    assert not isinstance(e.value, EngineUnavailable) and str(e.value) == NO_ENGINE_SCHEDULED
    assert hub.list_agents() == []


@pytest.mark.parametrize("trigger", PROGRAMMED)
def test_patch_to_a_programmed_trigger_is_refused_without_an_engine(trigger):
    hub = _hub()
    a = hub.create_from_body({"name": "A", "role": "r"}, "alice")
    with pytest.raises(AgentError, match="pas de moteur"):
        hub.patch_agent(a.id, {"trigger": trigger}, "alice")
    assert hub.get_agent(a.id).trigger == {"kind": "manual"} and hub.get_agent(a.id).status == "armed"


def test_arming_a_manual_write_agent_says_the_engine_is_missing():
    hub = _hub()
    a = hub.create_from_body({"name": "A", "role": "r", "auto_approve": ["note_write"]}, "alice")
    assert a.status == "paused"
    with pytest.raises(EngineUnavailable) as e:
        hub.arm(a.id, "alice")
    assert "moteur" in str(e.value) and "approbations" not in str(e.value)


def test_routes_400_for_programmed_201_armed_for_manual_503_for_a_write_arm():
    srv, hub, _ = start()
    try:
        st, body = call(srv, "POST", "/v1/agents", {"name": "Matin", "role": "veille",
                                                    "trigger": {"kind": "schedule", "cron": "0 7 * * *"}})
        assert st == 400 and body["error"]["message"] == NO_ENGINE_SCHEDULED
        st, a = call(srv, "POST", "/v1/agents", {"name": "Veille", "role": "scout", "model": "ship", "tools": []})
        assert st == 201 and a["status"] == "armed"
        st, body = call(srv, "PATCH", f"/v1/agents/{a['id']}", {"trigger": {"kind": "once",
                                                                            "at": "2099-01-01T07:00:00Z"}})
        assert st == 400 and "pas de moteur" in body["error"]["message"]
        st, w = call(srv, "POST", "/v1/agents", {"name": "Écrit", "role": "r", "auto_approve": ["note_write"]})
        assert st == 201 and w["status"] == "paused"
        st, body = call(srv, "POST", f"/v1/agents/{w['id']}/arm")
        assert st == 503 and "moteur" in body["error"]["message"] and "approbations" not in body["error"]["message"]
    finally:
        srv.stop()
