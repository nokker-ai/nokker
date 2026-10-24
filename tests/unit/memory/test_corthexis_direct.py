"""`DirectCorthexisMemory` (mode ii, A-2) — pas de Postgres/embed réel ici : on monkeypatch
`corthexis.service.{search,get,reset}`, le paquet réel étant installé (`--no-deps`, voir le
docstring de `nokker/memory/corthexis.py`) mais jamais sollicité en réseau dans ces tests.
Le test d'intégration réel (vrai Postgres + vrai embed) est dans
`test_corthexis_direct_integration.py`, marqué `integration`."""
from __future__ import annotations

import json
import logging

import pytest

# Paquet optionnel (installé --no-deps, épingle mcp<2) : absent de la CI de base → module sauté, pas la suite.
cx_service = pytest.importorskip("corthexis.service")

from nokker.core.ports import ToolContext  # noqa: E402
from nokker.memory.corthexis import DirectCorthexisMemory  # noqa: E402


def make_memory(**kwargs) -> DirectCorthexisMemory:
    return DirectCorthexisMemory(database_url="postgresql://unused/unused", embed_urls="http://unused:1", **kwargs)


def test_post_init_sets_env_without_overwriting_operator_value(monkeypatch):
    monkeypatch.delenv("CORTHEXIS_DATABASE_URL", raising=False)
    DirectCorthexisMemory(database_url="postgresql://a/db")
    import os
    assert os.environ["CORTHEXIS_DATABASE_URL"] == "postgresql://a/db"

    monkeypatch.setenv("CORTHEXIS_DATABASE_URL", "postgresql://deja-pose/db")
    DirectCorthexisMemory(database_url="postgresql://b/db")
    assert os.environ["CORTHEXIS_DATABASE_URL"] == "postgresql://deja-pose/db"


def test_recall_empty_query_returns_empty_without_calling_service(monkeypatch):
    called = []
    monkeypatch.setattr(cx_service, "search", lambda *a, **k: called.append(1) or [])
    mem = make_memory()
    assert mem.recall("   ") == []
    assert not called


def test_recall_filters_with_prod_hook_rule(monkeypatch):
    rows = [
        {"note_name": "hote21-gpu-experiment-safety", "score": 0.72, "rerank": None, "snippet": "clinfo -l"},
        {"note_name": "bruit", "score": 0.10, "rerank": None, "snippet": "pas pertinent"},
        {"note_name": "silence", "score": 0.26, "rerank": 0.6, "snippet": "rattrapé par le reranker"},
    ]
    monkeypatch.setattr(cx_service, "search", lambda *a, **k: rows)
    mem = make_memory()
    out = mem.recall("gpu hote21 experiment")
    assert {r.note for r in out} == {"hote21-gpu-experiment-safety", "silence"}


def test_recall_named_note_kept_despite_near_zero_score(monkeypatch):
    rows = [{"note_name": "hote21-gpu-experiment-safety", "score": 0.0, "rerank": None, "snippet": "x"}]
    monkeypatch.setattr(cx_service, "search", lambda *a, **k: rows)
    mem = make_memory()
    out = mem.recall("hote21-gpu-experiment-safety ?")
    assert [r.note for r in out] == ["hote21-gpu-experiment-safety"]


def test_recall_skips_error_and_info_rows(monkeypatch):
    monkeypatch.setattr(cx_service, "search", lambda *a, **k: [{"error": "store down"}])
    assert make_memory().recall("une question") == []
    monkeypatch.setattr(cx_service, "search", lambda *a, **k: [{"info": "empty", "empty": True}])
    assert make_memory().recall("une question") == []


def test_recall_respects_k(monkeypatch):
    rows = [{"note_name": f"note-{i}", "score": 0.9, "rerank": None, "snippet": ""} for i in range(10)]
    monkeypatch.setattr(cx_service, "search", lambda *a, **k: rows)
    out = make_memory().recall("question", k=2)
    assert len(out) == 2


def test_recall_service_failure_returns_empty_not_raise(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store down")
    monkeypatch.setattr(cx_service, "search", boom)
    assert make_memory().recall("x") == []


def test_recall_passes_deep_and_projects(monkeypatch):
    seen = {}

    def fake_search(query, top_k=8, *, deep=False, projects=None):
        seen.update(query=query, top_k=top_k, deep=deep, projects=projects)
        return []
    monkeypatch.setattr(cx_service, "search", fake_search)
    mem = make_memory(deep=True, projects=("radio",))
    mem.recall("une question", k=3)
    assert seen["deep"] is True
    assert seen["projects"] == ("radio",)
    assert seen["top_k"] >= 3


def test_propose_is_noop_and_logs(caplog):
    mem = make_memory()
    with caplog.at_level(logging.INFO, logger="nokker.memory.corthexis"):
        mem.propose("fil-1", ["Alice préfère le café noir"])
    assert any("quarantaine" in r.message for r in caplog.records)


def test_propose_empty_facts_does_not_log(caplog):
    mem = make_memory()
    with caplog.at_level(logging.INFO, logger="nokker.memory.corthexis"):
        mem.propose("fil-1", [])
    assert caplog.records == []


def test_tool_specs_expose_memory_search_and_get_as_core_with_direct_source():
    mem = make_memory()
    specs = {s.name: s for s in mem.specs()}
    assert set(specs) == {"memory_search", "memory_get"}
    assert all(s.core for s in specs.values())
    assert all(s.source == "corthexis:direct" for s in specs.values())


def test_tool_call_memory_search_roundtrip(monkeypatch):
    rows = [{"note_name": "nokker", "score": 0.9, "snippet": "x"}]
    monkeypatch.setattr(cx_service, "search", lambda *a, **k: rows)
    ctx = ToolContext(thread_id="t", turn_index=0, channel="api")
    r = make_memory().call("memory_search", {"query": "nokker", "top_k": 4}, ctx)
    assert not r.is_error
    assert json.loads(r.content) == {"result": rows}


def test_tool_call_memory_search_error_row_is_error(monkeypatch):
    monkeypatch.setattr(cx_service, "search", lambda *a, **k: [{"error": "store down"}])
    ctx = ToolContext(thread_id="t", turn_index=0, channel="api")
    r = make_memory().call("memory_search", {"query": "x"}, ctx)
    assert r.is_error


def test_tool_call_memory_get_found(monkeypatch):
    monkeypatch.setattr(cx_service, "get", lambda *a, **k: "corps complet de la note")
    ctx = ToolContext(thread_id="t", turn_index=0, channel="api")
    r = make_memory().call("memory_get", {"note_name": "nokker"}, ctx)
    assert r.content == "corps complet de la note" and not r.is_error


def test_tool_call_memory_get_not_found(monkeypatch):
    monkeypatch.setattr(cx_service, "get", lambda *a, **k: None)
    ctx = ToolContext(thread_id="t", turn_index=0, channel="api")
    r = make_memory().call("memory_get", {"note_name": "inconnue"}, ctx)
    assert r.is_error


def test_tool_call_unknown_tool_is_error():
    ctx = ToolContext(thread_id="t", turn_index=0, channel="api")
    r = make_memory().call("autre_outil", {}, ctx)
    assert r.is_error


def test_tool_call_service_exception_is_error_not_raised(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(cx_service, "search", boom)
    ctx = ToolContext(thread_id="t", turn_index=0, channel="api")
    r = make_memory().call("memory_search", {"query": "x"}, ctx)
    assert r.is_error


def test_close_resets_service(monkeypatch):
    called = []
    monkeypatch.setattr(cx_service, "reset", lambda: called.append(1))
    make_memory().close()
    assert called == [1]


def test_close_never_raises_even_if_reset_fails(monkeypatch):
    def boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(cx_service, "reset", boom)
    make_memory().close()  # ne doit pas lever
