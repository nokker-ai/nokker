"""Détecteur inter-tours de cartes identiques sur la chaîne APPROVAL_CONTINUE (cas lokkal-test1, 12.11 : 85 « approuver
une fois » de la même action). Seuil 3 ; côté personne seulement : le modèle ne reçoit aucun texte nouveau."""
from __future__ import annotations

import dataclasses

from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.approval_repeat import APPROVAL_REPEAT_REPLY
from nokker.core.approval_repeat import APPROVAL_REPEAT_SKIPPED
from nokker.core.loop import Kernel
from nokker.core.turn import Message, Tier, ToolCall, TurnInput
from tests.unit.test_loop_contract import Ctx, Esc, GatesFake, Inv, LedgerFake, Mem, Pol, StoreFake, Tools


class Provider:
    """Le modèle rappelle toujours le même outil ; garde chaque envoi pour inspecter ce qu'il voit."""
    model = "t"

    def __init__(self, calls):
        self.calls, self.sent, self.i, self.step = calls, [], 0, 0

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        from nokker.core.ports import Completion
        from nokker.core.turn import Usage
        self.sent.append(messages)
        args = self.calls[min(self.step, len(self.calls) - 1)]
        self.i += 1
        msg = Message("assistant", tool_calls=[ToolCall(id=f"c{self.i}", name="infra_lvextend", arguments=args)])
        return Completion(message=msg, usage=Usage(prompt=1), model="t", latency_s=0.0)


class ResultTools(Tools):
    def __init__(self, results):
        super().__init__()
        self.results = results

    def call(self, n, a, ctx):
        from nokker.core.turn import ToolResult
        self.called.append(n)
        return ToolResult(call_id="", name=n, content=self.results[min(len(self.called) - 1, len(self.results) - 1)])


def build(calls, results=("ok",)):
    prov, backend, tools = Provider(calls), InMemoryApprovals(), ResultTools(list(results))
    inv = Inv()
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=GatesFake(inv), escalator=Esc(),
               policy=Pol(), providers={Tier.SHIP: prov, Tier.DEEP: prov}, tools=tools, ledger=LedgerFake(),
               approvals=backend)
    return k, ApprovalExecutor(kernel=k, approvals=backend), backend, prov


def approve_and_run(k, ex, backend, turn):
    aid = turn.approval_ids[-1]
    k.providers[Tier.SHIP].step += 1  # le modèle change d'avis (ou non) au tour suivant
    a = dataclasses.replace(backend.get(aid), status="approved", decided_by="alice", decided_via="signature")
    backend._store_approval(a)
    return ex.run(a)


def chain(k, ex, backend, n, first="go"):
    turn = k.run_turn(TurnInput("t1", first))
    out = [turn]
    for _ in range(n):
        if not turn.approval_ids or turn.approval_ids[-1] in [t.approval_ids[-1] for t in out[:-1] if t.approval_ids]:
            break
        turn = approve_and_run(k, ex, backend, turn)
        out.append(turn)
    return out


def test_fourth_identical_card_is_not_posed_and_turn_concludes():
    k, ex, backend, prov = build([{"size": "1"}])
    turns = chain(k, ex, backend, 3)
    # tours : initial (carte 1), suites après exécutions 1, 2 (cartes 2, 3), après exécution 3 (4e carte refusée)
    assert [bool(t.approval_ids) for t in turns] == [True, True, True, False]
    last = turns[-1]
    assert last.reply == APPROVAL_REPEAT_REPLY.format(name="infra_lvextend", n=3, next=4)


def test_a_different_action_resets_the_counter():
    k, ex, backend, prov = build([{"size": "1"}, {"size": "1"}, {"size": "2"}, {"size": "1"}, {"size": "1"}, {"size": "1"}])
    turns = chain(k, ex, backend, 5)
    assert all(t.approval_ids for t in turns)  # 6 cartes posées : la chaîne repart à la 3e (args différents)


def test_same_args_but_different_result_does_not_count():
    k, ex, backend, prov = build([{"size": "1"}], results=("a", "b", "c", "d", "e"))
    turns = chain(k, ex, backend, 5)
    assert all(t.approval_ids for t in turns)


def test_a_person_message_resets_the_counter():
    k, ex, backend, prov = build([{"size": "1"}])
    turn = k.run_turn(TurnInput("t1", "go"))
    for _ in range(2):
        turn = approve_and_run(k, ex, backend, turn)
    assert k.approval_repeat._state["t1"][1] == 2
    turn = k.run_turn(TurnInput("t1", "encore"))  # vrai message de la personne
    assert "t1" not in k.approval_repeat._state and turn.approval_ids
    for _ in range(2):
        turn = approve_and_run(k, ex, backend, turn)
    assert turn.approval_ids  # 3e carte posée : compteur reparti de zéro


def test_the_model_sees_no_new_text():
    k, ex, backend, prov = build([{"size": "1"}])
    turns = chain(k, ex, backend, 3)
    final = turns[-1].messages
    tool_msgs = [m for m in final if m.role == "tool"]
    assert tool_msgs and tool_msgs[-1].content == APPROVAL_REPEAT_SKIPPED.format(n=3)
    assert not any(m.role == "user" for m in final)  # ni note système ni message ajouté
    assert "Je m'arrête" not in "".join(str(m) for m in prov.sent[-1])


def test_tool_name_is_made_safe_for_the_sentence():
    from nokker.core.approval_repeat import safe_name
    assert safe_name("mcp__x«évil»\nname") == "mcp__xévil name"
    assert len(safe_name("a" * 200)) == 60 and safe_name("a" * 200).endswith("…")


def test_frozen_texts_fingerprint():
    """Textes gelés par la relecture des textes (15.11.2026) : toute dérive casse la CI et repasse par la relecture."""
    import hashlib

    from nokker.core.approval_repeat import APPROVAL_REPEAT_REPLY

    def fp(s: str) -> str:
        return hashlib.sha256(s.encode()).hexdigest()[:16]

    assert fp(APPROVAL_REPEAT_REPLY) == "d3fc8a9250d92173"
    assert fp(APPROVAL_REPEAT_SKIPPED) == "15cc185fb0c53200"
