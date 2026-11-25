"""Valeurs hors score : NOMS seulement, relecture d'UNE valeur par nom, plafond de jetons des recherches (banc final)."""
from __future__ import annotations

from nokker.context.tools import RecallContextSource
from nokker.context.types import ContextBudgets
from nokker.core import model_texts as mt
from nokker.core.ports import ToolContext
from nokker.memory.provenance import PERSON
from nokker.tenancy.context import tenant_scope

from .test_values_capture import A, TA, Model, _world


def _two_values(svc):
    svc.write_value(A, key="budget marketing", value="8000 francs", title="b", text="budget marketing : 8000 francs")
    svc.write_value(A, key="serveur de test", value="Tarentule", title="s", text="serveur de test : Tarentule")


# ── (a) ──
def test_values_outside_the_score_are_named_never_valued():
    svc, _cap = _world(Model(None))
    svc.settings.always_values_mode = "names"  # le défaut est « marked » (23.11) ; ce mode reste réglable
    with tenant_scope(TA):
        _two_values(svc)
        out = svc.recall(A, "quelle est l'adresse de la machine de préproduction ?")  # aucun mot commun : score nul
        assert not any("Tarentule" in r.text for r in out)
        others = [r for r in out if r.note == "values:others"]
        assert len(others) == 1 and "budget marketing" in others[0].text and "8000" not in others[0].text
        assert others[0].provenance == PERSON  # aucun marqueur « tiré du résultat d'un outil »


def test_scored_values_keep_their_value_and_date():
    svc, _cap = _world(Model(None))
    with tenant_scope(TA):
        _two_values(svc)
        out = svc.recall(A, "budget marketing")
        hit = next(r for r in out if "8000" in r.text)
        assert "depuis le" in hit.text and "values:others" not in hit.note


def test_marked_is_the_default_and_injects_values_with_a_mention_and_off_injects_nothing():
    svc, _cap = _world(Model(None))
    with tenant_scope(TA):
        _two_values(svc)
        assert svc.settings.always_values_mode == "marked"
        out = svc.recall(A, "zzz qqq")
        assert any("8000" in r.text and mt.pick(mt.VALUE_UNSCORED) in r.text for r in out)
        svc.settings.always_values_mode = "off"
        assert svc.recall(A, "zzz qqq") == []


def test_one_value_is_read_back_by_name_and_is_not_a_search(tmp_path):
    from tests.unit.context.harness import make_session

    svc, _cap = _world(Model(None))
    s = make_session(ContextBudgets(), tmp_path=tmp_path)
    with tenant_scope(TA):
        _two_values(svc)
        s.manager.value_lookup = lambda _t, name: svc.value_line(A, name)
        calls: list = []
        real = s.manager.recall
        s.manager.recall = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        src = RecallContextSource(s.manager)
        ctx = ToolContext(thread_id=s.thread, turn_index=1, channel="api")
        by_ref = src.call("recall_context", {"ref": "budget marketing"}, ctx).content
        by_query = src.call("recall_context", {"query": "Budget  Marketing"}, ctx).content
        assert "8000 francs" in by_ref and "8000 francs" in by_query and calls == []
        src.call("recall_context", {"query": "sujet inconnu"}, ctx)
        assert len(calls) == 1  # un nom inconnu retombe sur la recherche d'avant


# ── (c) ──
def test_search_budget_stops_further_searches(tmp_path):
    from tests.unit.context.harness import make_session

    s = make_session(ContextBudgets(recall_budget_tokens=50), tmp_path=tmp_path)
    s.manager.recall = lambda *a, **k: "résultat " * 200  # ~400 jetons
    src = RecallContextSource(s.manager)
    ctx = ToolContext(thread_id=s.thread, turn_index=5, channel="api")
    first = src.call("recall_context", {"query": "premier"}, ctx).content
    second = src.call("recall_context", {"query": "second"}, ctx).content
    assert first.startswith("résultat") and second == mt.pick(mt.RECALL_NOTHING_FOUND)
    other = ToolContext(thread_id=s.thread, turn_index=6, channel="api")  # un autre tour repart de zéro
    assert src.call("recall_context", {"query": "premier"}, other).content.startswith("résultat")




def test_the_no_invention_instruction_is_added_only_when_the_recall_carries_values():
    svc, _cap = _world(Model(None))
    with tenant_scope(TA):
        svc.write_value(A, key="note libre", value="x", title="n", text="n")  # une valeur, mais plus bas on vide
        assert any(r.note == "values:guard" for r in svc.recall(A, "zzz qqq"))
        guard = next(r for r in svc.recall(A, "zzz qqq") if r.note == "values:guard")
        assert guard.text == mt.pick(mt.VALUES_GUARD) and "sans proposer de valeur" in guard.text
        assert "synonyme" in guard.text and "cite son nom" in guard.text  # v9
        svc.settings.values_guard_mode = "off"
        assert not any(r.note == "values:guard" for r in svc.recall(A, "zzz qqq"))  # consigne éteinte : pas de ligne
        svc.settings.values_guard_mode = "on"
        svc.settings.always_values_mode = "off"
        assert not any(r.note == "values:guard" for r in svc.recall(A, "zzz qqq"))  # aucune valeur dans le bloc : aucun texte
