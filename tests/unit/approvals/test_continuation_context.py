"""Ticket T3 (2e repro, lokkal-test1 rc.9.28) : ce que voit le modèle au tour de suite d'une carte `terminal` dont la
commande échoue (returncode 1), avec le VRAI moteur de contexte, et l'ordre quand la décision arrive PENDANT le tour qui
a posé la carte (même file par fil que `api/server.py::schedule_decision`)."""
from __future__ import annotations

import dataclasses
import threading
from datetime import datetime

from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.context.engine import ZURICH, ContextEngineV0
from nokker.core.loop import PENDING_MARK, Kernel
from nokker.core.ports import Completion, ToolSpec
from nokker.core.turn import Effect, Message, Tier, ToolCall, ToolResult, TurnInput, Usage
from nokker.core.turn_lock import TurnLock
from nokker.qa.untrusted import fenced_result
from nokker.store.pg import MemoryStore
from tests.unit.test_loop_contract import Esc, GatesFake, Inv, LedgerFake, Mem, Pol, Tools

CMD = {"command": "pytest -q test_sub.py"}
PYTEST_OUT = "FAILED test_sub.py::test_sub_negatif - assert 1 == 2\n1 failed, 3 passed in 0.12s"
PENDING_FINAL = "Une carte d'approbation est sur ton écran : j'attends ta décision."


class TermTools(Tools):
    specs = {**Tools.specs, "terminal": ToolSpec("terminal", "shell", {}, Effect.DESTRUCTIVE)}

    def call(self, n, a, ctx):
        self.called.append(n)
        return ToolResult(call_id="", name=n, content=fenced_result({"returncode": 1}, "terminal:output", PYTEST_OUT))


class Model:
    """Appelle `terminal` tant qu'il n'a vu aucun résultat ; garde chaque liste de messages reçue."""
    model = "t"

    def __init__(self, hold_final=None):
        self.sent, self.hold_final, self.reached = [], hold_final, threading.Event()

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.sent.append(messages)
        last = messages[-1]
        if last["role"] == "tool" and PENDING_MARK in last["content"]:  # tour 2, après la carte
            self.reached.set()
            if self.hold_final is not None:
                assert self.hold_final.wait(10)
            msg = Message("assistant", content=PENDING_FINAL)
        elif last["role"] == "user" and "[Approbation]" in last["content"]:  # tour 3
            msg = Message("assistant", content="je corrige")
        else:
            msg = Message("assistant", tool_calls=[ToolCall(f"c{len(self.sent)}", "terminal", CMD)])
        return Completion(message=msg, usage=Usage(prompt=1), model="t", latency_s=0.0)


def build(tmp_path, model):
    prefix = tmp_path / "p.md"
    prefix.write_text("PRÉFIXE", encoding="utf-8")
    tools, backend = TermTools(), InMemoryApprovals()
    inv = Inv()
    k = Kernel(store=MemoryStore(), context=ContextEngineV0(tools=tools, prefix_path=str(prefix),
                                                             now_fn=lambda: datetime(2026, 11, 15, tzinfo=ZURICH)),
               memory=Mem(), invariants=inv, gates=GatesFake(inv), escalator=Esc(), policy=Pol(),
               providers={Tier.SHIP: model, Tier.DEEP: model}, tools=tools, ledger=LedgerFake(), approvals=backend)
    return k, ApprovalExecutor(kernel=k, approvals=backend), backend


def holder_aid(backend):
    return backend._list_pending_approvals(None)[0].id


def approve(backend, aid):
    a = dataclasses.replace(backend.get(aid), status="approved", decided_by="alice", decided_via="signature")
    backend._store_approval(a)
    return a


def test_continuation_message_carries_output_and_returncode(tmp_path):
    model = Model()
    k, ex, backend = build(tmp_path, model)
    t2 = k.run_turn(TurnInput("t", "écris sub et test_sub puis lance pytest"))
    assert t2.approval_ids and t2.reply == PENDING_FINAL
    t3 = ex.run(approve(backend, t2.approval_ids[-1]))
    msgs = model.sent[-1]  # ce que le modèle reçoit au tour de suite
    last = msgs[-1]
    assert last["role"] == "user" and last["content"].endswith(t3.input.text)
    for needle in ('"returncode": 1', PYTEST_OUT, "Commande : pytest -q test_sub.py", "[Approbation]",
                   "Cette action est tranchée et son résultat complet est ci-dessus : ne la relance pas"):
        assert needle in last["content"], needle
    # historique du tour 2, rejoué : appel -> tool EN ATTENTE -> [Approbation]
    # Motif : le texte de fin rédigé avant la décision n'est plus rejoué : il disait d'attendre une validation déjà donnée.
    roles = [(m["role"], (m.get("content") or "")[:30]) for m in msgs[1:]]
    assert roles[0][0] == "user" and roles[1][0] == "assistant" and roles[2][0] == "tool"
    assert roles[2][1].startswith(PENDING_MARK[:30])
    assert roles[3][0] == "user"
    assert PENDING_FINAL not in [m.get("content") for m in msgs]


def test_decision_arriving_during_turn_runs_after_it_with_complete_history(tmp_path):
    """Même file que `schedule_decision` : reserve() pendant que le tour 2 tient le verrou, puis acquire(ticket)."""
    release = threading.Event()
    model = Model(hold_final=release)
    k, ex, backend = build(tmp_path, model)
    lock, order = TurnLock(), []

    def turn2():
        lock.acquire()
        try:
            order.append("t2-start")
            holder["t"] = k.run_turn(TurnInput("t", "lance pytest"))
            order.append("t2-end")
        finally:
            lock.release()

    holder: dict = {}
    th2 = threading.Thread(target=turn2)
    th2.start()
    assert model.reached.wait(10)  # le tour 2 tourne encore (carte posée, texte final pas encore rendu)
    aid = holder_aid(backend)
    decided = approve(backend, aid)
    ticket = lock.reserve()
    assert not ticket.immediate  # « en file, après le tour en cours »

    def job():
        lock.acquire(ticket=ticket)
        try:
            order.append("exec-start")
            ex.run(decided)
        finally:
            lock.release()

    thj = threading.Thread(target=job)
    thj.start()
    assert backend.get(aid).status == "approved"  # pas encore exécutée
    assert order == ["t2-start"]  # rien n'a bougé tant que le tour 2 tient le fil
    release.set()
    th2.join(10)
    thj.join(10)
    assert order == ["t2-start", "t2-end", "exec-start"]
    msgs = model.sent[-1]
    # Motif : le texte de fin rédigé avant la décision n'est plus rejoué : il disait d'attendre une validation déjà donnée.
    assert not any(m["role"] == "assistant" and m.get("content") == PENDING_FINAL for m in msgs)
    assert any(m["role"] == "assistant" and m.get("tool_calls") for m in msgs)  # le reste de l'historique est là
    assert "[Approbation]" in msgs[-1]["content"] and '"returncode": 1' in msgs[-1]["content"]
