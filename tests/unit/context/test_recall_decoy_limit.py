"""(c) Un leurre (`recall_context` sans résultat) ne relance qu'une fois ; à la 2e recherche vide le noyau répond « rien trouvé »."""
from __future__ import annotations

from nokker.context.tools import RecallContextSource
from nokker.context.types import ContextBudgets
from nokker.core import model_texts as mt
from nokker.core.ports import ToolContext

from .harness import make_session

# ── (c) ──
def test_a_decoy_costs_at_most_one_retry_then_the_core_answers_nothing_found(tmp_path):
    s = make_session(ContextBudgets(), tmp_path=tmp_path)
    src = RecallContextSource(s.manager)
    ctx = ToolContext(thread_id=s.thread, turn_index=3, channel="api")
    outs = [src.call("recall_context", {"query": f"code du coffre secret variante {i}"}, ctx).content for i in range(3)]
    assert "Rien de pertinent" in outs[0]
    assert outs[1] == mt.pick(mt.RECALL_NOTHING_FOUND) and outs[2] == outs[1]
    other_turn = ToolContext(thread_id=s.thread, turn_index=4, channel="api")  # un autre tour repart de zéro
    assert "Rien de pertinent" in src.call("recall_context", {"query": "autre sujet absent"}, other_turn).content
    ref = src.call("recall_context", {"ref": "rs_000000000000"}, ctx).content  # une relecture par id n'est pas une recherche
    assert ref.startswith("Élément inconnu")




def test_after_nothing_found_no_search_is_run_at_all_for_that_turn(tmp_path):
    """Un leurre : au plus UNE relance ; une fois `recall_nothing_found` rendu, `ContextManager.recall` n'est plus appelé."""
    s = make_session(ContextBudgets(), tmp_path=tmp_path)
    src = RecallContextSource(s.manager)
    calls: list[str] = []
    real = s.manager.recall
    s.manager.recall = lambda *a, **k: (calls.append(k.get("query") or ""), real(*a, **k))[1]
    ctx = ToolContext(thread_id=s.thread, turn_index=9, channel="api")
    for i in range(5):
        src.call("recall_context", {"query": f"coffre variante {i}"}, ctx)
    assert len(calls) == 2  # la recherche initiale et UNE relance ; les trois suivantes ne cherchent pas
