"""`nokker/agents/spec.py` et le cycle de la carte (`AgentsHub`, sans moteur) — docs/BUBBLES.md §9.5."""
from __future__ import annotations

import pytest

from nokker.agents.hub import AgentsHub, EngineUnavailable
from nokker.agents.memory import InMemoryAgents
from nokker.agents.spec import norm_budget, norm_fields, norm_placement, norm_rules, norm_secrets, norm_trigger
from nokker.agents.types import AgentError, agent_to_json
from nokker.core.turn import Effect
from tests.unit.agents.conftest import Recorder


def test_triggers_are_normalized():
    assert norm_trigger(None) == {"kind": "manual"}
    assert norm_trigger({"kind": "schedule", "cron": "0  7 * * 1-5", "tz": "Europe/Zurich"}) == \
        {"kind": "schedule", "cron": "0 7 * * 1-5", "tz": "Europe/Zurich"}
    assert norm_trigger({"kind": "schedule", "cron": "0 7 * * *"})["tz"] == "UTC"
    assert norm_trigger({"kind": "once", "at": "2026-11-02T08:00:00+01:00"}) == \
        {"kind": "once", "at": "2026-11-02T07:00:00Z"}
    assert norm_trigger({"kind": "event", "event": "webhook:github-pr"})["event"] == "webhook:github-pr"
    assert norm_trigger({"kind": "event", "event": "alert:disk-*"})["event"] == "alert:disk-*"


@pytest.mark.parametrize("raw", [
    {"kind": "cron"}, {"kind": "schedule", "cron": "bad"}, {"kind": "schedule", "cron": "0 0 30 2 *"},
    {"kind": "schedule", "cron": "0 7 * * *", "tz": "Nowhere/Land"}, {"kind": "once", "at": "demain"},
    {"kind": "once", "at": "2026-11-02T07:00:00"}, {"kind": "event", "event": "sms:x"},
    {"kind": "event", "event": "webhook:a b"}, "manual", {"kind": "event", "event": "mail:is:unread"},
])
def test_invalid_triggers(raw):
    with pytest.raises(AgentError):
        norm_trigger(raw)


def test_mail_trigger_is_refused_with_a_clear_message():
    """`mail:<filtre>` n'a pas de producteur (aucune relève du connecteur mail) : il ne tournerait jamais."""
    with pytest.raises(AgentError) as e:
        norm_trigger({"kind": "event", "event": "mail:from:billing@example.com"})
    msg = str(e.value)
    assert "mail:" in msg and "pas disponible" in msg and "webhook:" in msg and "schedule" in msg


def test_secrets_are_names_never_values():
    assert norm_secrets(["API_TOKEN", "API_TOKEN", "github.token"]) == ["API_TOKEN", "github.token"]
    for bad in (["sk-abc123def456"], ["ghp_A1b2C3d4E5f6G7h8I9j0"], ["Ab3dE5gH7jK9mN1pQ3sT5vX7zB9"], ["a b"], "X"):
        with pytest.raises(AgentError):
            norm_secrets(bad)


def test_rules_budget_placement():
    assert norm_rules(["terminal(git status*)", " web_search "]) == ["terminal(git status*)", "web_search"]
    with pytest.raises(AgentError):
        norm_rules(["(rien)"])
    assert norm_budget({"max_calls": 100, "max_cost": 2}) == {"max_calls": 100, "max_cost": 2.0, "currency": "EUR"}
    for bad in ({}, {"max_calls": 0}, {"max_cost": -1}, {"max_cost": 1, "currency": "euro"}):
        with pytest.raises(AgentError):
            norm_budget(bad)
    assert norm_placement(None, None) is None and norm_placement({"machine": "mc_moi"}, "mc_moi") == {"machine": "mc_moi"}
    # Lokkal (sans politique de nokker.ai) : seule cette machine ; le reste se choisit dans le deck (X15)
    with pytest.raises(AgentError, match="deck de nokker.ai"):
        norm_placement({"machine": "auto"}, "mc_moi")
    with pytest.raises(AgentError, match="deck de nokker.ai"):
        norm_placement({"machine": "mc_" + "a" * 16}, "mc_moi")
    with pytest.raises(AgentError, match="mc_…"):
        norm_placement({"machine": "ailleurs"}, "mc_moi")  # forme inconnue


def test_fields_partial_and_full():
    with pytest.raises(AgentError):
        norm_fields({"role": "x"}, partial=False)
    assert norm_fields({"project": "pj_1"}, partial=True) == {"project": "pj_1"}
    full = norm_fields({"name": "A", "role": "r"}, partial=False)
    assert full["trigger"] == {"kind": "manual"} and full["project"] is None and full["placement"] is None


class _Engine:
    """Moteur présent mais inerte : sans moteur, un déclencheur programmé est refusé (test_no_engine.py)."""

    def drop_card(self, approval_id, why):
        pass


def _hub(effects=None, engine=None):
    rec = Recorder()
    effects = effects or {"web_search": Effect.READ}
    return AgentsHub(store=InMemoryAgents(), publish=rec, rule_effect=effects.get, engine=engine), rec


def test_create_statuses_follow_the_contract():
    hub, rec = _hub(engine=_Engine())
    assert hub.create_from_body({"name": "A", "role": "r"}, "alice").status == "armed"  # manuel
    assert hub.create_from_body({"name": "B", "role": "r", "auto_approve": ["web_search"]}, "alice").status == "armed"
    assert hub.create_from_body({"name": "C", "role": "r", "auto_approve": ["note_write"]}, "alice").status == "paused"
    s = hub.create_from_body({"name": "D", "role": "r", "trigger": {"kind": "schedule", "cron": "0 7 * * *"}}, "a")
    assert s.status == "paused" and s.next_run_at is None
    assert [d["agent"]["name"] for d in rec.of("agent.created")] == ["A", "B", "C", "D"]
    body = agent_to_json(s)
    for key in ("instructions", "trigger", "next_run_at", "last_run", "secrets", "budget", "auto_approve", "placement",
                "project"):
        assert key in body
    assert "webhook_key" not in body and "armed" not in body and "approved_rules" not in body


def test_event_agent_refuses_a_write_rule():
    hub, _ = _hub(engine=_Engine())
    with pytest.raises(AgentError, match="ne lève jamais d'écriture"):
        hub.create_from_body({"name": "A", "role": "r", "trigger": {"kind": "event", "event": "alert:x"},
                              "auto_approve": ["note_write"]}, "alice")
    a = hub.create_from_body({"name": "A", "role": "r", "trigger": {"kind": "event", "event": "alert:x"},
                              "auto_approve": ["web_search"]}, "alice")
    with pytest.raises(AgentError):
        hub.patch_agent(a.id, {"auto_approve": ["terminal(rm *)"]}, "alice")
    b = hub.create_from_body({"name": "B", "role": "r", "auto_approve": ["note_write"]}, "alice")
    with pytest.raises(AgentError):
        hub.patch_agent(b.id, {"trigger": {"kind": "event", "event": "webhook:h"}}, "alice")


def test_patch_disarms_on_trigger_tools_rules_or_secrets_but_not_on_name():
    hub, rec = _hub()
    a = hub.create_from_body({"name": "A", "role": "r"}, "alice")
    a = hub.patch_agent(a.id, {"name": "A2", "instructions": "autre mission"}, "alice")
    assert a.status == "armed" and a.name == "A2"
    for patch in ({"tools": ["web_search"]}, {"secrets": ["TOKEN"]}, {"auto_approve": ["web_search"]},
                  {"trigger": {"kind": "manual"}}):
        a, _ = hub.arm(a.id, "alice")
        assert a.status == "armed"
        a = hub.patch_agent(a.id, patch, "alice")
        expected = "armed" if patch == {"trigger": {"kind": "manual"}} else "paused"  # même valeur : rien ne change
        assert a.status == expected, patch
    assert rec.of("agent.updated")[-1]["agent"]["status"] == "armed"


def test_write_rule_cannot_arm_without_an_engine():
    hub, _ = _hub()
    a = hub.create_from_body({"name": "A", "role": "r", "auto_approve": ["note_write"]}, "alice")
    with pytest.raises(EngineUnavailable):
        hub.arm(a.id, "alice")
    with pytest.raises(EngineUnavailable):
        hub.run_now(a.id, "alice")


def test_disarm_and_delete_publish():
    hub, rec = _hub()
    a = hub.create_from_body({"name": "A", "role": "r"}, "alice")
    assert hub.disarm(a.id, "alice").status == "paused"
    hub.delete_agent(a.id, "alice")
    assert rec.of("agent.deleted") == [{"id": a.id}] and hub.list_agents() == []
