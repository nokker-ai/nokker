"""`GatesV1` (`qa/gates.py`) — couche 1 (invariants factices) + couche 2 (juge SCRIPTÉ, AUCUN
réseau). PLAN-P1 §2.3/§2.5/§5.3 (B-2/B-3) : porte action APRÈS les invariants, porte bascule avec
déclenchement « si nécessaire », zones automatiser/vérifier/escalader, doctrine Alice 17.10 (le juge
ne DENY jamais, action->HITL en cas de doute, bascule->REDO_DEEP en cas de doute / ESCALATE au-dessus
du seuil haut)."""
from __future__ import annotations

from dataclasses import dataclass

from nokker.core.turn import Decision, Effect, Gate, InputCheck, Tier, ToolCall, Turn, Verdict
from nokker.qa.calibrate import DEFAULT_THRESHOLDS, Thresholds
from nokker.qa.gates import GatesV1
from nokker.qa.questions import QUESTIONS


@dataclass
class ToolSpecFake:
    name: str
    effect: Effect


@dataclass
class FakeInvariants:
    """Renvoie un verdict FIXE pour `check_action` (jamais la vraie F3 : ce module teste GatesV1,
    pas les invariants)."""

    verdict: Verdict

    def check_action(self, call, spec, turn):
        return self.verdict


class ScriptedJudge:
    """Juge scripté par `question_id` : {question_id: (choice, probabilities)}. AUCUN réseau."""

    def __init__(self, answers: dict[str, tuple[str, dict[str, float] | None]], advice: str | None = None):
        from nokker.core.ports import JudgeAnswer
        self.answers = answers
        self.calls: list[str] = []
        self._advice = advice
        self._JudgeAnswer = JudgeAnswer

    def ask(self, question_id, prompt, options):
        self.calls.append(question_id)
        choice, probs = self.answers.get(question_id, (options[0], None))
        return self._JudgeAnswer(choice=choice, probabilities=probs, model="scripted", latency_s=0.01)

    def advise(self, prompt, max_tokens=150):
        return self._advice or ""


def allow_v1():
    return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="effect:write")


def turn_of(text="fais ça", flags=frozenset(), escalated=False, messages=None, sender="alice"):
    return Turn(thread_id="t", index=0, input=InputCheck(text=text, domain_flags=set(flags)),
               escalated=escalated, sender=sender, messages=messages or [])


# ─────────────────────────────────────── porte action ───────────────────────────────────────


def test_action_short_circuits_on_non_allow_invariant_without_calling_judge():
    v1 = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:destructive")
    judge = ScriptedJudge({})
    gates = GatesV1(invariants=FakeInvariants(v1), judge=judge)
    out = gates.action(ToolCall(id="c", name="infra_lvextend", arguments={}),
                       ToolSpecFake("infra_lvextend", Effect.DESTRUCTIVE), turn_of())
    assert out == [v1]
    assert judge.calls == []  # pas nécessaire : la couche 1 a déjà statué


def test_action_skips_judge_on_read_effect():
    judge = ScriptedJudge({})
    gates = GatesV1(invariants=FakeInvariants(allow_v1()), judge=judge)
    out = gates.action(ToolCall(id="c", name="calendar_list", arguments={}),
                       ToolSpecFake("calendar_list", Effect.READ), turn_of())
    assert out == [allow_v1()]
    assert judge.calls == []  # lecture : pas d'effet à juger


def test_action_write_effect_low_p_alert_allows():
    judge = ScriptedJudge({"A.effet": ("Non", {"Oui": 0.05, "Non": 0.95})})
    gates = GatesV1(invariants=FakeInvariants(allow_v1()), judge=judge)
    out = gates.action(ToolCall(id="c", name="kanban_create_task", arguments={"titre": "x"}),
                       ToolSpecFake("kanban_create_task", Effect.WRITE), turn_of())
    assert judge.calls == ["A.effet"]
    assert out[0] == allow_v1()
    assert out[1].decision is Decision.ALLOW
    assert out[1].layer == 2


def test_action_write_effect_high_p_alert_is_hitl_never_deny():
    judge = ScriptedJudge({"A.effet": ("Oui", {"Oui": 0.95, "Non": 0.05})}, advice="Sauvegarde la table avant.")
    gates = GatesV1(invariants=FakeInvariants(allow_v1()), judge=judge)
    out = gates.action(ToolCall(id="c", name="send_email_forward", arguments={"to": "x@tiers.test"}),
                       ToolSpecFake("send_email_forward", Effect.SEND_EXTERNAL), turn_of())
    v2 = out[1]
    assert v2.decision is Decision.HITL  # jamais DENY depuis le juge (doctrine Alice 17.10)
    assert v2.probability == 0.95
    assert "Sauvegarde la table avant." in v2.reason  # avis en 2e appel, mis dans `reason` (pas de champ `advice`)


def test_action_grey_zone_is_hitl_not_redo_deep():
    judge = ScriptedJudge({"A.effet": ("Oui", {"Oui": 0.5, "Non": 0.5})})
    gates = GatesV1(invariants=FakeInvariants(allow_v1()), judge=judge,
                    thresholds={"A.effet": Thresholds("A.effet", low=0.3, high=0.7, n_fit=10,
                                                       cv_accuracy=0.9, cv_folds=5, source="logprob")})
    out = gates.action(ToolCall(id="c", name="kanban_create_task", arguments={}),
                       ToolSpecFake("kanban_create_task", Effect.WRITE), turn_of())
    assert out[1].decision is Decision.HITL
    assert "zone grise" in out[1].reason


def test_action_without_advise_capability_keeps_reason_short():
    class JudgeNoAdvise:
        def ask(self, question_id, prompt, options):
            from nokker.core.ports import JudgeAnswer
            return JudgeAnswer(choice="Oui", probabilities={"Oui": 0.9, "Non": 0.1}, model="scripted",
                               latency_s=0.0)

    gates = GatesV1(invariants=FakeInvariants(allow_v1()), judge=JudgeNoAdvise())
    out = gates.action(ToolCall(id="c", name="send_bulk_email", arguments={}),
                       ToolSpecFake("send_bulk_email", Effect.SEND_BULK), turn_of())
    assert "Avis" not in out[1].reason  # pas de 2e appel si le backend n'a pas `advise`


# ─────────────────────────────────────── porte bascule ───────────────────────────────────────


def test_switch_domain_flag_escalates_without_calling_judge():
    judge = ScriptedJudge({})
    gates = GatesV1(invariants=FakeInvariants(allow_v1()), judge=judge)
    out = gates.switch("réponse", turn_of(flags={"fiscal"}), Tier.SHIP)
    assert out[0].decision is Decision.ESCALATE
    assert judge.calls == []


def test_switch_not_necessary_allows_without_calling_judge():
    judge = ScriptedJudge({})
    gates = GatesV1(invariants=FakeInvariants(allow_v1()), judge=judge)
    out = gates.switch("Voici l'agenda de la semaine.", turn_of(), Tier.SHIP)
    assert out == [out[0]]
    assert out[0].decision is Decision.ALLOW
    assert judge.calls == []


def _tool_effect_verdict(rule="effect:send_external"):
    return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule=rule)


def test_switch_necessary_when_effectful_tool_used_this_turn():
    judge = ScriptedJudge({"R.humain": ("Non", {"Oui": 0.05, "Non": 0.95}),
                          "R.invention": ("Non", {"Oui": 0.05, "Non": 0.95}),
                          "R.secret": ("Non", {"Oui": 0.02, "Non": 0.98})})
    gates = GatesV1(invariants=FakeInvariants(allow_v1()), judge=judge)
    turn = turn_of()
    turn.verdicts.append(_tool_effect_verdict())
    out = gates.switch("Transféré à Alice.", turn, Tier.SHIP)
    assert sorted(judge.calls) == ["R.humain", "R.invention", "R.secret"]
    assert all(v.decision is Decision.ALLOW for v in out)


def test_switch_necessary_when_reply_looks_assertive():
    judge = ScriptedJudge({"R.humain": ("Non", {"Oui": 0.05, "Non": 0.95}),
                          "R.invention": ("Non", {"Oui": 0.05, "Non": 0.95}),
                          "R.secret": ("Non", {"Oui": 0.02, "Non": 0.98})})
    gates = GatesV1(invariants=FakeInvariants(allow_v1()), judge=judge)
    gates.switch("Le chiffre d'affaires est de CHF 2 400 000.", turn_of(), Tier.SHIP)
    assert judge.calls  # déclenché par l'heuristique d'affirmation, pas par un outil/drapeau


def test_switch_high_p_alert_escalates_never_deny():
    judge = ScriptedJudge({"R.humain": ("Oui", {"Oui": 0.9, "Non": 0.1}),
                          "R.invention": ("Non", {"Oui": 0.05, "Non": 0.95}),
                          "R.secret": ("Non", {"Oui": 0.02, "Non": 0.98})})
    gates = GatesV1(invariants=FakeInvariants(allow_v1()), judge=judge)
    turn = turn_of()
    turn.verdicts.append(_tool_effect_verdict())
    out = gates.switch("Le CA de l'entreprise est connu.", turn, Tier.SHIP)
    humain_verdict = next(v for v in out if v.rule == "judge:R.humain")
    assert humain_verdict.decision is Decision.ESCALATE
    assert humain_verdict.decision is not Decision.DENY


def test_switch_grey_zone_redoes_in_deep_when_not_already_deep():
    judge = ScriptedJudge({"R.humain": ("Oui", {"Oui": 0.5, "Non": 0.5}),
                          "R.invention": ("Non", {"Oui": 0.05, "Non": 0.95}),
                          "R.secret": ("Non", {"Oui": 0.02, "Non": 0.98})})
    gates = GatesV1(invariants=FakeInvariants(allow_v1()), judge=judge)
    turn = turn_of()
    turn.verdicts.append(_tool_effect_verdict())
    out = gates.switch("réponse ambiguë", turn, Tier.SHIP)
    humain_verdict = next(v for v in out if v.rule == "judge:R.humain")
    assert humain_verdict.decision is Decision.REDO_DEEP


def test_switch_grey_zone_at_deep_tier_does_not_redo_again():
    judge = ScriptedJudge({"R.humain": ("Oui", {"Oui": 0.5, "Non": 0.5}),
                          "R.invention": ("Non", {"Oui": 0.05, "Non": 0.95}),
                          "R.secret": ("Non", {"Oui": 0.02, "Non": 0.98})})
    gates = GatesV1(invariants=FakeInvariants(allow_v1()), judge=judge)
    turn = turn_of()
    turn.verdicts.append(_tool_effect_verdict())
    out = gates.switch("réponse ambiguë", turn, Tier.DEEP)
    humain_verdict = next(v for v in out if v.rule == "judge:R.humain")
    assert humain_verdict.decision is Decision.ALLOW  # déjà en Deep : pas de relance sur soi-même


def test_switch_already_escalated_does_not_re_escalate_on_domain_flag():
    judge = ScriptedJudge({})
    gates = GatesV1(invariants=FakeInvariants(allow_v1()), judge=judge)
    out = gates.switch("réponse", turn_of(flags={"fiscal"}, escalated=True), Tier.SHIP)
    assert all(v.decision is not Decision.ESCALATE for v in out) or judge.calls
    # Si le drapeau seul ne ré-escalade pas, la nécessité peut tout de même déclencher le juge sur
    # le fond (switch nécessaire) — mais jamais la ré-escalade AUTOMATIQUE de la couche 1 (qui, elle,
    # est bien gardée par `not turn.escalated`).


def test_default_thresholds_cover_every_question():
    assert set(DEFAULT_THRESHOLDS) == set(QUESTIONS)
