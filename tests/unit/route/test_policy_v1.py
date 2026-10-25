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


# ── images (X7) : un tour avec image ne part jamais sur un palier sans vision ──

def image_state(text, thread, **kw):
    from nokker.core.turn import ImageRef

    s = state(text, thread=thread, **kw)
    s.turn.images = [ImageRef(sha256="ab" * 32, media_type="image/png", size=68, name="capture.png")]
    return s


def vision_policy(vision=(Tier.SHIP, Tier.DEEP), **kw):
    return policy(vision=lambda: vision, **kw)


def test_trivial_turn_with_an_image_goes_to_a_tier_with_vision():
    # La bêta : petit palier 35B sans vision, Ship et Deep (397B) avec. « merci ! » + capture : jamais le 35B.
    p = vision_policy()
    s = image_state("merci !", thread="i1")
    d = p.choose(s)
    assert d.tier is Tier.SHIP and "image" in d.reason
    assert p.choose(RouteState(turn=s.turn, call_index=1)).tier is Tier.SHIP  # la suite du tour aussi


def test_text_turns_keep_their_tier_and_their_cost():
    p = vision_policy()
    assert p.choose(state("merci !", thread="t1")).tier is Tier.SMALL  # sans image : petit palier, comme avant
    assert p.choose(state("résume ce mail", thread="t2")).tier is Tier.SHIP
    assert p.choose(state("corrige le bug dans app.py", thread="t3")).tier is Tier.DEEP


def test_image_turn_on_a_tier_without_vision_moves_to_the_nearest_one_with_it():
    only_deep = vision_policy(vision=(Tier.DEEP,))
    assert only_deep.choose(image_state("merci !", thread="v1")).tier is Tier.DEEP
    assert only_deep.choose(image_state("que vois-tu sur cette capture ?", thread="v2")).tier is Tier.DEEP
    # Déjà sur un palier avec vision : rien ne change (code → Deep, conversation → Ship).
    p = vision_policy()
    assert p.choose(image_state("corrige le bug visible sur la capture dans app.py", thread="v3")).tier is Tier.DEEP
    assert p.choose(image_state("que vois-tu sur cette capture ?", thread="v4")).tier is Tier.SHIP


def test_no_tier_with_vision_keeps_the_routing():
    # Aucun palier ne lit les images (clé de l'utilisateur, modèle local) : le routage ne change pas, l'image est
    # signalée non lue par le noyau (`attachment.skipped`), comme avant.
    p = vision_policy(vision=())
    assert p.choose(image_state("merci !", thread="n1")).tier is Tier.SMALL
    assert policy().choose(image_state("merci !", thread="n2")).tier is Tier.SMALL  # vision inconnue : inchangé


def test_build_policy_reads_the_vision_of_the_served_tiers():
    """`build_policy` : la vision lue sur les fournisseurs servis À CHAQUE tour (un palier retiré à chaud compris)."""
    from types import SimpleNamespace

    from nokker.api.server import build_policy
    from nokker.config import NokkerConfig

    providers = {Tier.SMALL: SimpleNamespace(vision=False), Tier.SHIP: SimpleNamespace(vision=True),
                 Tier.DEEP: SimpleNamespace(vision=True)}
    p = build_policy(NokkerConfig(route={"policy": "v1"}), providers)
    assert p.choose(image_state("merci !", thread="b1")).tier is Tier.SHIP
    assert p.choose(state("merci !", thread="b2")).tier is Tier.SMALL
    providers.pop(Tier.SHIP)
    assert p.choose(image_state("merci !", thread="b3")).tier is Tier.DEEP
