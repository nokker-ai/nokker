"""Appel direct d'un outil différé sans schéma chargé (`core/deferred_args.py`, `core/loop.py::_tool_step`) : erreur qui cite les
arguments attendus quand un champ requis manque ou est vide ; sinon comportement inchangé."""
from __future__ import annotations

from nokker.core import deferred_args as D
from nokker.core.ports import ToolSpec
from nokker.core.turn import Effect, Message, ToolResult, TurnInput
from tests.unit.test_loop_contract import kernel, tc

MAISON = ToolSpec("maison", "domotique", {
    "type": "object", "required": ["demande"], "properties": {
        "demande": {"type": "string", "description": "ce que l'utilisateur veut, en une phrase"},
        "zone": {"type": "string"}}}, Effect.READ)
PATCH = ToolSpec("patch", "modifie", {"type": "object", "required": ["mode"], "properties": {
    "mode": {"type": "string", "enum": ["replace", "patch"]}}}, Effect.READ)


def run(calls_by_round, spec=MAISON):
    k, tools, *_ = kernel([Message("assistant", tool_calls=c) for c in calls_by_round] + [Message("assistant", "fini")])
    tools.specs = {**tools.specs, spec.name: spec}
    answers = []
    orig = tools.call

    def call(n, a, ctx):
        answers.append((n, a))
        return ToolResult(call_id="", name=n, content="source: demande vide", is_error=True) if n == "maison" and not a.get("demande") else orig(n, a, ctx)
    tools.call = call
    turn = k.run_turn(TurnInput("t1", "allume la lampe"))
    return turn, tools, answers


def tool_msgs(turn):
    return [m for m in turn.messages if m.role == "tool"]


def test_text_cites_name_type_and_short_description():
    assert D.deferred_args_error("maison", MAISON.parameters, {"entity_id": "light.x", "attribute": "state"}) == (
        "maison : arguments attendus : demande (string) : ce que l'utilisateur veut, en une phrase. "
        "Le schéma est chargé : refais l'appel avec ces arguments.")
    assert "valeurs : replace | patch" in D.deferred_args_error("patch", PATCH.parameters, {})


def test_missing_or_empty_required_only():
    p = MAISON.parameters
    assert D.deferred_args_error("maison", p, {"demande": "allume"}) is None
    assert D.deferred_args_error("maison", p, {"demande": "allume", "inconnu": 1}) is None  # requis présents, clé inconnue : inchangé
    for bad in ({}, {"demande": ""}, {"demande": "  "}, {"demande": None}, {"entity_id": "x"}):
        assert D.deferred_args_error("maison", p, bad)
    assert D.deferred_args_error("t", {"type": "object", "required": ["n"]}, {"n": 0}) is None  # 0 et False sont des valeurs
    assert D.deferred_args_error("t", {}, {}) is None and D.deferred_args_error("t", None, None) is None


def test_direct_call_with_invented_arguments_gets_the_schema_error_and_nothing_runs():
    turn, tools, answers = run([[tc("maison", entity_id="light.salon", attribute="state")]])
    assert answers == [] and "maison" not in tools.called  # jamais exécuté
    (m,) = tool_msgs(turn)
    assert m.content.startswith("maison : arguments attendus : demande (string)")
    assert not [v for v in turn.verdicts if v.rule.startswith("effect:")]  # aucune porte, aucun verdict d'action


def test_second_invalid_call_gets_the_source_error_not_the_same_one():
    turn, _tools, answers = run([[tc("maison", entity_id="x")], [tc("maison", entity_id="y")]])
    m1, m2 = tool_msgs(turn)
    assert m1.content.startswith("maison : arguments attendus") and m2.content == "source: demande vide"
    assert answers == [("maison", {"entity_id": "y"})]  # le schéma était chargé après la 1re erreur : la source répond


def test_valid_direct_call_and_unknown_keys_with_required_present_are_unchanged():
    turn, _t, answers = run([[tc("maison", demande="allume la lampe", entity_id="x")]])
    assert answers == [("maison", {"demande": "allume la lampe", "entity_id": "x"})]
    assert tool_msgs(turn)[0].content == "ok"


def test_error_counts_as_a_normal_round_no_infinite_loop():
    k, tools, *_ = kernel([Message("assistant", tool_calls=[tc("maison", x=i)]) for i in range(40)])
    k.max_calls = 4
    tools.specs = {**tools.specs, "maison": MAISON}
    turn = k.run_turn(TurnInput("t1", "allume la lampe"))
    assert len([m for m in turn.messages if m.role == "tool"]) <= 4  # borné par max_calls
