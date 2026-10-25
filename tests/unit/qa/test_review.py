"""X8 : relecture du diff avant « terminé » et avis sur les cartes fortes (`nokker/qa/review.py`, `core/loop.py`)."""
from __future__ import annotations

import json

from nokker.approvals.memory import InMemoryApprovals
from nokker.core.turn import InputCheck, Message, Tier, ToolCall, Turn, TurnInput
from nokker.qa.review import (
    CardAdvisor,
    DiffReviewer,
    format_advice,
    parse_advice,
    parse_advice_reply,
    parse_review,
    turn_diff,
)
from tests.unit.test_loop_approvals import make_kernel, tc


def write_turn():
    t = Turn(thread_id="t", index=0, input=InputCheck(text="corrige slugify"))
    call = ToolCall(id="p1", name="patch", arguments={"mode": "replace", "path": "s.py", "old_string": "a = 1",
                                                      "new_string": "a = 2"})
    bad = ToolCall(id="p2", name="write_file", arguments={"path": "x.py", "content": "boom"})
    t.messages = [Message(role="assistant", tool_calls=[call, bad]),
                  Message(role="tool", content=json.dumps({"diff": "..."}), tool_call_id="p1", name="patch"),
                  Message(role="tool", content=json.dumps({"error": "refusé"}), tool_call_id="p2", name="write_file")]
    return t


def test_turn_diff_keeps_successful_writes_only():
    d = turn_diff(write_turn())
    assert "-a = 1" in d and "+a = 2" in d and "x.py" not in d
    assert turn_diff(Turn(thread_id="t", index=0, input=InputCheck(text="salut"))) == ""


def test_parse_review():
    assert parse_review("OK") == (True, "")
    ok, remarks = parse_review("**À CORRIGER**\n- s.py : cas vide oublié")
    assert ok is False and "cas vide" in remarks
    assert parse_review("Je pense que…")[0] is None


def test_advice_format_round_trip():
    assert parse_advice(format_advice("high", "Supprime la base.")) == ("high", "Supprime la base.")
    assert parse_advice("avis d'avant X8") == (None, "avis d'avant X8")
    assert parse_advice_reply("RISQUE: élevé\nAVIS: efface tout le dossier.") == ("high", "efface tout le dossier.")
    assert parse_advice_reply("Risque : moyen.\nAvis : réversible par git.")[0] == "medium"


class Transport:
    def __init__(self, *contents, fail=False):
        self.contents, self.fail, self.bodies = list(contents), fail, []

    def post(self, body):
        self.bodies.append(body)
        if self.fail:
            raise RuntimeError("HTTP 503")
        return {"choices": [{"message": {"content": self.contents.pop(0)}}], "model": "juge-x",
                "usage": {"prompt_tokens": 500, "completion_tokens": 20}}


def code_kernel(script, reviewer=None, advisor=None, approvals=None):
    k, tools = make_kernel(script, approvals=approvals)
    from nokker.core.ports import ToolSpec
    from nokker.core.turn import Effect

    tools.specs["patch"] = ToolSpec("patch", "patch", {}, Effect.WRITE)
    k.reviewer, k.advisor = reviewer, advisor
    return k, tools


def patch_call(i=1):
    return Message("assistant", tool_calls=[tc("patch", mode="replace", path="s.py", old_string="a", new_string=f"b{i}")])


def test_review_sends_back_to_the_author_once_then_replies():
    rev = DiffReviewer(transport=Transport("À CORRIGER\n- s.py : le cas vide n'est pas traité"), tier=Tier.DEEP)
    k, tools = code_kernel([patch_call(1), Message("assistant", "terminé"), patch_call(2),
                            Message("assistant", "terminé, cas vide traité")], reviewer=rev)
    turn = k.run_turn(TurnInput("t1", "corrige slugify"))
    assert turn.reply == "terminé, cas vide traité"
    assert tools.called == ["patch", "patch"]
    notes = [m.content for m in turn.messages if m.role == "user" and m.content.startswith("[relecture]")]
    assert len(notes) == 1 and "cas vide" in notes[0]
    assert len(rev.transport.bodies) == 1  # une relecture par tour (rounds=1)
    review = [v for v in turn.verdicts if v.rule == "judge:review"]
    assert review and review[0].judge_model == "juge-x"
    judge_calls = [c for c in turn.calls if c.route_reason.startswith("juge :")]
    assert judge_calls and judge_calls[0].usage.prompt == 500 and judge_calls[0].tier is Tier.DEEP


def test_review_ok_or_judge_down_lets_the_reply_go():
    for rev in (DiffReviewer(transport=Transport("OK")), DiffReviewer(transport=Transport(fail=True))):
        k, tools = code_kernel([patch_call(), Message("assistant", "terminé")], reviewer=rev)
        turn = k.run_turn(TurnInput("t1", "corrige slugify"))
        assert turn.reply == "terminé" and tools.called == ["patch"] and not turn.error


def test_no_write_no_review():
    rev = DiffReviewer(transport=Transport("À CORRIGER\n- rien"))
    k, _ = code_kernel([Message("assistant", "bonjour")], reviewer=rev)
    assert k.run_turn(TurnInput("t1", "salut")).reply == "bonjour"
    assert rev.transport.bodies == []


def test_advice_on_strong_card_only():
    backend = InMemoryApprovals()
    adv = CardAdvisor(transport=Transport("RISQUE: élevé\nAVIS: agrandit un volume en production."))
    k, _ = code_kernel([Message("assistant", tool_calls=[tc("infra_lvextend", path="/srv", size="50G")]),
                            Message("assistant", "j'attends ta décision")], advisor=adv, approvals=backend)
    turn = k.run_turn(TurnInput("t1", "le disque est plein", sender="alice@example.ch"))
    card = backend.pending()[0]
    assert parse_advice(card.advice) == ("high", "agrandit un volume en production.")
    assert any(c.route_reason.startswith("juge : avis") for c in turn.calls)
    assert CardAdvisor(transport=Transport("x")).advise(turn, "patch", {}, "", "standard") is None


def test_advice_down_card_still_posed():
    backend = InMemoryApprovals()
    k, _ = code_kernel([Message("assistant", tool_calls=[tc("infra_lvextend", path="/srv")]),
                        Message("assistant", "j'attends")], advisor=CardAdvisor(transport=Transport(fail=True)),
                       approvals=backend)
    k.run_turn(TurnInput("t1", "le disque est plein", sender="alice@example.ch"))
    assert backend.pending()[0].advice is None


def test_approval_json_splits_advice_and_risk():
    from nokker.api.server import approval_to_json
    from nokker.core.ports import Approval

    a = Approval(id="a", thread_id="t", turn_index=0, channel="api", tool_name="terminal", arguments={},
                 args_sha256="x", strength="strong", rule="effect:destructive", advice=format_advice("medium", "réversible"))
    j = approval_to_json(a)
    assert j["advice"] == "réversible" and j["advice_risk"] == "medium"
