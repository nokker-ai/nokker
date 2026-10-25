"""Une conclusion forcée est relue (lot fix-batch-1, défaut 3 ; banc D1, tâche H03).

Un tour qui finit au plafond d'appels (`loop:max_calls`) ou par stagnation (`loop:stalled`) conclut sans outils
(`Kernel._conclude`) : le juge ne relisait pas ce diff. Maintenant il le relit une fois, SANS renvoi à l'auteur (plus
d'appel pour corriger), et ses remarques sont jointes à la réponse ; s'il ne peut pas relire, la réponse le dit.
"""
from __future__ import annotations

from nokker.core.turn import Decision, Message, TurnInput
from nokker.qa.review import DiffReviewer
from tests.unit.qa.test_review import Transport, code_kernel, patch_call


def forced_kernel(transport, *, max_calls=3, script=None, stall_limit=None):
    rev = DiffReviewer(transport=transport)
    script = script or [patch_call(i) for i in range(1, max_calls + 1)] + [Message("assistant", "Voilà où j'en suis.")]
    k, tools = code_kernel(script, reviewer=rev)
    k.max_calls, k.stall_limit = max_calls, stall_limit
    return k, tools, rev


def test_max_calls_conclusion_is_reviewed_and_remarks_are_visible_without_a_second_round():
    k, tools, rev = forced_kernel(Transport("À CORRIGER\n- s.py : le cas vide n'est pas traité"))
    turn = k.run_turn(TurnInput("t1", "corrige slugify"))
    assert any(v.rule == "loop:max_calls" for v in turn.verdicts)
    assert turn.reply.startswith("Voilà où j'en suis.")
    assert "cas vide" in turn.reply and "non corrigé" in turn.reply  # visible pour l'utilisateur
    assert len(rev.transport.bodies) == 1  # une relecture
    assert tools.called == ["patch"] * 3  # aucun renvoi à l'auteur après le plafond
    assert not any(m.role == "user" and (m.content or "").startswith("[relecture]") for m in turn.messages)
    review = [v for v in turn.verdicts if v.rule == "judge:review"]
    assert review and review[0].decision is Decision.REDO_DEEP
    assert "cas vide" in turn.messages[-1].content  # le tour suivant voit aussi les remarques


def test_conclusion_with_judge_down_says_the_diff_was_not_reviewed():
    k, _, _ = forced_kernel(Transport(fail=True))
    turn = k.run_turn(TurnInput("t1", "corrige slugify"))
    assert turn.reply.startswith("Voilà où j'en suis.") and "pas été relu" in turn.reply
    assert any(v.rule == "judge:review:skipped" for v in turn.verdicts)


def test_conclusion_reviewed_ok_is_unchanged():
    k, _, rev = forced_kernel(Transport("OK"))
    turn = k.run_turn(TurnInput("t1", "corrige slugify"))
    assert turn.reply == "Voilà où j'en suis." and len(rev.transport.bodies) == 1


def test_stalled_conclusion_is_reviewed_too():
    same = [patch_call(1)] * 4 + [Message("assistant", "Je bloque sur ce patch.")]
    k, _, rev = forced_kernel(Transport("À CORRIGER\n- s.py : le patch ne s'applique pas"), max_calls=8,
                              script=same, stall_limit=3)
    turn = k.run_turn(TurnInput("t1", "corrige slugify"))
    assert any(v.rule == "loop:stalled" for v in turn.verdicts)
    assert "ne s'applique pas" in turn.reply and len(rev.transport.bodies) == 1


def test_last_call_answer_with_no_room_left_is_reviewed_without_a_round():
    """Réponse finale au dernier appel : avant, « plus de place pour corriger » la laissait partir sans relecture."""
    k, tools, rev = forced_kernel(Transport("À CORRIGER\n- s.py : test supprimé"), max_calls=3,
                                  script=[patch_call(1), patch_call(2), Message("assistant", "terminé")])
    turn = k.run_turn(TurnInput("t1", "corrige slugify"))
    assert turn.reply.startswith("terminé") and "test supprimé" in turn.reply
    assert tools.called == ["patch", "patch"] and len(rev.transport.bodies) == 1


def test_no_write_no_review_no_notice():
    script = [Message("assistant", tool_calls=[]), Message("assistant", "rien écrit")]
    k, _, rev = forced_kernel(Transport("À CORRIGER\n- x"), max_calls=1, script=script[1:])
    turn = k.run_turn(TurnInput("t1", "explique slugify"))
    assert turn.reply == "rien écrit" and rev.transport.bodies == []
