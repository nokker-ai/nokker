"""`DirectCorthexisMemory` contre un VRAI Postgres + VRAI serveur d'embedding CPU (A-2, (ii)).

Marqué `integration` (voir `pyproject.toml`) : SKIP si `NOKKER_TEST_CORTHEXIS_DSN` /
`NOKKER_TEST_CORTHEXIS_EMBED_URL` (ou leurs défauts, `conftest.py`) ne répondent pas — jamais
d'échec de CI sans ces services. Ce test ne touche NI la mémoire de prod NI sa base : base
jetable créée/détruite par test (`indexed_corthexis_memory`), corpus écrit dans `tmp_path`.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_recall_finds_the_right_note_end_to_end(indexed_corthexis_memory):
    out = indexed_corthexis_memory.recall("avant de lancer un test de charge GPU sur hote21 que faut-il vérifier ?")
    assert out, "aucune note rappelée"
    assert out[0].note == "hote21-gpu-experiment-safety"
    assert out[0].score > 0


def test_recall_cross_lingual_english_question_on_french_note(indexed_corthexis_memory):
    out = indexed_corthexis_memory.recall("is it safe to run a GPU load test on hote21 right now?")
    assert any(r.note == "hote21-gpu-experiment-safety" for r in out)


def test_recall_unrelated_question_does_not_force_a_note(indexed_corthexis_memory):
    out = indexed_corthexis_memory.recall("quelle est la capitale de l'Islande ?")
    assert out == [] or all(r.score >= 0.40 for r in out)  # pas de rappel forcé par un faux rerank/nom


def test_tool_memory_search_matches_recall_candidates(indexed_corthexis_memory):
    from nokker.core.ports import ToolContext
    ctx = ToolContext(thread_id="t", turn_index=0, channel="api")
    r = indexed_corthexis_memory.call("memory_search", {"query": "hote21 gpu", "top_k": 5}, ctx)
    assert not r.is_error
    assert "hote21-gpu-experiment-safety" in r.content


def test_tool_memory_get_returns_full_body(indexed_corthexis_memory):
    from nokker.core.ports import ToolContext
    ctx = ToolContext(thread_id="t", turn_index=0, channel="api")
    r = indexed_corthexis_memory.call("memory_get", {"note_name": "hote21-gpu-experiment-safety"}, ctx)
    assert not r.is_error
    assert "clinfo" in r.content
