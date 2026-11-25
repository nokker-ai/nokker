"""Appel d'outil écrit en TEXTE (« [tool_input] », « [Appel de l'outil : … ] ») avec `tool_calls` vide : le noyau ne l'interprète
ni ne l'exécute jamais ; il relance le même appel UNE fois par tour, sans rien ajouter à l'historique, et la relance compte dans
`max_calls`. Hors de ces deux motifs stricts en tête du content, rien ne change (empreinte figée sur origin)."""
from __future__ import annotations

import hashlib
import json

import pytest

from nokker.core.loop import Kernel
from nokker.core.ports import Completion, RouteDecision
from nokker.core.turn import Decision, Gate, Message, Tier, TurnInput, Usage
from tests.unit.test_loop_contract import Ctx, Esc, GatesFake, Inv, LedgerFake, Mem, ScriptedProvider, StoreFake, Tools, tc

# Empreinte d'une conversation ordinaire (requêtes envoyées, historique, réponses, verdicts), calculée sur
# origin/p2/integ-beta-core (3992fc13) AVANT le lot, via ce même banc (`git archive`).
EMPREINTE_ORDINAIRE = "d40cb9372f9c89a9c51da2ffc534162f7b016670ee3ff5ffe27a6eab6f3eea64"

CASE1 = "[tool_input]"
CASE2 = '[Appel de l\'outil : terminal avec {"command":"ls"}]'


class Rec(ScriptedProvider):
    def __init__(self, script):
        super().__init__([])
        self.pairs = list(script)
        self.requests: list[dict] = []

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048, **_kw):
        self.requests.append(json.loads(json.dumps({"messages": messages, "tools": tools, "reasoning": reasoning,
                                                    "max_tokens": max_tokens}, default=str)))
        return Completion(message=self.pairs.pop(0), usage=Usage(prompt=100), model="scripted", latency_s=0.01,
                          finish_reason="stop")


class Ctx2(Ctx):
    def __init__(self, tools):
        self.tools = tools

    def assemble(self, turn, history, recall, loaded, tier):
        out = super().assemble(turn, history, recall, loaded, tier)
        out.tools = [{"type": "function", "function": {"name": "terminal"}}] if self.tools else []
        return out


class Pol:
    def choose(self, state):
        return RouteDecision(tier=Tier.SHIP, reasoning=False, reason="test")


class Obs:
    def __init__(self):
        self.events = []

    def context_for(self, thread_id):
        return []

    def turn_text_toolcall_retry(self, turn, data):
        self.events.append(dict(data))

    def __getattr__(self, name):
        return lambda *a, **k: None


def A(text, **kw):
    return Message("assistant", text, **kw)


def _kernel(script, *, with_tools=True, observer=None, **kw):
    inv, tools = Inv(), Tools()
    ship = Rec(script)
    k = Kernel(store=StoreFake(), context=Ctx2(with_tools), memory=Mem(), invariants=inv, gates=GatesFake(inv),
               escalator=Esc(), policy=Pol(), providers={Tier.SHIP: ship, Tier.DEEP: ScriptedProvider([])}, tools=tools,
               ledger=LedgerFake(), observer=observer, **kw)
    return k, ship, tools


def _hash():
    blob = []
    for with_tools in (True, False):
        script = [A("un"), A("[Note] voici"), A("", tool_calls=[tc("calendar_list")]), A("[1] premier point"),
                  A('{"a": 1}'), A("Il a écrit [tool_input] au milieu."), A("L'Appel de l'outil est fini."),
                  A("Réponse : {\"x\": 2} voilà")]
        k, ship, tools = _kernel(script, with_tools=with_tools)
        turns = []
        for text in ("a", "b", "c", "d", "e", "f"):
            t = k.run_turn(TurnInput("t1", text))
            turns.append({"reply": t.reply, "messages": [m.to_openai() for m in t.messages],
                          "verdicts": [(v.gate.value, v.decision.value, v.rule) for v in t.verdicts],
                          "calls": len(t.calls), "failure": t.failure, "error": t.error})
        blob.append({"requests": ship.requests, "turns": turns, "called": tools.called})
    return hashlib.sha256(json.dumps(blob, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def test_empreinte_ordinaire():
    assert _hash() == EMPREINTE_ORDINAIRE


@pytest.mark.parametrize("bad,kind", [(CASE1, "tool_input"), (CASE2, "appel_outil"), ("  \n" + CASE1 + " suite", "tool_input")])
def test_text_toolcall_is_retried_once_without_entering_history(bad, kind):
    obs = Obs()
    k, ship, tools = _kernel([A(bad), A("Voilà.")], observer=obs)
    turn = k.run_turn(TurnInput("t1", "liste"))
    assert turn.reply == "Voilà." and tools.called == [] and turn.failure is None
    assert len(ship.requests) == 2 and ship.requests[0] == ship.requests[1]  # même corps
    assert not any(bad.strip() in (m.content or "") for m in turn.messages)
    assert obs.events == [{"kind": kind}] and "ls" not in json.dumps(obs.events)
    [v] = [v for v in turn.verdicts if v.rule == "loop:text_toolcall"]
    assert v.gate is Gate.SWITCH and v.decision is Decision.ALLOW and "ls" not in v.reason and "tool_input" not in v.reason
    assert len(turn.calls) == 2  # la relance est un appel de plus


def test_second_occurrence_in_the_turn_is_treated_normally():
    obs = Obs()
    k, ship, _ = _kernel([A(CASE1), A(CASE2)], observer=obs)
    turn = k.run_turn(TurnInput("t1", "liste"))
    assert turn.reply == CASE2 and len(ship.requests) == 2 and obs.events == [{"kind": "tool_input"}]


def test_the_retry_counts_in_max_calls():
    """Avec 1 appel permis, la relance consomme la limite : le tour passe à la conclusion SANS outils (pas d'appel d'outil de plus)."""
    k, ship, _ = _kernel([A(CASE1), A("Conclusion.")], max_calls=1)
    turn = k.run_turn(TurnInput("t1", "liste"))
    assert turn.reply == "Conclusion." and len(ship.requests) == 2
    assert ship.requests[0]["tools"] and ship.requests[1]["tools"] == []  # 2e appel = conclusion forcée, sans outils
    assert any(v.rule == "loop:max_calls" for v in turn.verdicts)


@pytest.mark.parametrize("text", ["[Note] voici le résultat", "[1] premier point", "Il a écrit [tool_input] dans sa phrase.",
                                  "Voir l'Appel de l'outil plus haut.", 'Voici : {"command":"ls"} comme demandé.',
                                  '{"command": "ls"}', "[Appel de l'outil] sans deux-points", "x [tool_input]"])
def test_legitimate_replies_are_not_retried(text):
    obs = Obs()
    k, ship, _ = _kernel([A(text)], observer=obs)
    turn = k.run_turn(TurnInput("t1", "liste"))
    assert turn.reply == text and len(ship.requests) == 1 and obs.events == []


def test_turn_without_tools_is_not_retried():
    obs = Obs()
    k, ship, _ = _kernel([A(CASE1)], with_tools=False, observer=obs)
    turn = k.run_turn(TurnInput("t1", "liste"))
    assert turn.reply == CASE1 and len(ship.requests) == 1 and obs.events == []


def test_real_tool_calls_are_unchanged():
    obs = Obs()
    k, ship, tools = _kernel([A("", tool_calls=[tc("calendar_list")]), A("Fait.")], observer=obs)
    turn = k.run_turn(TurnInput("t1", "agenda"))
    assert tools.called == ["calendar_list"] and turn.reply == "Fait." and obs.events == []


def test_a_reply_cut_by_length_is_not_concerned():
    class Cut(Rec):
        def complete(self, messages, tools, **kw):
            comp = super().complete(messages, tools, **kw)
            comp.finish_reason = "length" if len(self.requests) == 1 else "stop"
            return comp

    obs = Obs()
    inv = Inv()
    ship = Cut([A(CASE1), A("suite")])
    k = Kernel(store=StoreFake(), context=Ctx2(True), memory=Mem(), invariants=inv, gates=GatesFake(inv), escalator=Esc(),
               policy=Pol(), providers={Tier.SHIP: ship, Tier.DEEP: ScriptedProvider([])}, tools=Tools(),
               ledger=LedgerFake(), observer=obs)
    turn = k.run_turn(TurnInput("t1", "x"))
    assert obs.events == [] and not any(v.rule == "loop:text_toolcall" for v in turn.verdicts)
    assert any(m.content == CASE1 for m in turn.messages)  # chemin `length` d'avant : le content coupé est gardé
