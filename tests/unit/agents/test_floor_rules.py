"""Planchers ops (X11) dans un passage d'agent (lot X16) : une règle `auto_approve` lève une carte ordinaire, jamais
la carte prod ni la confiance d'une clé d'hôte."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nokker.agents import engine as eng
from nokker.agents.engine import AgentRunGates
from nokker.core.ports import ToolSpec
from nokker.core.turn import Decision, Effect, Gate, ToolCall, Verdict
from nokker.qa.permissions import Rule
from nokker.tools.ssh import PROD_RULE, TRUST_RULE


class Inner:
    def __init__(self, rule):
        self.rule = rule

    def action(self, call, spec, turn):
        return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=self.rule, subject=call.id,
                        strength="strong")]


SPEC = ToolSpec(name="ssh_run", description="", parameters={}, effect=Effect.DESTRUCTIVE)
CALL = ToolCall(id="c1", name="ssh_run", arguments={"host": "web", "command": "systemctl restart app"})


def test_floor_rules_are_the_ssh_ones_and_the_local_mailbox_send():
    from nokker.qa.permissions import LOCAL_STRONG_RULE

    from nokker.household.model import GUARDIAN_RULE

    assert eng._FLOOR_RULES == {PROD_RULE, TRUST_RULE, LOCAL_STRONG_RULE, GUARDIAN_RULE}


@pytest.mark.parametrize("rule", [PROD_RULE, TRUST_RULE, "permissions:local_connector_strong",
                                  "household:guardian"])
def test_agent_rule_never_lifts_a_floor_card(rule):
    gates = AgentRunGates(inner=Inner(rule), rules=[Rule.parse("ssh_run"), Rule.parse("ssh_trust")])
    out = gates.action(CALL, SPEC, SimpleNamespace(thread_id="th_x"))
    assert [v.decision for v in out] == [Decision.HITL] and out[0].rule == rule


def test_agent_rule_still_lifts_an_ordinary_card():
    gates = AgentRunGates(inner=Inner("effect:destructive"), rules=[Rule.parse("ssh_run")])
    out = gates.action(CALL, SPEC, SimpleNamespace(thread_id="th_x"))
    assert out[-1].decision is Decision.ALLOW and out[-1].rule == "agent:auto_approve"
