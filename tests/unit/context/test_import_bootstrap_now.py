"""Chargement d'un historique importé : l'état de vue du fil est amorcé AU CHARGEMENT (épisodes, délestage des gros
résultats d'outils, capture des valeurs durables), pas au premier tour — le premier appel du modèle ne part plus avec tout
l'historique en clair, et la capture voit l'import. Un échec de l'amorçage ne fait jamais échouer l'import."""
from __future__ import annotations

from nokker.context.types import ContextBudgets
from nokker.core import history_import as hi

from .harness import make_session

BIG = "ligne de journal — disque à 91 pourcent, rien d'anormal\n" * 120
HISTORY = [
    {"role": "user", "content": "Lis le journal"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "big_tool", "arguments": {}}]},
    {"role": "tool", "content": BIG, "name": "big_tool", "tool_call_id": "c1"},
    {"role": "assistant", "content": "Rien d'anormal."},
    {"role": "user", "content": "Le budget retenu est de 5000 francs."},
    {"role": "assistant", "content": "Noté."},
]


def test_import_primes_the_view_state_sheds_and_triggers_the_capture(tmp_path):
    s = make_session(ContextBudgets(), tmp_path=tmp_path, thread="th_imp")
    seen: list = []
    s.manager.on_capture = lambda thread, turns, reason: seen.append((thread, len(turns), reason))
    assert s.manager.episodes.get_state("th_imp") is None
    out = hi.import_history(s.kernel, "th_imp", {"turns": HISTORY})
    assert out["turns"] == 2
    st = s.manager.episodes.get_state("th_imp")
    assert st is not None and st.compaction_version >= 1  # amorcé (délestage `bootstrap_compact`) AVANT tout tour du modèle
    assert seen == [("th_imp", 2, "import")]


def test_a_failing_priming_never_fails_the_import(tmp_path):
    s = make_session(ContextBudgets(), tmp_path=tmp_path, thread="th_imp2")

    def boom(_thread):
        raise RuntimeError("base en panne")

    s.manager.state = boom
    out = hi.import_history(s.kernel, "th_imp2", {"turns": HISTORY})
    assert out["turns"] == 2 and s.kernel.store.history("th_imp2")
