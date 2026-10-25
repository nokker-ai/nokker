"""`agent_draft` suit l'instance : sans moteur Crew (la bêta), seulement le déclencheur `manual`, et le modèle le sait.

Revue du train du 2.11, §0 décision 4 : l'outil invitait toujours le modèle à proposer `schedule`/`once`/`event`, dont
l'activation échoue maintenant en 400 (`NO_ENGINE_SCHEDULED`, fix-batch-2). Sans moteur, le schéma n'offre plus que
`manual`, la description le dit, et une carte programmée est refusée tout de suite avec une consigne lisible (pas de
bulle « Activer » vouée au 400)."""
from __future__ import annotations

import json

import pytest

from nokker.agents.hub import AgentsHub
from nokker.agents.memory import InMemoryAgents
from nokker.bubbles.tools import BubbleToolSource
from nokker.core.ports import ToolContext
from nokker.core.turn import Effect

PROGRAMMED = [{"kind": "schedule", "cron": "0 7 * * 1-5", "tz": "Europe/Zurich"},
              {"kind": "once", "at": "2099-01-01T07:00:00Z"}, {"kind": "event", "event": "alert:disque"}]


class _Engine:
    local_runs = True

    def drop_card(self, approval_id, why):
        pass


def _spec(src: BubbleToolSource):
    return next(s for s in src.specs() if s.name == "agent_draft")


def _agents(engine=None) -> AgentsHub:
    return AgentsHub(store=InMemoryAgents(), publish=lambda *a: None, rule_effect={"web_search": Effect.READ}.get,
                     engine=engine)


@pytest.mark.parametrize("with_agents_hub", [True, False])
def test_without_engine_the_tool_offers_manual_only_and_says_so(hub, with_agents_hub):
    hub.agents_hub = _agents() if with_agents_hub else None
    spec = _spec(BubbleToolSource(hub=hub))
    trigger = spec.parameters["properties"]["trigger"]
    assert trigger["properties"]["kind"]["enum"] == ["manual"]
    text = (spec.description + json.dumps(spec.parameters, ensure_ascii=False)).lower()
    for word in ("schedule", "cron", "once", "event", "webhook", "alert:", "auto_approve"):
        assert word not in text, word
    assert "pas de moteur" in spec.description and "manuel" in spec.description
    assert "auto_approve" not in spec.parameters["properties"]  # sans moteur, une règle levée laisse l'agent au repos


@pytest.mark.parametrize("trigger", PROGRAMMED)
def test_without_engine_a_programmed_card_is_refused_with_a_clear_instruction(hub, trigger):
    hub.agents_hub = _agents()
    t = hub.create_thread("Veille", "alice")
    out = BubbleToolSource(hub=hub).call("agent_draft", {"name": "Veille", "role": "scout", "trigger": trigger},
                                         ToolContext(t.id, 0, "api", "alice"))
    assert out.is_error and "pas de moteur" in out.content and '{"kind": "manual"}' in out.content
    assert hub.bubbles.list_thread(t.id) == []  # pas de carte vouée au 400 à l'activation


def test_without_engine_a_manual_card_is_proposed_and_activates(hub):
    hub.agents_hub = _agents()
    t = hub.create_thread("Veille", "alice")
    src = BubbleToolSource(hub=hub)
    out = src.call("agent_draft", {"name": "Veille", "role": "scout", "trigger": {"kind": "manual"}},
                   ToolContext(t.id, 0, "api", "alice"))
    assert not out.is_error
    [b] = hub.bubbles.list_thread(t.id)
    assert hub.action(b.id, "alice", "activate")["activated"] is True


def test_with_an_engine_the_tool_offers_every_trigger(hub):
    hub.agents_hub = _agents(_Engine())
    src = BubbleToolSource(hub=hub)
    spec = _spec(src)
    assert spec.parameters["properties"]["trigger"]["properties"]["kind"]["enum"] == ["manual", "schedule", "once",
                                                                                        "event"]
    assert "auto_approve" in spec.parameters["properties"] and "pas de moteur" not in spec.description
    t = hub.create_thread("Veille", "alice")
    out = src.call("agent_draft", {"name": "Veille", "role": "scout", "trigger": PROGRAMMED[0]},
                   ToolContext(t.id, 0, "api", "alice"))
    assert not out.is_error and len(hub.bubbles.list_thread(t.id)) == 1


def test_the_spec_follows_the_engine_once_it_is_attached(hub):
    """Le moteur est posé par `serve` APRÈS la construction du noyau : la spec se relit (le registre est rafraîchi)."""
    hub.agents_hub = _agents()
    src = BubbleToolSource(hub=hub)
    assert _spec(src).parameters["properties"]["trigger"]["properties"]["kind"]["enum"] == ["manual"]
    hub.agents_hub.engine = _Engine()
    assert "schedule" in _spec(src).parameters["properties"]["trigger"]["properties"]["kind"]["enum"]


def test_mail_trigger_is_not_offered_and_a_mail_card_is_refused(hub):
    """`mail:<filtre>` n'a pas de producteur (revue de cohérence de la doc, 25.10) : le schéma de l'outil ne le propose
    plus, et une carte qui le porte est refusée tout de suite (pas de bulle « Activer » pour un agent qui ne tournerait
    jamais)."""
    hub.agents_hub = _agents(_Engine())
    src = BubbleToolSource(hub=hub)
    spec = _spec(src)
    text = spec.description + json.dumps(spec.parameters, ensure_ascii=False)
    assert "mail:" not in text and "webhook:" in text and "alert:" in text
    t = hub.create_thread("Tri", "alice")
    out = src.call("agent_draft", {"name": "Tri", "role": "courrier",
                                   "trigger": {"kind": "event", "event": "mail:is:unread"}},
                   ToolContext(t.id, 0, "api", "alice"))
    assert out.is_error and "mail:" in out.content and "pas disponible" in out.content
    assert hub.bubbles.list_thread(t.id) == []


class _Watch:
    def __init__(self, why=None):
        self.why = why

    def unavailable(self):
        return self.why


def test_with_the_mail_watch_the_tool_offers_mail_and_checks_the_filter(hub):
    """Relève du courrier branchée (moteur + connecteurs) : `mail:<filtre>` revient dans le schéma, le filtre est
    validé dès la proposition, et une boîte non reliée est dite au modèle tout de suite."""
    engine = _Engine()
    engine.mail_watch = _Watch()
    hub.agents_hub = _agents(engine)
    src = BubbleToolSource(hub=hub)
    spec = _spec(src)
    assert "mail:<filtre>" in json.dumps(spec.parameters, ensure_ascii=False)
    t = hub.create_thread("Tri", "alice")
    ctx = ToolContext(t.id, 0, "api", "alice")
    out = src.call("agent_draft", {"name": "Tri", "role": "courrier",
                                   "trigger": {"kind": "event", "event": "mail:from:billing@example.com  is:unread"}},
                   ctx)
    assert not out.is_error, out.content
    b = hub.bubbles.list_thread(t.id)[0]
    assert b.state["trigger"] == {"kind": "event", "event": "mail:from:billing@example.com is:unread"}
    out = src.call("agent_draft", {"name": "Tri", "role": "courrier",
                                   "trigger": {"kind": "event", "event": "mail:after:2026/01/01"}}, ctx)
    assert out.is_error and "after:" in out.content
    engine.mail_watch = _Watch("trigger.event mail:<filtre> : aucune boîte mail reliée à ce compte")
    out = src.call("agent_draft", {"name": "Tri", "role": "courrier",
                                   "trigger": {"kind": "event", "event": "mail:*"}}, ctx)
    assert out.is_error and "aucune boîte" in out.content
    assert len(hub.bubbles.list_thread(t.id)) == 1
