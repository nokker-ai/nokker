"""Carte tranchée PENDANT le tour (`Kernel.approval_wait_s` = réglage d'hôte `local.approval_wait_s`).

Constat « avant » : la décision arrive pendant le tour, mais l'exécution est mise en file derrière le verrou du fil ; la
réponse du MÊME tour dit « la commande attend ton approbation » alors que la carte est déjà tranchée, et l'outil ne tourne
qu'ensuite. Maintenant : le tour attend la décision (au plus `approval_wait_s` secondes), exécute l'outil lui-même sous le
verrou déjà tenu, et le modèle voit le résultat (ou le refus) dans CE tour. Vrai noyau, vraie boucle, faux fournisseur."""
from __future__ import annotations

import threading
import time

import pytest

from nokker.approvals.errors import ApprovalNotPending
from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.ports import Completion, ToolSpec
from nokker.core.refusal import is_refusal
from nokker.core.turn import Effect, Message, ToolCall, ToolResult, TurnInput, Usage
from tests.unit.test_loop_approvals import make_kernel, tc

WAIT_WORDS = "la commande attend ton approbation"


class Model:
    """Répond d'après ce que le dernier résultat d'outil dit : « en attente » s'il l'est, sinon le résultat ou le refus."""

    def __init__(self, first, hook=None):
        self.first, self.hook, self.n, self.seen = first, hook, 0, []

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.n += 1
        self.seen.append([dict(m) for m in messages])
        if self.n == 1:
            return Completion(message=self.first, usage=Usage(prompt=10), model="m", latency_s=0.0)
        if self.hook is not None and self.n == 2:
            self.hook()  # la carte est tranchée ENTRE le résultat « en attente » et la réponse finale
        texts = [m["content"] for m in messages if m["role"] in ("tool", "user")][1:]
        pending = [t for t in texts if t.startswith("EN ATTENTE D'APPROBATION")]
        blocks = [t for t in texts if "[Approbation]" in t]
        if blocks:
            reply = "Fait, d'après la décision : " + blocks[-1].split("Résultat : ")[1].splitlines()[0]
        elif any(t.startswith("Refusé par la personne") for t in texts):
            reply = "D'accord, je continue sans cette action."
        elif pending and not any(not t.startswith("EN ATTENTE") and "agrandi" in t for t in texts):
            reply = f"Pour l'instant {WAIT_WORDS}."
        else:
            reply = "Fait : " + next(t for t in texts if "agrandi" in t)
        return Completion(message=Message("assistant", reply), usage=Usage(prompt=10), model="m", latency_s=0.0)


def _setup(first, wait_s=3.0, hook=None):
    backend = InMemoryApprovals()
    k, tools = make_kernel([], approvals=backend)

    def call(n, a, ctx):
        tools.called.append(n)
        return ToolResult(call_id="", name=n, content="Volume agrandi à 150G.")

    tools.call = call
    k.providers = {next(iter(k.providers)): Model(first, hook)}
    executor = ApprovalExecutor(kernel=k, approvals=backend)
    k.approval_inline, k.approval_wait_s = executor.settle_in_turn, wait_s
    return k, tools, backend, executor


def _run(k, text="agrandis le volume"):
    box: dict = {}
    t = threading.Thread(target=lambda: box.setdefault("turn", k.run_turn(TurnInput("fil", text, sender="alice"))),
                         daemon=True)
    t.start()
    return t, box


def _await_card(backend, n=1, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        cards = backend.pending()
        if len(cards) >= n:
            return cards
        time.sleep(0.01)
    raise AssertionError("aucune carte posée")


def _tool_texts(turn):
    return [m.content for m in turn.messages if m.role == "tool"]


FIRST = Message("assistant", tool_calls=[tc("infra_lvextend", size="150G")])


def test_approved_during_the_wait_runs_the_tool_once_and_the_same_turn_sees_the_result():
    k, tools, backend, executor = _setup(FIRST)
    t, box = _run(k)
    card = _await_card(backend)[0]
    decided = backend.decide_local(card.id, "approve", "alice")
    finished = executor.hand_off(card.id)  # ce que fait `schedule_decision` : le tour qui attend prend la décision
    t.join(10)
    turn = box["turn"]
    assert tools.called == ["infra_lvextend"]  # une seule exécution
    assert _tool_texts(turn) == ["Volume agrandi à 150G."]  # le VRAI résultat, pas « en attente »
    assert WAIT_WORDS not in turn.reply and "agrandi" in turn.reply
    assert backend.get(card.id).status == "executed" and turn.pending_approvals == []
    assert finished is not None and finished.is_set()
    # le mécanisme de suite habituel, rejoué sur la même décision, n'exécute rien de plus
    assert executor.run(decided) is None and tools.called == ["infra_lvextend"]
    assert executor.hand_off(card.id) is None  # plus personne n'attend


def test_declined_during_the_wait_is_returned_as_the_tool_result_and_the_turn_continues():
    k, tools, backend, executor = _setup(FIRST)
    t, box = _run(k)
    card = _await_card(backend)[0]
    decided = backend.decide_local(card.id, "reject", "alice")
    t.join(10)
    turn = box["turn"]
    assert tools.called == []
    [text] = _tool_texts(turn)
    assert text.startswith("Refusé par la personne (alice)") and is_refusal(text) and f"[approval:{card.id}]" in text
    assert turn.reply == "D'accord, je continue sans cette action." and WAIT_WORDS not in turn.reply
    assert executor.run(decided) is None  # pas de tour de suite en double
    assert tools.called == []


def test_no_decision_in_the_window_keeps_the_current_behaviour():
    k, tools, backend, executor = _setup(FIRST, wait_s=0.4)
    t0 = time.monotonic()
    turn = k.run_turn(TurnInput("fil", "agrandis le volume", sender="alice"))
    assert 0.35 <= time.monotonic() - t0 < 3.0
    assert tools.called == [] and WAIT_WORDS in turn.reply
    [text] = _tool_texts(turn)
    assert text.startswith("EN ATTENTE D'APPROBATION HUMAINE")
    card = backend.pending()[0]
    assert executor.hand_off(card.id) is None  # le tour a cessé d'attendre : la suite habituelle prend la décision
    decided = backend.decide_local(card.id, "approve", "alice")
    follow = executor.run(decided)
    assert tools.called == ["infra_lvextend"] and "[Approbation]" in follow.input.text


def test_zero_wait_is_the_unchanged_behaviour():
    k, tools, backend, _executor = _setup(FIRST, wait_s=0.0)
    t0 = time.monotonic()
    turn = k.run_turn(TurnInput("fil", "agrandis le volume", sender="alice"))
    assert time.monotonic() - t0 < 0.5 and tools.called == [] and WAIT_WORDS in turn.reply
    assert backend.pending() and turn.pending_approvals


def test_no_hook_wired_is_the_unchanged_behaviour():
    k, _tools, _backend, _executor = _setup(FIRST, wait_s=5.0)
    k.approval_inline = None
    t0 = time.monotonic()
    turn = k.run_turn(TurnInput("fil", "agrandis le volume", sender="alice"))
    assert time.monotonic() - t0 < 0.5 and WAIT_WORDS in turn.reply


def test_cancel_during_the_wait_ends_the_wait_and_runs_nothing():
    k, tools, backend, _executor = _setup(FIRST, wait_s=20.0)
    t, box = _run(k)
    card = _await_card(backend)[0]
    t0 = time.monotonic()
    k.cancel("fil")
    t.join(5)
    assert not t.is_alive() and time.monotonic() - t0 < 3.0  # bien avant les 20 s
    assert tools.called == [] and box["turn"].error == "cancelled"
    assert backend.get(card.id).status == "pending"


def test_second_decision_is_refused_and_nothing_runs_twice():
    k, tools, backend, _executor = _setup(FIRST)
    t, _box = _run(k)
    card = _await_card(backend)[0]
    backend.decide_local(card.id, "approve", "alice")
    with pytest.raises(ApprovalNotPending):
        backend.decide_local(card.id, "approve", "alice")
    t.join(10)
    assert tools.called == ["infra_lvextend"]


def test_two_cards_in_one_turn_are_each_settled_in_the_turn():
    k, tools, backend, _executor = _setup(Message("assistant", tool_calls=[
        ToolCall(id="c1", name="infra_lvextend", arguments={"size": "150G"}),
        ToolCall(id="c2", name="infra_lvshrink", arguments={"size": "10G"})]))
    tools.specs["infra_lvshrink"] = ToolSpec("infra_lvshrink", "lvm", {}, Effect.DESTRUCTIVE)
    t, box = _run(k)
    first = _await_card(backend)[0]
    backend.decide_local(first.id, "approve", "alice")
    second = next(c for c in _await_card(backend) if c.id != first.id)
    backend.decide_local(second.id, "reject", "alice")
    t.join(10)
    turn = box["turn"]
    assert tools.called == ["infra_lvextend"]
    texts = _tool_texts(turn)
    assert texts[0] == "Volume agrandi à 150G." and texts[1].startswith("Refusé par la personne")
    assert WAIT_WORDS not in turn.reply


def test_card_decided_just_before_the_final_reply_is_taken_instead_of_publishing_pending():
    """Filet : la carte est tranchée après la fin de l'attente mais avant la réponse finale « en attente »."""
    box: dict = {}

    def decide_now():
        card = box["backend"].pending()[0]
        box["backend"].decide_local(card.id, "approve", "alice")

    k, tools, backend, _executor = _setup(FIRST, wait_s=0.2, hook=decide_now)
    box["backend"] = backend
    turn = k.run_turn(TurnInput("fil", "agrandis le volume", sender="alice"))
    assert tools.called == ["infra_lvextend"]  # une seule exécution
    assert WAIT_WORDS not in turn.reply and turn.reply.startswith("Fait")
    assert backend.get(turn.approval_ids[0]).status == "executed"
    assert any("[Approbation]" in m.content for m in turn.messages if m.role == "user")
