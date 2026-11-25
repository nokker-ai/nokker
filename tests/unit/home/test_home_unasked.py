"""Action d'appareil directe non demandée (`home:unasked`) : sans demande explicite de la personne, jugée par le petit
modèle de l'agent maison, l'action passe par une carte d'approbation et rien n'est exécuté avant ; juge absent, en
panne, trop lent ou illisible : carte. Le contexte du juge est le dernier message de la personne et la dernière réponse
de Nokker. Tours automatiques des routines et objectifs armés, appel direct, carte approuvée : inchangés."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from nokker.core.ports import Approval, ToolContext
from nokker.core.turn import Decision, InputCheck, ToolCall, Turn
from nokker.home import source as S
from tests.unit.home.test_home import FakeProvider, journal, make, plan

LIGHT = plan(("light", "turn_off", "light.salon"))
ARGS = {"domain": "light", "service": "turn_off", "entity_id": "light.salon"}


def turn_ctx(text, *, channel="api", history=(), thread="th_u", index=3, origin=""):
    turn = Turn(thread_id=thread, index=index, input=InputCheck(text=text))
    turn.sender = "alice"
    turn.input.origin = origin  # InputCheck.origin
    kernel = SimpleNamespace(store=SimpleNamespace(history=lambda tid, limit=None: list(history)))
    return ToolContext(thread_id=thread, turn_index=index, channel=channel, sender="alice", kernel=kernel, turn=turn)


def prev(text, reply):
    t = Turn(thread_id="th_u", index=2, input=InputCheck(text=text))
    t.input.origin = ""
    t.reply = reply
    return t


def run(tmp_path, ctx, judge, plan_json=LIGHT):
    src, ha, small, _ship, _k = make(tmp_path, [plan_json, *judge])
    r = src.call("maison", {"demande": "allume la lumière du salon"}, ctx)
    return src, ha, small, r


def test_tired_message_judged_not_explicit_goes_to_a_card_and_nothing_runs(tmp_path):
    _src, ha, small, r = run(tmp_path, turn_ctx("je suis fatigué… une astuce ?"), ["non"])
    assert not r.is_error and ha.calls == []
    assert S.UNASKED_SUMMARY in r.content and '"entity_id": "light.salon"' in r.content and "Fait :" not in r.content
    judge = small.log[1]
    assert judge["messages"][0]["content"] == S.EXPLICIT_JUDGE_SYSTEM
    assert "je suis fatigué… une astuce ?" in judge["messages"][1]["content"]


@pytest.mark.parametrize("text", ["allume le salon", "Schalte das Licht im Wohnzimmer ein",
                                  "turn on the living room light", "accendi la luce del salotto"])
@pytest.mark.parametrize("yes", ["oui", "Yes.", "ja", "sì", " Oui\n", "Oui.", "OUI", "Ja!", "si", "sí", "Oui, bien sûr"])
def test_explicit_request_in_four_languages_runs_directly(tmp_path, text, yes):
    _src, ha, small, r = run(tmp_path, turn_ctx(text), [yes])
    assert not r.is_error and ha.calls == [("light", "turn_off", ["light.salon"], {})]
    assert "Fait :\n- light.turn_off light.salon" in r.content and text in small.log[1]["messages"][1]["content"]


# Aucun de ces messages n'est un exemple du prompt du juge (test_judge_prompt_has_none_of_the_test_messages).
DIRECT = ["Éteins la lampe de la chambre.", "Mets le chauffage à 19 degrés.", "Switch off the kitchen light.",
          "Schalte die Heizung im Bad aus.", "Accendi la luce del corridoio."]
POLITE = ["Allume la lampe de test, s'il te plaît.", "Baisse le store du salon, merci.", "Mets la lampe en marche, s'il vous plaît.",
          "Pourrais-tu éteindre la lampe de test, s'il te plaît ?", "Je peux te demander d'ouvrir la porte du garage ?",
          "Can you turn off the bedroom light?", "Please turn on the test lamp.",
          "Kannst du bitte die Lampe im Wohnzimmer einschalten?", "Könntest du das Licht ausmachen?",
          "Puoi spegnere la luce del salotto?", "Potresti spegnere la luce, per favore?"]
INDIRECT = ["Tu peux allumer la lampe de test ?", "Peux-tu fermer le volet de la chambre ?"]
NOT_ASKED = ["Il fait sombre dans le salon.", "La lampe du salon est-elle allumée ?", "Je voudrais qu'il fasse plus chaud.",
             "Je voudrais que tu me dises si la porte est fermée.", "Quelle température fait-il dans la chambre ?",
             "Is the garage door open?", "Ist das Licht im Bad an?", "oui"]


@pytest.mark.parametrize("text", [*DIRECT, *POLITE, *INDIRECT])
def test_direct_polite_and_indirect_requests_reach_the_judge_whole_and_run_when_it_says_yes(tmp_path, text):
    """Le juge reste l'arbitre (aucun pré-filtre lexical) : la formule arrive intacte, et un « oui » exécute."""
    _src, ha, small, r = run(tmp_path, turn_ctx(text), ["Oui."])
    assert not r.is_error and ha.calls == [("light", "turn_off", ["light.salon"], {})]
    assert f"<<<{text}>>>" in small.log[1]["messages"][1]["content"] and "Fait :" in r.content


@pytest.mark.parametrize("text", [*DIRECT, *POLITE, *INDIRECT, *NOT_ASKED])
def test_anything_the_judge_answers_no_to_is_still_a_card(tmp_path, text):
    _src, ha, _small, r = run(tmp_path, turn_ctx(text), ["non"])
    assert ha.calls == [] and S.UNASKED_SUMMARY in r.content


@pytest.mark.parametrize("text", NOT_ASKED)
def test_not_asked_messages_and_a_bare_yes_without_a_proposal_reach_the_judge_with_an_empty_proposal(tmp_path, text):
    _src, ha, small, r = run(tmp_path, turn_ctx(text), ["non"])
    user = small.log[1]["messages"][1]["content"]
    assert f"<<<{text}>>>" in user and "Dernière réponse de Nokker :\n<<<>>>" in user
    assert ha.calls == [] and S.UNASKED_SUMMARY in r.content


def test_judge_prompt_names_polite_and_indirect_forms_without_the_test_messages():
    user = S.EXPLICIT_JUDGE_USER
    assert "polie ou indirecte" in user and "demande explicite" in user and "autres langues" in user
    assert "souhait sans appareil visé" in user and "je voudrais que" not in user.casefold()
    for text in [t for t in [*DIRECT, *POLITE, *INDIRECT, *NOT_ASKED] if " " in t]:  # « oui » seul est dans le texte
        assert text.rstrip(".?").casefold() not in user.casefold(), text
    assert S.EXPLICIT_YES == {"oui", "yes", "ja", "sì", "si", "sí"}  # lecture stricte inchangée


def test_agreement_to_a_proposal_about_something_else_is_a_card_when_the_judge_says_no(tmp_path):
    reply = "Veux-tu que je décale ta réunion de demain à 15 h ?" + " Le détail de l'agenda suit." * 40
    history = [prev("quoi de prévu demain ?", reply)]
    _src, ha, small, r = run(tmp_path, turn_ctx("oui", history=history), ["non"])
    user = small.log[1]["messages"][1]["content"]
    assert reply[:S.EXPLICIT_TEXT_MAX] in user and reply[:S.EXPLICIT_TEXT_MAX + 1] not in user  # intacte, tronquée à 600
    assert "<<<oui>>>" in user and user.rstrip().endswith("Réponds uniquement par oui ou non, en français.")
    assert ha.calls == [] and S.UNASKED_SUMMARY in r.content


@pytest.mark.parametrize("word", ["oui", "ja", "yes", "sì"])
def test_agreement_after_a_proposal_gives_the_previous_reply_to_the_judge(tmp_path, word):
    history = [prev("il fait sombre", "Veux-tu que j'allume la lumière du salon ?")]
    _src, ha, small, _r = run(tmp_path, turn_ctx(word, history=history), ["oui"])
    user = small.log[1]["messages"][1]["content"]
    assert ha.calls and f"<<<{word}>>>" in user and "Veux-tu que j'allume la lumière du salon ?" in user


@pytest.mark.parametrize("reply", ["non", "No", "nein", "peut-être", "", "vraiment oui", "ouiii", "si tu veux", "non, oui"])
def test_anything_but_a_strict_yes_is_a_card(tmp_path, reply):
    _src, ha, _small, r = run(tmp_path, turn_ctx("quoi de neuf ?"), [reply])
    assert ha.calls == [] and S.UNASKED_SUMMARY in r.content


@pytest.mark.parametrize("answer,raw,explicit", [("oui", "oui", True), ("Non\nparce que ce n'est pas une demande", "Non parce que ce", False),
                                                  ("", "", False)])
def test_judged_event_keeps_the_raw_answer_cut_to_16_characters_on_one_line(tmp_path, answer, raw, explicit):
    run(tmp_path, turn_ctx("allume le salon"), [answer])
    judged = [r for r in journal(tmp_path) if r["event"] == "home.plan" and r["outcome"] == "judged"]
    assert len(judged) == 1 and judged[0]["what"] == {"explicit": explicit, "raw": raw}
    assert len(judged[0]["what"]["raw"]) <= S.JUDGE_RAW_MAX == 16


def test_judge_failures_never_execute(tmp_path, monkeypatch):
    _src, ha, _small, r = run(tmp_path, turn_ctx("allume le salon"), [])  # le faux modèle n'a plus de réponse : exception
    assert ha.calls == [] and S.UNASKED_SUMMARY in r.content

    class Slow(FakeProvider):
        def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
            if len(self.log) >= 1:
                time.sleep(0.4)
            return super().complete(messages, tools, reasoning=reasoning, session_key=session_key,
                                    max_tokens=max_tokens)

    monkeypatch.setattr(S, "EXPLICIT_JUDGE_TIMEOUT_S", 0.05)
    src2, ha2, *_rest = make(tmp_path / "b", [LIGHT, "oui"])
    kernel_small = Slow([LIGHT, "oui"])
    src2.providers = lambda: {src2_tier: kernel_small for src2_tier in __import__("nokker.core.turn", fromlist=["Tier"]).Tier}
    r2 = src2.call("maison", {"demande": "allume le salon"}, turn_ctx("allume le salon"))
    assert ha2.calls == [] and S.UNASKED_SUMMARY in r2.content


def test_automatic_and_direct_calls_keep_their_behaviour(tmp_path):
    for channel in ("agent", "goal", "local-direct"):  # routine ou objectif armés par la personne, appel du propriétaire
        _src, ha, small, _r = run(tmp_path / channel, turn_ctx("(tâche)", channel=channel), [])
        assert ha.calls == [("light", "turn_off", ["light.salon"], {})] and len(small.log) == 1
    src, ha, small, _s, _k = make(tmp_path / "x", [LIGHT])  # sans tour de la personne (carte approuvée, banc)
    src.call("maison", {"demande": "allume la lumière du salon"},
             ToolContext(thread_id="th_1", turn_index=0, channel="api", sender="alice"))
    assert ha.calls and len(small.log) == 1
    src, ha, small, _r = run(tmp_path / "other", turn_ctx("(timer)", channel="forge"), ["non"])  # autre canal : jugé
    assert ha.calls == []


def test_strong_actions_and_noop_never_reach_the_judge(tmp_path):
    _src, _ha, small, r = run(tmp_path, turn_ctx("ouvre la porte"), [], plan(("lock", "unlock", "lock.entree")))
    assert "À confirmer (carte forte" in r.content and S.UNASKED_SUMMARY not in r.content and len(small.log) == 1


def test_unasked_card_approval_runs_the_action_and_is_not_asked_twice(tmp_path):
    src, ha, _small, _r = run(tmp_path, turn_ctx("une astuce ?"), ["non"])
    turn = turn_ctx("une astuce ?").turn
    v = src.verdict(ToolCall(id="c", name="maison_action_forte", arguments=ARGS), turn)
    assert v.decision is Decision.HITL and v.rule == "home:unasked" and v.strength is None
    from nokker.approvals.crypto import args_sha256

    card = Approval(id="ap9", thread_id="th_u", turn_index=3, channel="api", tool_name="maison_action_forte",
                    arguments=ARGS, args_sha256=args_sha256(ARGS), strength=None, rule="home:unasked",
                    status="pending", decided_by="", decided_via="")
    src.approvals = lambda: SimpleNamespace(get=lambda i: card)
    ctx = ToolContext(thread_id="th_u", turn_index=3, channel="api", sender="alice", approval_id="ap9")
    assert src.call("maison_action_forte", ARGS, ctx).is_error and ha.calls == []  # carte encore en attente
    card.status, card.decided_via = "approved", "local-token"  # accord de la personne : le jeton local suffit
    out = src.call("maison_action_forte", ARGS, ctx)
    assert not out.is_error and ha.calls == [("light", "turn_off", ["light.salon"], {})]
    assert src.call("maison_action_forte", {**ARGS, "service": "turn_on"}, ctx).is_error  # arguments changés
    # une carte `home:unasked` ne lève jamais une action sensible
    strong = {"domain": "lock", "service": "unlock", "entity_id": "lock.entree"}
    card.arguments, card.args_sha256 = strong, args_sha256(strong)
    assert src.call("maison_action_forte", strong, ctx).is_error
    # même action, même fil : plus de carte (déjà approuvée), sans nouvel appel du juge
    src.providers = dict
    again = src.call("maison", {"demande": "allume la lumière du salon"}, turn_ctx("encore ?"))
    assert again.is_error  # agent sans modèle ici ; on vérifie l'autre voie ci-dessous
    assert src._approved_before(ctx, SimpleNamespace(domain="light", service="turn_off", entity_id="light.salon", data={}))


# ── par le vrai serveur : « je suis fatigué… une astuce ? » → carte posée, rien d'exécuté ──
class _HomeTools:
    def __init__(self, src):
        self.src = src
        self.specs = {s.name: s for s in src.specs()}

    def core(self):
        return list(self.specs.values())

    def spec(self, n):
        return self.specs.get(n)

    def find(self, q, limit=5):
        return []

    def index(self):
        return []

    def call(self, n, a, ctx):
        return self.src.call(n, a, ctx)


class _MainModel:
    """Le modèle principal agit tout seul : `maison`, puis (À confirmer) `maison_action_forte`, puis conclut."""

    forte = ARGS

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        from nokker.core.ports import Completion
        from nokker.core.turn import Message, Usage

        n = sum(1 for m in messages if m.get("role") == "tool")
        if n == 0:
            tc = ToolCall(id="c1", name="maison", arguments={"demande": "allume la lumière du salon"})
        elif n == 1:
            tc = ToolCall(id="c2", name="maison_action_forte", arguments=self.forte)
        else:
            return Completion(message=Message(role="assistant", content="J'attends ton accord."), usage=Usage(prompt=5),
                              model="t", latency_s=0.0)
        return Completion(message=Message(role="assistant", tool_calls=[tc]), usage=Usage(prompt=5), model="t",
                          latency_s=0.0)


@pytest.mark.parametrize("judge,carded", [("non", True), ("oui", False)])
def test_server_unasked_home_action_poses_a_card_and_runs_nothing(tmp_path, judge, carded):
    import threading
    from http.server import ThreadingHTTPServer

    from nokker.api.server import make_handler
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.core.turn import Tier
    from tests.unit.api.test_approvals_routes import ReplayingContext, make_kernel_with_approvals
    from tests.unit.test_thread_serialization import _req

    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    src, ha, small, _s, _hk = make(tmp_path, [LIGHT, judge])
    kernel.context = ReplayingContext()
    kernel.tools = _HomeTools(src)
    kernel.providers = {Tier.SHIP: _MainModel(), Tier.LOCAL: small, Tier.DEEP: FakeProvider([])}
    src.bind(kernel)
    kernel.gates = S.HomeGates(inner=kernel.gates, source=src)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {"tok": "alice"}, approvals=approvals))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, body = _req(httpd.server_address[1], "/v1/chat/completions", token="tok", timeout=20,
                            headers={"X-Session-Id": "th_srv"},
                            body={"messages": [{"role": "user", "content": "je suis fatigué… une astuce ?"}]})
        assert status == 200, body
    finally:
        httpd.shutdown()
    cards = approvals.pending()
    if carded:
        assert ha.calls == [] and len(cards) == 1
        # carte HITL standard, SANS exigence d'appareil : ni `home:strong` ni `strength` forte
        assert cards[0].rule == "home:unasked" and cards[0].tool_name == "maison_action_forte"
        assert cards[0].strength != "strong" and cards[0].rule != S.STRONG_RULE
    else:
        assert ha.calls == [("light", "turn_off", ["light.salon"], {})]  # (le faux modèle appelle ensuite la voie à carte : sans effet de plus)


def test_server_a_real_strong_action_still_poses_home_strong(tmp_path):
    import threading
    from http.server import ThreadingHTTPServer

    from nokker.api.server import make_handler
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.core.turn import Tier
    from tests.unit.api.test_approvals_routes import ReplayingContext, make_kernel_with_approvals
    from tests.unit.test_thread_serialization import _req

    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    src, ha, small, _s, _hk = make(tmp_path, [])
    main = _MainModel()
    main.forte = {"domain": "lock", "service": "unlock", "entity_id": "lock.entree"}
    kernel.context = ReplayingContext()
    kernel.tools = _HomeTools(src)
    kernel.providers = {Tier.SHIP: main, Tier.LOCAL: small, Tier.DEEP: FakeProvider([])}
    src.bind(kernel)
    kernel.gates = S.HomeGates(inner=kernel.gates, source=src)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {"tok": "alice"}, approvals=approvals))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        # le faux modèle appelle directement la voie forte pour une serrure
        from nokker.core.ports import Completion
        from nokker.core.turn import Message, Usage

        class Direct:
            def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
                if not any(m.get("role") == "tool" for m in messages):
                    tc = ToolCall(id="c2", name="maison_action_forte", arguments=main.forte)
                    return Completion(message=Message(role="assistant", tool_calls=[tc]), usage=Usage(prompt=5),
                                      model="t", latency_s=0.0)
                return Completion(message=Message(role="assistant", content="J'attends ton accord."),
                                  usage=Usage(prompt=5), model="t", latency_s=0.0)

        kernel.providers[Tier.SHIP] = Direct()
        status, _body = _req(httpd.server_address[1], "/v1/chat/completions", token="tok", timeout=20,
                             headers={"X-Session-Id": "th_srv2"},
                             body={"messages": [{"role": "user", "content": "ouvre la porte d'entrée"}]})
        assert status == 200
    finally:
        httpd.shutdown()
    (card,) = approvals.pending()
    assert ha.calls == [] and card.rule == "home:strong" and card.strength == "strong"
