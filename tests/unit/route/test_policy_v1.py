"""X8 : routage par tour sur trois paliers (`PolicyV1`, `route/classify.py`) et repli sur un seul modèle."""
from __future__ import annotations

from nokker.core.ports import RouteState
from nokker.core.turn import Decision, Gate, InputCheck, Message, Tier, ToolCall, Turn, Verdict
from nokker.route.classify import ModelClassifier, TaskClass, classify, classify_rules
from nokker.route.policy import PolicyV1, resolve

ALL = (Tier.SMALL, Tier.SHIP, Tier.DEEP)


def state(text, thread="t", index=0, call_index=0, messages=(), **kw):
    turn = Turn(thread_id=thread, index=index, input=InputCheck(text=text))
    turn.messages = list(messages)
    return RouteState(turn=turn, call_index=call_index, **kw)


def policy(tiers=ALL, **kw):
    return PolicyV1(available=lambda: tiers, **kw)


# ── règles ──

def test_courtesy_alone_is_trivial():
    for text in ("merci !", "Merci beaucoup", "bonjour", "ok merci", "super, merci 🙏", "bonne nuit"):
        assert classify_rules(text).task is TaskClass.TRIVIAL, text


def test_go_words_are_never_trivial():
    # Un GO peut répondre à « je supprime ? » : jamais au petit palier.
    for text in ("oui", "ok", "vas-y", "merci, vas-y", "d'accord merci", "parfait", "go", "oui merci"):
        assert classify_rules(text).task is not TaskClass.TRIVIAL, text


def test_code_requests_are_code():
    for text in ("corrige le bug dans parser.py", "ajoute un test pour la fonction slugify",
                 "Traceback (most recent call last):\n  File \"a.py\", line 3", "```py\nprint(1)\n```",
                 "implémente l'endpoint /health", "pytest échoue sur test_api"):
        assert classify_rules(text).task is TaskClass.CODE, text


def test_short_follow_up_of_a_code_turn_stays_code():
    assert classify_rules("et les tests ?", TaskClass.CODE).task is TaskClass.CODE
    assert classify_rules("vas-y", TaskClass.CODE).task is TaskClass.CODE
    assert classify_rules("et les tests ?", TaskClass.CONVERSATION).task is TaskClass.CONVERSATION


def test_conversation_is_not_sure_so_the_model_may_decide():
    c = classify_rules("qu'est-ce que je fais demain ?")
    assert c.task is TaskClass.CONVERSATION and not c.sure


class FakeTransport:
    def __init__(self, content="C", fail=False):
        self.content, self.fail, self.bodies = content, fail, []

    def post(self, body):
        self.bodies.append(body)
        if self.fail:
            raise RuntimeError("HTTP 502")
        return {"choices": [{"message": {"content": self.content}}]}


def test_model_classifier_only_for_unsure_turns_and_never_trivial_on_go():
    t = FakeTransport("C")
    m = ModelClassifier(transport=t)
    assert classify("merci !", None, m).task is TaskClass.TRIVIAL and t.bodies == []  # règle sûre : aucun appel
    assert classify("regarde pourquoi le service plante au démarrage", None, m).task is TaskClass.CODE
    assert len(t.bodies) == 1 and t.bodies[0]["max_tokens"] == 1
    t2 = FakeTransport("A")
    assert classify("ok on y va", None, ModelClassifier(transport=t2)).task is TaskClass.CONVERSATION


def test_model_classifier_down_keeps_the_rule():
    m = ModelClassifier(transport=FakeTransport(fail=True))
    assert classify("explique-moi le plan", None, m).task is TaskClass.CONVERSATION


# ── politique ──

def test_three_tiers_by_task_class():
    p = policy()
    assert p.choose(state("merci !", thread="a")).tier is Tier.SMALL
    assert p.choose(state("résume ce mail", thread="b")).tier is Tier.SHIP
    d = p.choose(state("corrige le bug dans app.py", thread="c"))
    assert d.tier is Tier.DEEP and d.reasoning and "code" in d.reason


def test_class_is_fixed_for_the_whole_turn():
    p = policy()
    s0 = state("corrige le bug dans app.py", thread="x")
    assert p.choose(s0).tier is Tier.DEEP
    s1 = RouteState(turn=s0.turn, call_index=3)
    assert p.choose(s1).tier is Tier.DEEP


def test_trivial_that_calls_a_tool_goes_up_to_ship():
    p = policy()
    s = state("merci !", thread="y")
    assert p.choose(s).tier is Tier.SMALL
    assert p.choose(RouteState(turn=s.turn, call_index=1)).tier is Tier.SHIP


def test_writing_a_file_in_a_conversation_turn_moves_to_deep():
    p = policy()
    write = Message(role="assistant", tool_calls=[ToolCall(id="1", name="patch", arguments={"mode": "replace"})])
    s = state("range un peu ce dossier", thread="z", call_index=2, messages=[write])
    assert p.choose(s).tier is Tier.DEEP


def test_v0_signals_still_win():
    p = policy()
    v = Verdict(gate=Gate.SWITCH, decision=Decision.REDO_DEEP, layer=2, rule="judge:R.invention")
    assert p.choose(state("merci !", thread="s", switch_verdicts=[v])).tier is Tier.DEEP
    assert p.choose(state("résume", thread="s2", tool_failures=3)).tier is Tier.DEEP


def test_missing_tiers_fall_back_to_a_single_model_without_error():
    only_ship = policy(tiers=(Tier.SHIP,))
    assert only_ship.choose(state("merci !", thread="f1")).tier is Tier.SHIP
    d = only_ship.choose(state("corrige le bug dans app.py", thread="f2"))
    assert d.tier is Tier.SHIP and "absent" in d.reason
    only_deep = policy(tiers=(Tier.DEEP,))
    assert only_deep.choose(state("merci !", thread="f3")).tier is Tier.DEEP
    assert resolve(Tier.SMALL, (Tier.SHIP, Tier.DEEP)) is Tier.SHIP
    assert resolve(Tier.DEEP, (Tier.SMALL,)) is Tier.SMALL


def test_code_to_deep_off_keeps_code_on_ship():
    p = policy(code_to_deep=False)
    assert p.choose(state("corrige le bug dans app.py", thread="o")).tier is Tier.SHIP


def test_previous_turn_class_is_remembered_per_thread():
    p = policy()
    assert p.choose(state("corrige le bug dans app.py", thread="m", index=0)).tier is Tier.DEEP
    assert p.choose(state("et les tests ?", thread="m", index=1)).tier is Tier.DEEP
    assert p.choose(state("et les tests ?", thread="other", index=0)).tier is Tier.SHIP
