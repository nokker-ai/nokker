"""Carte posée par un sous-agent puis décidée APRÈS sa fin (premier plan et arrière-plan).

L'exécution différée (outil approuvé + tour de suite) se fait avec le noyau de l'ENFANT : son registre filtré, ses
règles. Jamais avec le noyau du parent (élévation : `clarify`, `send_*`… redeviendraient appelables). Enfant disparu
(annulé, redémarrage) : la carte est refusée proprement, rien n'est exécuté."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.loop import Kernel
from nokker.core.ports import Completion, ToolSpec
from nokker.core.turn import Effect, Message, Tier, ToolCall, ToolResult, Usage
from nokker.qa.permissions import LocalPermissions, Mode, Rule
from nokker.tools.delegate import DelegateToolSource
from nokker.tools.registry import Registry
from tests.unit.test_loop_contract import Ctx, Esc, GatesFake, Inv, LedgerFake, Mem, Pol, StoreFake
from tests.unit.tools.test_delegate import FakeCode, Factory, ctx, git_fake, turn


class Base:
    """Outils du parent : `clarify` et `send_mail` sont interdits à un enfant ; `infra_lvextend` pose une carte."""

    SPECS = {"infra_lvextend": Effect.DESTRUCTIVE, "clarify": Effect.READ, "send_mail": Effect.SEND_EXTERNAL,
             "calendar_list": Effect.READ}

    def __init__(self):
        self.calls = []

    def specs(self):
        return [ToolSpec(n, n, {"type": "object"}, e) for n, e in self.SPECS.items()]

    def call(self, name, args, c):
        self.calls.append((name, c.thread_id))
        if name == "clarify":
            return ToolResult(call_id="", name=name, content=f"Question: {args.get('question', '')}")
        return ToolResult(call_id="", name=name, content="ok")


class Model:
    """Premier tour de l'enfant : demande `infra_lvextend` (carte), puis attend. Tour de suite (bloc [Approbation]) :
    tente `clarify` (hors du registre de l'enfant : avec le noyau du parent, la question partirait)."""

    model = "scripted"

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        user = next(m["content"] for m in messages if m["role"] == "user")
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        if "[Approbation]" in user:
            msg = (Message("assistant", tool_calls=[ToolCall("c2", "clarify", {"question": "on y va ?"})])
                   if not tool_msgs else Message("assistant", "fait après la carte"))
        else:
            msg = (Message("assistant", tool_calls=[ToolCall("c1", "infra_lvextend", {"size": "50G"})])
                   if not tool_msgs else Message("assistant", "j'attends la carte"))
        return Completion(message=msg, usage=Usage(prompt=10), model="scripted", latency_s=0.0)


def world(background=False):
    inv, approvals, base = Inv(), InMemoryApprovals(), Base()
    parent = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=GatesFake(inv),
                    escalator=Esc(), policy=Pol(), providers={Tier.SHIP: Model()}, tools=None, ledger=LedgerFake(),
                    approvals=approvals)
    src = DelegateToolSource(lambda registry, tid, observer: dataclasses.replace(parent, tools=registry,
                                                                                 observer=observer),
                             None, background=background)
    parent.tools = Registry(sources=[base, src])
    src.bind(parent.tools)
    executor = ApprovalExecutor(kernel=parent, approvals=approvals)
    published = []
    executor.on_result = lambda a, content, is_error, ms: published.append((a.thread_id, content, is_error))
    return parent, approvals, base, src, executor, published


def approve(approvals):
    card = approvals.pending()[0]
    card.status, card.decided_by, card.decided_via = "approved", "alice", "test"
    return card


def child_card(background):
    parent, approvals, base, src, executor, published = world(background)
    args = {"tasks": [{"goal": "agrandis le volume"}], "isolate": False}
    if background:
        args["background"] = True
    r = src.call("delegate", args, ctx("t1"))
    assert not r.is_error, r.content
    if background:
        st = json.loads(src.call("delegate_status", {"wait_s": 5}, ctx("t1")).content)["children"][0]
    else:
        st = json.loads(r.content)[0]
    assert (st["status"], st["exit_reason"]) == ("interrupted", "approval_pending")  # l'enfant a fini, carte posée
    card = approve(approvals)
    assert card.thread_id == st["thread_id"] and base.calls == []
    return parent, approvals, base, src, executor, published, card, st


def _check_runs_as_the_child(background):
    parent, approvals, base, src, executor, published, card, st = child_card(background)
    executor.subagent_kernel = src.kernel_for_card
    cont = executor.run(card)
    child = st["thread_id"]
    assert ("infra_lvextend", child) in base.calls  # l'outil approuvé, par le registre de l'enfant
    assert not any(name == "clarify" for name, _ in base.calls)  # hors de son registre : jamais appelable
    # le noyau de l'enfant ne connaît pas `clarify` : la question n'est jamais posée (avec le parent, elle l'était)
    assert cont is not None and cont.thread_id == child and "Question" not in cont.reply
    assert "clarify" in cont.reply and "inconnu" in cont.reply
    assert approvals.get(card.id).status == "executed"
    return src, child, cont


def test_carte_d_enfant_au_premier_plan_suite_avec_le_registre_de_l_enfant():
    src, child, _ = _check_runs_as_the_child(background=False)
    assert src.kernel_for_card(child) is None


def test_carte_d_enfant_de_fond_suite_avec_le_registre_de_l_enfant_et_resultat_mis_a_jour():
    src, child, cont = _check_runs_as_the_child(background=True)
    st = json.loads(src.call("delegate_status", {}, ctx("t1")).content)["children"][0]
    assert (st["status"], st["exit_reason"], st["summary"]) == ("completed", "completed", cont.reply)
    assert src.kernel_for_card(child) is None  # repris : plus de noyau d'enfant en attente


def test_executeur_seul_ne_prend_jamais_le_noyau_du_parent_pour_un_enfant():
    for background in (False, True):
        parent, approvals, base, src, executor, published, card, st = child_card(background)
        assert executor.run(card) is None  # aucun résolveur de noyau d'enfant : refus, pas de repli sur le parent
        assert base.calls == [] and approvals.get(card.id).status == "failed"
        assert published and published[-1][0] == st["thread_id"] and published[-1][2] is True


def test_enfant_disparu_carte_refusee_proprement():
    parent, approvals, base, src, executor, published, card, st = child_card(background=True)
    executor.subagent_kernel = src.kernel_for_card
    src.cancel_children("t1")  # fil parent annulé : l'enfant en attente n'est plus reprenable
    assert src.kernel_for_card(st["thread_id"]) is None
    assert executor.run(card) is None
    a = approvals.get(card.id)
    assert a.status == "failed" and "sous-agent" in (a.error or "")
    assert base.calls == [] and published[-1][2] is True
    # redémarrage : une source neuve ne connaît aucun enfant
    parent, approvals, base, src, executor, published, card, st = child_card(background=False)
    executor.subagent_kernel = DelegateToolSource(Factory(), None, background=True).kernel_for_card
    assert executor.run(card) is None and base.calls == []


def test_refus_d_une_carte_d_enfant_sans_suite_par_le_parent():
    parent, approvals, base, src, executor, published, card, st = child_card(background=False)
    card.status = "rejected"
    executor.subagent_kernel = lambda tid: None
    assert executor.run(card) is None and base.calls == []


# ─────────────────────────────── cycle de vie d'un enfant en attente ───────────────────────────────


def test_enfant_en_attente_garde_son_worktree_et_sa_source(tmp_path):
    (tmp_path / ".git").mkdir()
    st = {}
    code = FakeCode(str(tmp_path))
    code.run_impl = git_fake(st)
    f = Factory(behave=lambda k, inp: turn("j'attends", pending_approvals=[ToolCall("1", "terminal", {})]))
    src = DelegateToolSource(f, code, background=True)
    src.bind(Registry(sources=[code, src]))
    res = json.loads(src.call("delegate", {"tasks": [{"goal": "g"}]}, ctx("t1")).content)[0]
    assert res["exit_reason"] == "approval_pending" and res["worktree"]["kept"]  # 0 commit mais pas supprimé
    assert not st.get("removed") and code.scopes and not code.scopes[0].closed
    assert src.kernel_for_card(res["thread_id"]) is f.kernels[0]
    f.observers[0].turn_done(turn("fini après la carte"))  # le tour de suite de l'enfant se termine
    assert code.scopes[0].closed and st.get("removed")  # 0 commit, propre : supprimé maintenant
    assert src.kernel_for_card(res["thread_id"]) is None


def test_un_enfant_herite_des_restrictions_du_depot_de_son_parent(tmp_path):
    settings = Path(tmp_path / "s.json")
    settings.write_text(json.dumps({"permissions": {"mode": "default", "deny": ["terminal(git push*)"],
                                                    "ask": ["write_file"]}}))
    perms = LocalPermissions(mode=Mode.AUTO)
    perms.restrict_for_thread("t1", settings)
    push = ToolCall("1", "terminal", {"command": "git push origin main"})
    for tid in ("t1", "t1/sa-abcd1234"):
        assert perms.decide(push, Effect.DESTRUCTIVE, tid)[0].value == "deny", tid
        assert perms.decide(ToolCall("2", "write_file", {"path": "a"}), Effect.WRITE, tid)[0].value == "hitl", tid
        assert perms.mode_for(tid) is Mode.DEFAULT, tid
    perms.allow_for_thread("t1", Rule.parse("calendar_list"))  # un « pour cette session » ne s'étend pas
    assert perms.decide(ToolCall("3", "calendar_list", {}), Effect.WRITE, "t1/sa-abcd1234")[0].value == "hitl"
