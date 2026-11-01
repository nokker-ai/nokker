"""Pause / reprise et steer d'un tour (nokker/core/controls.py, points de contrôle de nokker/core/loop.py).

Fakes de test_loop_contract.py ; le tour tourne dans un fil d'exécution à part, le test pilote la pause depuis le sien
et lit les événements par un observateur minimal."""
from __future__ import annotations

import threading

import pytest

from nokker.core.controls import ANY_ACCOUNT, TurnControls
from nokker.core.loop import RESUME_NOTE, STEER_BLOCK, Kernel
from nokker.core.ports import Completion
from nokker.core.turn import Message, Tier, ToolResult, TurnInput, Usage
from nokker.tenancy.context import INTERNAL_TENANT
from tests.unit.test_loop_contract import (
    Ctx,
    Esc,
    GatesFake,
    Inv,
    LedgerFake,
    Mem,
    Pol,
    ScriptedProvider,
    StoreFake,
    Tools,
    tc,
)

WAIT = 5.0


class Obs:
    """Observateur : garde les événements et lève un `threading.Event` par nom."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self.seen: dict[str, threading.Event] = {}

    def _ev(self, name):
        return self.seen.setdefault(name, threading.Event())

    def _rec(self, name, data):
        self.events.append((name, data))
        self._ev(name).set()

    def context_for(self, thread_id):
        return []

    def turn_started(self, turn):
        pass

    def tool_call(self, turn, call):
        pass

    def tool_result(self, turn, call, result, ms):
        pass

    def tool_executed(self, turn, call, spec, result):
        return None

    def turn_done(self, turn):
        self._rec("done", {})

    def turn_paused(self, turn, data):
        self._rec("paused", data)

    def turn_resumed(self, turn, data):
        self._rec("resumed", data)

    def turn_steered(self, turn, data):
        self._rec("steered", data)

    def turn_steer_dropped(self, turn, data):
        self._rec("dropped", data)

    def names(self):
        return [n for n, _ in self.events]


def kernel(script, *, steer=False, tools=None, provider=None):
    inv = Inv()
    tools = tools or Tools()
    ship = provider or ScriptedProvider(script)
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=GatesFake(inv), escalator=Esc(),
               policy=Pol(), providers={Tier.SHIP: ship, Tier.DEEP: ScriptedProvider([])}, tools=tools,
               ledger=LedgerFake(), observer=Obs(), steer_enabled=steer)
    return k, tools, ship


def run_bg(k, inp):
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("turn", k.run_turn(inp)), daemon=True)
    th.start()
    return th, out


def finish(th, out):
    th.join(WAIT)
    assert not th.is_alive(), "le tour ne finit pas"
    return out["turn"]


def flat(messages):
    return [(m["role"], m.get("content") or "") for m in messages]


class Hooked(Tools):
    """Outil dont l'exécution appelle `hook(name, args)` (pause, steer… posés PENDANT un outil)."""

    def __init__(self, hook):
        super().__init__()
        self.hook = hook

    def call(self, n, a, ctx):
        r = super().call(n, a, ctx)
        self.hook(n, a)
        return r


# ─────────────── pause ───────────────

def test_pause_before_the_model_call_holds_the_turn_until_resume():
    k, _, ship = kernel([Message("assistant", "fait")])
    k.pause("t1")  # fil en pause sans tour : le prochain tour s'arrête à son premier point de contrôle
    th, out = run_bg(k, TurnInput("t1", "salut"))
    assert k.observer.seen.setdefault("paused", threading.Event()).wait(WAIT)
    assert ship.seen == [] and k.controls.running("t1")  # aucun appel modèle parti
    assert k.observer.events[0] == ("paused", {"at": "model"})
    assert k.resume("t1") is True
    turn = finish(th, out)
    assert turn.reply == "fait" and turn.error is None and len(ship.seen) == 1
    assert ("resumed", "model") in [(n, d.get("at")) for n, d in k.observer.events]
    assert any(v.rule == "loop:paused" for v in turn.verdicts)
    assert not k.controls.running("t1")


def test_pause_before_a_tool_stops_between_two_calls_and_resumes_at_the_same_point():
    k = None

    def hook(n, a):
        if a.get("i") == 1:
            k.pause("t1")  # pendant le premier outil : il finit, le second attend

    tools = Hooked(hook)
    k, _, ship = kernel([Message("assistant", tool_calls=[tc("calendar_list", i=1), tc("calendar_list", i=2)]),
                         Message("assistant", "fini")], tools=tools)
    th, out = run_bg(k, TurnInput("t1", "agenda"))
    assert k.observer.seen.setdefault("paused", threading.Event()).wait(WAIT)
    assert tools.called == ["calendar_list"]  # le premier a fini, le second n'est pas parti
    assert ("paused", {"at": "tool", "tool": "calendar_list"}) in k.observer.events
    assert len(ship.seen) == 1
    k.resume("t1")
    turn = finish(th, out)
    assert tools.called == ["calendar_list", "calendar_list"] and turn.reply == "fini"
    # état gardé : les deux résultats suivent l'appel du modèle, dans l'ordre ; rien n'est ajouté (steer éteint)
    assert [m.role for m in turn.messages] == ["assistant", "tool", "tool", "assistant"]
    assert all(RESUME_NOTE not in (m.get("content") or "") for m in ship.seen[1])


def test_resume_note_is_seen_by_the_model_only_with_steer_enabled():
    k = None

    def hook(n, a):
        k.pause("t1")

    k, _, ship = kernel([Message("assistant", tool_calls=[tc("calendar_list")]), Message("assistant", "ok")],
                        steer=True, tools=Hooked(hook))
    th, out = run_bg(k, TurnInput("t1", "agenda"))
    # pause posée pendant l'outil : le tour s'arrête avant l'appel modèle suivant
    assert k.observer.seen.setdefault("paused", threading.Event()).wait(WAIT)
    assert ("paused", {"at": "model"}) in k.observer.events
    k.resume("t1")
    turn = finish(th, out)
    second = flat(ship.seen[1])
    assert second[-1] == ("user", RESUME_NOTE) and second[-2][0] == "tool"
    assert turn.messages[-2].content == RESUME_NOTE  # gardée dans l'historique : rejouée telle quelle


def test_cancel_wakes_a_paused_turn():
    k, _tools, ship = kernel([Message("assistant", "jamais")])
    k.pause("t1")
    th, out = run_bg(k, TurnInput("t1", "x"))
    assert k.observer.seen.setdefault("paused", threading.Event()).wait(WAIT)
    k.cancel("t1")
    turn = finish(th, out)
    assert turn.error == "cancelled" and ship.seen == []
    assert "resumed" not in k.observer.names()
    assert k.controls.thread_paused("t1")  # la pause du fil reste posée : c'est l'utilisateur qui la lève


def test_account_pause_holds_only_that_account_and_that_tenant():
    c = TurnControls()
    c.pause_account(INTERNAL_TENANT, "alice")
    assert c.is_paused("t1", INTERNAL_TENANT, "alice") and not c.is_paused("t1", INTERNAL_TENANT, "bruno")
    c.pause_account("tn_aaaaaaaaaaaa", ANY_ACCOUNT)
    assert c.is_paused("tn_aaaaaaaaaaaa:s", "tn_aaaaaaaaaaaa", "us_x")
    assert not c.is_paused("tn_bbbbbbbbbbbb:s", "tn_bbbbbbbbbbbb", "us_y")
    c.pause("t9")
    assert c.is_paused("t9/sa-abc", INTERNAL_TENANT, None)  # un sous-agent suit son fil racine
    assert c.resume_account(INTERNAL_TENANT, "alice") and not c.resume_account(INTERNAL_TENANT, "alice")


def test_global_pause_stops_the_account_turns_and_lets_others_run():
    k, _, _ship = kernel([Message("assistant", "bruno ok"), Message("assistant", "alice ok")])
    k.pause_account(INTERNAL_TENANT, "alice")
    th, out = run_bg(k, TurnInput("ta", "x", sender="alice"))
    assert k.observer.seen.setdefault("paused", threading.Event()).wait(WAIT)
    other = k.run_turn(TurnInput("tb", "y", sender="bruno"))  # un autre compte n'est pas touché
    assert other.reply == "bruno ok"
    assert k.controls.running("ta")
    k.resume("ta")  # reprendre le fil ne lève pas la pause du compte
    assert k.controls.is_paused("ta", INTERNAL_TENANT, "alice")
    k.resume_account(INTERNAL_TENANT, "alice")
    assert finish(th, out).reply == "alice ok"


# ─────────────── steer ───────────────

def test_steer_is_consumed_after_the_whole_tool_batch_before_the_next_model_call():
    k = None
    ids = []

    def hook(n, a):
        if a.get("i") == 1:
            ids.append(k.steer("t1", "plutôt demain, et hunter2"))

    k, tools, ship = kernel([Message("assistant", tool_calls=[tc("calendar_list", i=1), tc("calendar_list", i=2)]),
                             Message("assistant", "demain : rien")], steer=True, tools=Hooked(hook))
    turn = k.run_turn(TurnInput("t1", "agenda"))
    assert tools.called == ["calendar_list", "calendar_list"] and turn.reply == "demain : rien"
    block = STEER_BLOCK.format(text="plutôt demain, et [secret:pw]")  # caviardé comme un message
    assert all(c != block for _, c in flat(ship.seen[0]))
    second = flat(ship.seen[1])
    assert [r for r, _ in second[-4:]] == ["assistant", "tool", "tool", "user"] and second[-1][1] == block
    assert ("steered", {"steer_id": ids[0].id}) in k.observer.events
    assert [m.role for m in turn.messages] == ["assistant", "tool", "tool", "user", "assistant"]


def test_steer_while_paused_enters_at_resume_after_the_resume_note():
    k, _, ship = kernel([Message("assistant", tool_calls=[tc("calendar_list")]), Message("assistant", "ok")],
                        steer=True)
    k_hook = []

    def hook(n, a):
        k.pause("t1")
        k_hook.append(1)

    k.tools = Hooked(hook)
    th, out = run_bg(k, TurnInput("t1", "agenda"))
    assert k.observer.seen.setdefault("paused", threading.Event()).wait(WAIT)
    assert k.steer("t1", "ajoute la météo") is not None
    k.resume("t1")
    finish(th, out)
    tail = flat(ship.seen[1])[-2:]
    assert tail == [("user", RESUME_NOTE), ("user", STEER_BLOCK.format(text="ajoute la météo"))]


def test_steer_without_a_running_turn_or_disabled_is_refused():
    k, _, _ = kernel([Message("assistant", "ok")], steer=True)
    assert k.steer("t1", "hop") is None  # aucun tour : refusé (la route répond 409)
    k2, _tools, ship = kernel([Message("assistant", tool_calls=[tc("calendar_list")]), Message("assistant", "ok")])
    got = []
    k2.tools = Hooked(lambda n, a: got.append(k2.steer("t1", "hop")))
    turn = k2.run_turn(TurnInput("t1", "x"))
    assert got == [None]  # steer éteint : refusé même pendant un tour, rien n'entre dans le contexte
    assert all("[Message de l'utilisateur" not in c for _, c in flat(ship.seen[1]))
    assert turn.reply == "ok"


def test_steer_arriving_during_the_final_answer_is_dropped_not_lost():
    k = None

    class Final(ScriptedProvider):
        def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
            self.seen.append(messages)
            self.steer = k.steer("t1", "trop tard")  # le modèle écrit sa réponse finale
            return Completion(message=Message("assistant", "voilà"), usage=Usage(prompt=1), model="m", latency_s=0.0)

    prov = Final([])
    k, _, _ = kernel([], steer=True, provider=prov)
    turn = k.run_turn(TurnInput("t1", "x"))
    assert turn.reply == "voilà" and prov.steer is not None
    assert ("dropped", {"steer_id": prov.steer.id, "text": "trop tard"}) in k.observer.events
    assert k.controls.take_steers("t1") == [] and not k.controls.running("t1")


def test_controls_are_shared_by_kernel_copies():
    import dataclasses

    k, _, _ = kernel([])
    child = dataclasses.replace(k, max_calls=3)
    k.pause("t1")
    assert child.controls is k.controls and child.controls.thread_paused("t1/sa-x")


@pytest.mark.parametrize("flag", [False, True])
def test_no_pause_no_steer_leaves_the_model_context_unchanged(flag):
    k, _, ship = kernel([Message("assistant", tool_calls=[tc("calendar_list")]), Message("assistant", "ok")],
                        steer=flag)
    k.run_turn(TurnInput("t1", "agenda"))
    assert [r for r, _ in flat(ship.seen[1])] == ["system", "user", "assistant", "tool"]


def test_tool_result_kept_when_paused_mid_batch():
    """Le résultat d'un outil exécuté avant la pause est gardé tel quel (pas rejoué à la reprise)."""
    calls = []
    k = None

    class Counting(Tools):
        def call(self, n, a, ctx):
            calls.append(a.get("i"))
            if a.get("i") == 1:
                k.pause("t1")
            return ToolResult(call_id="", name=n, content=f"r{a.get('i')}")

    k, _, _ship = kernel([Message("assistant", tool_calls=[tc("calendar_list", i=1), tc("calendar_list", i=2)]),
                         Message("assistant", "ok")], tools=Counting())
    th, out = run_bg(k, TurnInput("t1", "x"))
    assert k.observer.seen.setdefault("paused", threading.Event()).wait(WAIT)
    k.resume("t1")
    turn = finish(th, out)
    assert calls == [1, 2] and [m.content for m in turn.messages if m.role == "tool"] == ["r1", "r2"]
