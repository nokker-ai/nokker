"""(vi) Une ancienne valeur citée par un résumé d'épisode, un message retrouvé ou `recall_context` est marquée
« remplacée par X le … » ; le bloc figé de la mémoire de session le porte au figement. Sans source, rien ne change."""
from __future__ import annotations

from nokker.context.types import ContextBudgets

from .harness import make_session

SUP = [{"old": "A101", "new": "D415", "key": "salle comite pilotage", "at": 1_790_000_000.0}]


def _session(tmp_path):
    s = make_session(ContextBudgets(), tmp_path=tmp_path)
    s.say("Pour le comité de pilotage on prend la salle A101.")
    s.say("Noté, salle A101 pour la réunion de direction.")
    assert s.manager.new_episode(s.thread, "manual")
    s.say("Autre sujet : la météo de demain.")
    return s


def test_recall_context_marks_a_replaced_value(tmp_path):
    s = _session(tmp_path)
    plain = s.manager.recall(s.thread, query="salle comité de pilotage")
    assert "A101" in plain and "remplacée" not in plain
    s.manager.superseded = lambda _t: SUP
    marked = s.manager.recall(s.thread, query="salle comité de pilotage")
    assert marked.count("remplacée par « D415 »") >= 1 and "2026-10-22" not in marked



def test_the_frozen_memory_block_carries_the_mark_and_the_current_value_is_never_marked(tmp_path):
    s = _session(tmp_path)
    s.manager.superseded = lambda _t: SUP
    s.manager._mem_snap.clear()  # le bloc se refige à l'événement suivant (clôture, épinglage) : une valeur remplacée entre-temps n'y entre qu'alors
    block = s.manager.frozen_memory_block(s.thread)
    assert "A101" in block and "remplacée par « D415 »" in block
    line_with_new = [ln for ln in block.split("\n") if "D415" in ln and "A101" in ln]
    assert all(ln.count("remplacée") <= 1 for ln in line_with_new)


def test_no_source_or_no_occurrence_leaves_the_text_byte_identical(tmp_path):
    s = _session(tmp_path)
    base = s.manager.recall(s.thread, query="salle comité de pilotage")
    s.manager.superseded = lambda _t: [{"old": "Z999", "new": "Q1", "key": "k", "at": 1.0}]
    assert s.manager.recall(s.thread, query="salle comité de pilotage") == base
    s.manager.superseded = lambda _t: (_ for _ in ()).throw(RuntimeError("base en panne"))
    assert s.manager.recall(s.thread, query="salle comité de pilotage") == base  # une panne ne casse pas la relecture
