"""Réponse coupée par la limite (`finish_reason: length`) quand la RÉFLEXION est active : la fin du raisonnement peut
ressortir dans `content` ; elle n'entre jamais dans l'historique, la relance part sans réflexion et sous un plafond bas, la
3e coupure est un échec propre (jamais le texte). Sans réflexion, le comportement d'avant est gardé (vraie réponse coupée).
Et : sans `length`, rien ne change (empreinte de la requête et de l'historique, figée avant le lot)."""
from __future__ import annotations

import hashlib
import json

import pytest

from nokker.core import model_texts as mt
from nokker.core import person_texts as ptx
from nokker.core.loop import Kernel
from nokker.core.ports import Completion, RouteDecision
from nokker.core.turn import Decision, Gate, Message, Tier, TurnInput, Usage
from tests.unit.test_loop_contract import (
    Ctx, Esc, GatesFake, Inv, LedgerFake, Mem, StoreFake, Tools, ScriptedProvider, tc,
)

LANGS = ("fr", "en", "de", "it")
THINK_TAIL = "…donc la réponse est <think>je vérifie encore le calcul"

# Empreinte (requêtes envoyées + historique + réponses + verdicts) d'une conversation SANS `length`, calculée sur
# origin/p2/integ-beta-core (4ac8a8d7) AVANT le lot, via ce même banc (`git archive`).
EMPREINTE_SANS_LENGTH = "3894775683cb4ca43d836834276763f0124b5043b2c79546eadc55a6b74835a4"


class Rec(ScriptedProvider):
    """Faux amont : `script` = (message, finish_reason) ; garde le corps de chaque requête."""

    def __init__(self, script, model="scripted"):
        super().__init__([], model)
        self.pairs = list(script)
        self.requests: list[dict] = []

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048, **_kw):
        self.requests.append({"messages": json.loads(json.dumps(messages)), "reasoning": reasoning,
                              "max_tokens": max_tokens, "tools": len(tools or [])})
        msg, finish = self.pairs.pop(0)
        return Completion(message=msg, usage=Usage(prompt=100), model=self.model, latency_s=0.01, finish_reason=finish)


class Pol:
    def __init__(self, reasoning):
        self.reasoning = reasoning

    def choose(self, state):
        return RouteDecision(tier=Tier.SHIP, reasoning=self.reasoning, reason="test")


class Obs:
    def __init__(self):
        self.events = []

    def context_for(self, thread_id):
        return []

    def turn_truncated(self, turn, data):
        self.events.append(dict(data))

    def __getattr__(self, name):
        return lambda *a, **k: None


def _kernel(script, *, reasoning, observer=None, **kw):
    inv, tools = Inv(), Tools()
    ship = Rec(script)
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=GatesFake(inv), escalator=Esc(),
               policy=Pol(reasoning), providers={Tier.SHIP: ship, Tier.DEEP: ScriptedProvider([], model="deep")},
               tools=tools, ledger=LedgerFake(), max_tokens=16384, observer=observer, **kw)
    return k, ship, tools


def A(text, **kw):
    return Message("assistant", text, **kw)


# ── rien ne change sans `length` ────────────────────────────────────────────────────────────────────────────────────
def _conversation_hash():
    blob = []
    for reasoning in (False, True):
        script = [(A("un"), "stop"), (A("", tool_calls=[tc("calendar_list")]), None), (A("deux"), "stop"),
                  (A("", tool_calls=[tc("calendar_list")]), "tool_calls"), (A("trois, long mais complet"), "stop")]
        k, ship, tools = _kernel(script, reasoning=reasoning)
        replies = []
        for i, text in enumerate(("bonjour", "agenda ?", "et demain ?")):
            t = k.run_turn(TurnInput("t1", text))
            replies.append({"reply": t.reply, "messages": [m.to_openai() for m in t.messages],
                            "verdicts": [(v.gate.value, v.decision.value, v.rule) for v in t.verdicts],
                            "calls": [(c.tier.value, c.reasoning) for c in t.calls], "failure": t.failure,
                            "error": t.error})
        blob.append({"requests": ship.requests, "turns": replies, "called": tools.called})
    return hashlib.sha256(json.dumps(blob, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def test_empreinte_sans_length():
    assert _conversation_hash() == EMPREINTE_SANS_LENGTH


# ── réflexion active + length ───────────────────────────────────────────────────────────────────────────────────────
def test_cut_content_with_reasoning_never_enters_history_and_relaunch_is_cheap():
    k, ship, _ = _kernel([(A(THINK_TAIL), "length"), (A("La réponse est 42."), "stop")], reasoning=True)
    turn = k.run_turn(TurnInput("t1", "calcule", lang="fr"))
    assert turn.reply == "La réponse est 42." and turn.failure is None
    first, second = ship.requests
    assert first["reasoning"] is True and first["max_tokens"] == 16384
    assert second["reasoning"] is False and second["max_tokens"] == 1024  # relance : sans réflexion, plafond bas
    assert THINK_TAIL not in json.dumps(second["messages"]) and "donc la réponse est" not in json.dumps(second["messages"])
    assert not any("donc la réponse est" in (m.content or "") for m in turn.messages)
    assert second["messages"][-1] == {"role": "user", "content": mt.pick(mt.LENGTH_CUT_THINK, "fr")}
    assert not any(v.rule == "loop:truncated" for v in turn.verdicts)  # une relance qui aboutit : rien à signaler


def test_the_cheap_relaunch_is_for_one_call_only():
    k, ship, _ = _kernel([(A(THINK_TAIL), "length"), (A("", tool_calls=[tc("calendar_list")]), "stop"),
                          (A("Fait."), "stop")], reasoning=True)
    turn = k.run_turn(TurnInput("t1", "agenda ?"))
    assert [r["reasoning"] for r in ship.requests] == [True, False, True]
    assert [r["max_tokens"] for r in ship.requests] == [16384, 1024, 16384]
    assert turn.reply == "Fait."


def test_reasoning_seen_only_in_the_completion_counts_as_active():
    """La politique ne demandait pas la réflexion, mais l'amont rend un `reasoning` : la fin de raisonnement est dans `content`."""
    class Think(Rec):
        def complete(self, messages, tools, **kw):
            comp = super().complete(messages, tools, **kw)
            comp.reasoning = "je réfléchis longuement"
            return comp

    inv = Inv()
    ship = Think([(A(THINK_TAIL), "length"), (A("ok"), "stop")])
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=GatesFake(inv), escalator=Esc(),
               policy=Pol(False), providers={Tier.SHIP: ship, Tier.DEEP: ScriptedProvider([])}, tools=Tools(),
               ledger=LedgerFake(), max_tokens=16384)
    k.run_turn(TurnInput("t1", "x"))
    assert THINK_TAIL not in json.dumps(ship.requests[1]["messages"]) and ship.requests[1]["max_tokens"] == 1024


@pytest.mark.parametrize("lang", LANGS)
def test_three_cuts_with_reasoning_is_a_clean_failure(lang):
    obs = Obs()
    k, ship, _ = _kernel([(A(THINK_TAIL + str(i)), "length") for i in range(3)], reasoning=True, observer=obs)
    turn = k.run_turn(TurnInput("t1", "calcule", lang=lang))
    text = ptx.pick(ptx.LENGTH_FAILED, lang)
    assert turn.reply == text and turn.failure["code"] == "length_truncated" and turn.failure["person_message"] == text
    assert THINK_TAIL not in turn.reply and not any("donc la réponse" in (m.content or "") for m in turn.messages)
    assert turn.messages[-1].role == "assistant" and turn.messages[-1].content == text
    [v] = [v for v in turn.verdicts if v.rule == "loop:truncated"]
    assert v.gate is Gate.SWITCH and v.decision is Decision.DENY
    assert obs.events == [{"reasoning": True, "retries": 2}]
    assert len(ship.requests) == 3 and [r["reasoning"] for r in ship.requests] == [True, False, False]


def test_failure_texts_are_the_agreed_ones():
    assert ptx.LENGTH_FAILED["fr"] == ("La réponse a été interrompue : elle était trop longue. Reformule ou découpe ta "
                                       "demande.")
    assert set(ptx.LENGTH_FAILED) == set(LANGS) and len(set(ptx.LENGTH_FAILED.values())) == 4


# ── sans réflexion + length ─────────────────────────────────────────────────────────────────────────────────────────
def test_cut_without_reasoning_keeps_the_current_behaviour_for_one_or_two_retries():
    k, ship, _ = _kernel([(A("Donc le début"), "length"), (A("puis la suite"), "length"), (A("et la fin."), "stop")],
                         reasoning=False)
    turn = k.run_turn(TurnInput("t1", "explique", lang="fr"))
    assert turn.reply == "et la fin."
    assert [r["reasoning"] for r in ship.requests] == [False] * 3 and [r["max_tokens"] for r in ship.requests] == [16384] * 3
    contents = [m.content for m in turn.messages]
    assert contents[:4] == ["Donc le début", mt.pick(mt.LENGTH_CUT, "fr"), "puis la suite", mt.pick(mt.LENGTH_CUT, "fr")]
    assert not any(v.rule == "loop:truncated" for v in turn.verdicts)


@pytest.mark.parametrize("lang", LANGS)
def test_three_cuts_without_reasoning_show_the_content_followed_by_a_mark(lang):
    obs = Obs()
    k, _, _ = _kernel([(A("a"), "length"), (A("b"), "length"), (A("Voici le texte coupé"), "length")], reasoning=False,
                      observer=obs)
    turn = k.run_turn(TurnInput("t1", "explique", lang=lang))
    mark = mt.pick(mt.LENGTH_MARK, lang)
    assert turn.reply == f"Voici le texte coupé\n\n{mark}" and turn.failure is None
    assert turn.messages[-1].content == turn.reply  # la marque est rangée avec la réponse : le modèle la voit
    [v] = [v for v in turn.verdicts if v.rule == "loop:truncated"]
    assert v.decision is Decision.ALLOW
    assert obs.events == [{"reasoning": False, "retries": 2}]


def test_mark_texts_are_the_agreed_ones():
    assert mt.LENGTH_MARK["fr"] == "(réponse interrompue : trop longue)"
    assert set(mt.LENGTH_MARK) == set(LANGS) and len(set(mt.LENGTH_MARK.values())) == 4
    assert set(mt.LENGTH_CUT_THINK) == set(LANGS) and len(set(mt.LENGTH_CUT_THINK.values())) == 4


# ── length avec appel d'outil : inchangé ────────────────────────────────────────────────────────────────────────────
def test_length_with_a_tool_call_is_unchanged():
    obs = Obs()
    k, ship, tools = _kernel([(A("réflexion " + THINK_TAIL, tool_calls=[tc("calendar_list")]), "length"),
                              (A("Fini."), "stop")], reasoning=True, observer=obs)
    turn = k.run_turn(TurnInput("t1", "agenda ?"))
    assert tools.called == ["calendar_list"] and turn.reply == "Fini."
    assert [r["reasoning"] for r in ship.requests] == [True, True] and [r["max_tokens"] for r in ship.requests] == [16384] * 2
    assert not any(m.content == mt.pick(mt.LENGTH_CUT_THINK, "fr") for m in turn.messages) and obs.events == []
