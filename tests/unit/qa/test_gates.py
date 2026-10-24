"""`GatesV0` — couche 1 (Invariants, factice ici — la vraie vient de F3) + couche 2 (NullJudge)."""
from __future__ import annotations

from dataclasses import dataclass

from nokker.core.turn import Decision, Gate, InputCheck, Tier, ToolCall, Turn
from nokker.qa.gates import GatesV0, NullJudge


@dataclass
class FakeInvariants:
    """Implémentation factice (PAS F3) : juste assez pour exercer `GatesV0.action`."""

    verdict: object = None

    def check_action(self, call, spec, turn):
        return self.verdict


def turn_with_flags(flags=frozenset(), escalated=False):
    return Turn(thread_id="t", index=0, input=InputCheck(text="x", domain_flags=set(flags)),
               escalated=escalated)


def test_action_delegates_to_invariants():
    from nokker.core.turn import Verdict
    v = Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="r")
    gates = GatesV0(invariants=FakeInvariants(verdict=v))
    out = gates.action(ToolCall(id="c", name="x", arguments={}), None, turn_with_flags())
    assert out == [v]


def test_switch_escalates_on_domain_flag_not_yet_escalated():
    gates = GatesV0(invariants=FakeInvariants())
    verdicts = gates.switch("réponse", turn_with_flags(flags={"fiscal"}), Tier.SHIP)
    assert any(v.decision is Decision.ESCALATE for v in verdicts)
    assert any("fiscal" in v.rule for v in verdicts)


def test_switch_does_not_re_escalate_if_already_escalated():
    gates = GatesV0(invariants=FakeInvariants())
    verdicts = gates.switch("réponse", turn_with_flags(flags={"fiscal"}, escalated=True), Tier.SHIP)
    assert all(v.decision is not Decision.ESCALATE for v in verdicts)


def test_switch_allows_when_no_domain_flag():
    gates = GatesV0(invariants=FakeInvariants())
    verdicts = gates.switch("réponse", turn_with_flags(), Tier.SHIP)
    assert verdicts == [v for v in verdicts if v.decision is Decision.ALLOW]
    assert verdicts[0].layer == 2 and verdicts[0].judge_model == "null-judge-v0"


def test_switch_only_escalates_configured_domains():
    gates = GatesV0(invariants=FakeInvariants())
    verdicts = gates.switch("réponse", turn_with_flags(flags={"infra"}), Tier.SHIP)
    assert all(v.decision is Decision.ALLOW for v in verdicts)


def test_null_judge_always_allows():
    answer = NullJudge().ask("q1", "la réponse", ["ALLOW", "DENY"])
    assert answer.choice == "ALLOW" and answer.probabilities is None
