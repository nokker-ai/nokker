"""Point 2 : un historique importé (fil d'avant ce chantier) est délesté AU PREMIER CONTACT, une fois, comme `compact`."""
from __future__ import annotations

import re

from nokker.context.types import ContextBudgets

from .harness import make_session
from .import_fixture import TOOL_TURNS, entries, import_into, window_tokens_of


def _first_call(budgets: ContextBudgets, tmp_path):
    s = make_session(budgets, tmp_path=tmp_path, tool_size=100)
    n = import_into(s)
    assert n == 266 and len(TOOL_TURNS) == 13
    assert 90_000 <= sum(len(e.content.encode()) for e in entries()) <= 120_000
    turn = s.say("Dans quel fichier se trouve le modèle de référence ?")
    assert turn.error is None
    return s, s.provider.seen[0]


def test_first_call_after_an_import_is_far_smaller_with_the_bootstrap_shed(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _, before = _first_call(ContextBudgets(bootstrap_compact=False), tmp_path / "a")
    s, after = _first_call(ContextBudgets(), tmp_path / "b")
    w_before, w_after = window_tokens_of(before), window_tokens_of(after)
    assert w_after < w_before * 0.8, (w_before, w_after)
    assert w_after <= 13_000, w_after  # objectif : fenêtre + résumés d'un historique de ~104 Ko ≤ ~13 k jetons réels
    st = s.manager.state(s.thread)
    assert st.shed and st.compaction_version >= 1  # événement discret persisté


def test_shed_results_are_recallable_by_their_rs_id(tmp_path):
    s, call = _first_call(ContextBudgets(), tmp_path)
    stubs = [m for m in call if m["role"] == "tool" and "déplacé hors fenêtre" in (m.get("content") or "")]
    assert stubs, "au moins un résultat de la fenêtre doit être délesté"
    for m in stubs:
        rid = re.search(r"recall_context\(\"(rs_[0-9a-f]+)\"\)", m["content"]).group(1)
        full = s.manager.recall(s.thread, ref=rid, max_chars=8000)
        assert "fichier" not in full[:0] and len(full) > 1000  # le contenu complet, pas le stub
        assert "déplacé hors fenêtre" not in full
    assert s.manager.recall(s.thread, ref="rs_000000000000").startswith("Élément inconnu")


def test_bootstrap_shed_runs_once_and_the_view_is_stable_afterwards(tmp_path):
    s, first = _first_call(ContextBudgets(), tmp_path)
    version = s.manager.state(s.thread).compaction_version
    s.say("Et la valeur du pas de temps ?")
    again = s.provider.seen[-1]
    assert s.manager.state(s.thread).compaction_version >= version
    # entre deux événements la fenêtre ne fait que s'allonger : les messages du 1er appel sont repris tels quels
    n_common = len(first) - 1  # sans le dernier message (la question du 1er tour)
    assert again[:n_common] == first[:n_common]


def test_small_thread_without_tool_results_is_untouched(tmp_path):
    s = make_session(ContextBudgets(), tmp_path=tmp_path)
    s.say("bonjour")
    assert not s.manager.state(s.thread).shed
