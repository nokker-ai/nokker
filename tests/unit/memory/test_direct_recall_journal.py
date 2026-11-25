"""Journal du rappel direct CortHeXis : `service.search` ne journalise jamais (seul le moteur de rappel du paquet écrit
`recall_log` et `recall_turns`) ; `DirectCorthexisMemory.recall` les écrit donc lui-même, une ligne par recherche, même
sans résultat, et une panne du journal ne casse jamais le tour. Paquet `corthexis` simulé (aucun réseau, aucune base)."""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field

import pytest

from nokker.memory.corthexis import DirectCorthexisMemory


@dataclass
class _Hit:
    note_name: str
    score: float
    cosine: float | None = None
    lexical: float = 0.0
    rerank: float | None = None
    snippet: str = ""
    age_days: int | None = None
    date_source: str = ""
    generation: int | None = None


@dataclass
class _Store:
    recalls: list = field(default_factory=list)
    turns: list = field(default_factory=list)
    fail: bool = False

    def log_recall(self, channel, hits, **kw):
        if self.fail:
            raise RuntimeError("base en panne")
        self.recalls.append((channel, [h.note_name for h in hits], kw))

    def log_recall_turn(self, channel, **kw):
        if self.fail:
            raise RuntimeError("base en panne")
        self.turns.append((channel, kw))


@pytest.fixture
def world(monkeypatch):
    rows: list[dict] = []
    pkg = types.ModuleType("corthexis")
    service = types.ModuleType("corthexis.service")
    service.search = lambda *a, **k: list(rows)
    search = types.ModuleType("corthexis.search")
    search.Hit = _Hit
    pkg.service, pkg.search = service, search
    monkeypatch.setitem(sys.modules, "corthexis", pkg)
    monkeypatch.setitem(sys.modules, "corthexis.service", service)
    monkeypatch.setitem(sys.modules, "corthexis.search", search)
    store = _Store()
    import nokker.context.corthexis_tenancy as ct

    monkeypatch.setattr(ct, "guarded_store", lambda: store)
    mem = DirectCorthexisMemory(database_url="postgresql://unused/unused")
    return mem, rows, store


def test_every_search_is_journaled_with_its_injected_notes(world):
    mem, rows, store = world
    rows += [{"note_name": "reglage-budget", "score": 0.72, "snippet": "budget 5000"},
             {"note_name": "bruit", "score": 0.1, "snippet": "x"}]
    out = mem.recall("quel budget retenu ?", k=3)
    assert [r.note for r in out] == ["reglage-budget"]
    assert store.recalls and store.recalls[0][1] == ["reglage-budget"]
    channel, turn = store.turns[0]
    assert turn["candidates"] == 2 and turn["injected"] == 1 and channel == "nokker"


def test_a_search_without_result_still_leaves_a_turn_row(world):
    mem, rows, store = world
    assert mem.recall("question sans réponse") == []
    assert store.recalls == [] and len(store.turns) == 1 and store.turns[0][1]["injected"] == 0


def test_a_failing_journal_never_breaks_the_turn(world):
    mem, rows, store = world
    rows.append({"note_name": "reglage-budget", "score": 0.72, "snippet": "budget 5000"})
    store.fail = True
    assert [r.note for r in mem.recall("quel budget ?")] == ["reglage-budget"]
