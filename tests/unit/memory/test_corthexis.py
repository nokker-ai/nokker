"""`CorthexisMemory` — pas de socket réel : même `FakeSyncMcp` que `tests/unit/tools/test_mcp.py`
(exécution directe des coroutines, fausse session MCP)."""
from __future__ import annotations

import asyncio
import json
import logging

from mcp import types

from nokker.core.ports import ToolContext
from nokker.memory.corthexis import CorthexisMemory, DirectCorthexisMemory, build, kept, named


class FakeSession:
    def __init__(self, call_results):
        self._call_results = call_results

    async def call_tool(self, name, arguments):
        fn = self._call_results[name]
        return fn(arguments) if callable(fn) else fn


class FakeSyncMcp:
    def __init__(self, session):
        self._session = session

    def run(self, coro):
        return asyncio.run(coro)

    async def session_async(self):
        return self._session

    def close(self):
        pass


def _text_result(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


def make_memory(call_results):
    mem = CorthexisMemory(url="https://example.invalid/mcp", token="tok")
    mem._io = FakeSyncMcp(FakeSession(call_results))
    return mem


def _multi_block_result(*note_dicts: dict) -> types.CallToolResult:
    """UN bloc texte PAR NOTE, format RÉEL de `memory.example.ch/mcp` (pas `{"result": [...]}`
    dans un bloc unique) — vérifié contre le service le 17.10.2026, cf. le docstring de
    `_rows_from_mcp_result`."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(d)) for d in note_dicts])


def test_recall_filters_below_threshold():
    payload = json.dumps({"result": [
        {"note_name": "hote21-gpu-experiment-safety", "score": 0.72, "snippet": "GO explicite requis"},
        {"note_name": "bruit", "score": 0.10, "snippet": "pas pertinent"},
    ]})
    mem = make_memory({"memory_search": _text_result(payload)})
    out = mem.recall("gpu hote21")
    assert [r.note for r in out] == ["hote21-gpu-experiment-safety"]
    assert out[0].score == 0.72


def test_recall_empty_query_returns_empty_without_calling_mcp():
    mem = make_memory({})
    assert mem.recall("   ") == []


def test_recall_malformed_json_returns_empty():
    mem = make_memory({"memory_search": _text_result("pas du json")})
    assert mem.recall("x") == []


def test_recall_mcp_failure_returns_empty_not_raise():
    class Boom(FakeSyncMcp):
        def run(self, coro):
            coro.close()
            raise RuntimeError("mcp down")
    mem = CorthexisMemory(url="https://example.invalid/mcp")
    mem._io = Boom(FakeSession({}))
    assert mem.recall("x") == []


def test_propose_is_noop_and_logs(caplog):
    mem = make_memory({})
    with caplog.at_level(logging.INFO, logger="nokker.memory.corthexis"):
        mem.propose("fil-1", ["Alice préfère le café noir"])
    assert any("quarantaine" in r.message for r in caplog.records)


def test_propose_empty_facts_does_not_log(caplog):
    mem = make_memory({})
    with caplog.at_level(logging.INFO, logger="nokker.memory.corthexis"):
        mem.propose("fil-1", [])
    assert caplog.records == []


def test_tool_specs_expose_memory_search_and_get_as_core():
    mem = make_memory({})
    names = {s.name: s for s in mem.specs()}
    assert set(names) == {"memory_search", "memory_get"}
    assert all(s.core for s in names.values())


def test_tool_call_memory_get():
    mem = make_memory({"memory_get": _text_result("corps complet de la note")})
    ctx = ToolContext(thread_id="t", turn_index=0, channel="api")
    r = mem.call("memory_get", {"note_name": "nokker"}, ctx)
    assert r.content == "corps complet de la note" and not r.is_error


def test_score_threshold_is_040_by_default():
    assert CorthexisMemory().score_threshold == 0.40


# ─────────────────────── A-2 : règle d'injection du hook de prod (named/kept) ───────────────────────
# Port de `infra/sokkan/memory/hook_prompt_recall.py` (dépôt interne) : score>=0.40, OU rerank>=0.5
# avec score>=0.25, OU le nom de la note cité dans le message.

def test_named_full_slug_cited():
    assert named("hote21-gpu-experiment-safety", "la note hote21-gpu-experiment-safety le dit")


def test_named_underscored_name_matches_hyphen_slug():
    assert named("hote21_gpu_experiment_safety", "hote21-gpu-experiment-safety")


def test_named_two_segments_of_name_present():
    # "hote21" (4 lettres) et "experiment" (10 lettres) du slug, mais le message ne cite pas le nom
    assert named("hote21-gpu-experiment-safety", "on lance une experiment sur hote21 cet aprem")


def test_named_single_segment_is_not_enough():
    # un seul mot du nom (> règle du 03.10.2026 : "mission" ou "feedback" seuls ne suffisent plus)
    assert not named("hote21-gpu-experiment-safety", "il nous faut une hote21 de plus")


def test_named_no_segment_present():
    assert not named("hote21-gpu-experiment-safety", "quel temps fait-il à Genève ?")


def test_kept_score_above_threshold_alone():
    assert kept(0.41, None, "bruit", "message sans rapport")


def test_kept_rerank_strong_rescues_low_score():
    assert kept(0.26, 0.6, "bruit", "message sans rapport")


def test_kept_rerank_below_floor_score_not_rescued():
    # score < 0.25 : même un bon rerank ne rattrape pas (règle exacte du hook)
    assert not kept(0.24, 0.9, "bruit", "message sans rapport")


def test_kept_rerank_weak_does_not_rescue():
    assert not kept(0.30, 0.3, "bruit", "message sans rapport")


def test_kept_named_rescues_below_every_threshold():
    assert kept(0.0, None, "hote21-gpu-experiment-safety", "hote21-gpu-experiment-safety ?")


def test_kept_nothing_matches_is_dropped():
    assert not kept(0.10, 0.2, "bruit", "aucun rapport")


def test_mcp_recall_named_note_kept_despite_low_score():
    payload = json.dumps({"result": [
        {"note_name": "hote21-gpu-experiment-safety", "score": 0.05, "snippet": "vérifier le matériel"},
    ]})
    mem = make_memory({"memory_search": _text_result(payload)})
    out = mem.recall("hote21-gpu-experiment-safety ?")
    assert [r.note for r in out] == ["hote21-gpu-experiment-safety"]


def test_mcp_recall_rerank_rescue():
    payload = json.dumps({"result": [
        {"note_name": "bruit", "score": 0.26, "rerank": 0.6, "snippet": "x"},
        {"note_name": "silence", "score": 0.26, "rerank": 0.3, "snippet": "y"},
    ]})
    mem = make_memory({"memory_search": _text_result(payload)})
    out = mem.recall("question quelconque")
    assert [r.note for r in out] == ["bruit"]


# ───────────── RÉGRESSION A-2 : format RÉEL du serveur, un bloc texte PAR NOTE ─────────────
# Bug réel trouvé en mesurant A1 contre memory.example.ch/mcp (17.10.2026) : avant ce correctif,
# `recall()` passait `text_of(result)` (jointure `\n` des blocs) à `json.loads` — avec plusieurs
# blocs, le texte joint est plusieurs objets JSON concaténés, invalide, et le MCP réel RENVOIE
# UN BLOC PAR NOTE (jamais `{"result": [...]}` dans un bloc unique) → 0/300 sur le banc A1,
# alors que le service répondait correctement. `_rows_from_mcp_result` lit les blocs correctement.

def test_mcp_recall_real_multi_block_format():
    result = _multi_block_result(
        {"note_name": "hote21-gpu-experiment-safety", "score": 0.72, "snippet": "vérifier le matériel"},
        {"note_name": "bruit", "score": 0.05, "snippet": "pas pertinent"},
    )
    mem = make_memory({"memory_search": result})
    out = mem.recall("gpu hote21")
    assert [r.note for r in out] == ["hote21-gpu-experiment-safety"]


def test_mcp_recall_real_multi_block_single_note():
    # exactement UNE note trouvée : un seul bloc texte, qui N'EST PAS enveloppé dans {"result": [...]}
    # (ambiguïté avec le format historique à un seul bloc — doit rester distingué par le contenu).
    result = _multi_block_result({"note_name": "hote21-gpu-experiment-safety", "score": 0.80, "snippet": "x"})
    mem = make_memory({"memory_search": result})
    out = mem.recall("gpu hote21")
    assert [r.note for r in out] == ["hote21-gpu-experiment-safety"]


def test_mcp_recall_structured_content_preferred_when_present():
    # sortie structurée MCP 2.0 : source la plus sûre quand le serveur la fournit.
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text="ignoré si structured_content présent")],
        structured_content={"result": [{"note_name": "hote21-gpu-experiment-safety", "score": 0.9}]},
    )
    mem = make_memory({"memory_search": result})
    out = mem.recall("gpu hote21")
    assert [r.note for r in out] == ["hote21-gpu-experiment-safety"]


def test_mcp_recall_real_multi_block_skips_unparseable_block():
    result = types.CallToolResult(content=[
        types.TextContent(type="text", text=json.dumps({"note_name": "bonne-note", "score": 0.9})),
        types.TextContent(type="text", text="pas du json"),
    ])
    mem = make_memory({"memory_search": result})
    out = mem.recall("question")
    assert [r.note for r in out] == ["bonne-note"]


# ───────────────────────────────────── build() : choix (i)/(ii) par config ─────────────────────────────────────

def test_build_default_is_mcp(monkeypatch):
    monkeypatch.delenv("NOKKER_MEMORY_MODE", raising=False)
    assert isinstance(build(), CorthexisMemory)


def test_build_env_selects_direct(monkeypatch):
    monkeypatch.setenv("NOKKER_MEMORY_MODE", "direct")
    mem = build()
    assert isinstance(mem, DirectCorthexisMemory)


def test_build_explicit_mode_overrides_env(monkeypatch):
    monkeypatch.setenv("NOKKER_MEMORY_MODE", "direct")
    assert isinstance(build(mode="mcp"), CorthexisMemory)


def test_build_unknown_mode_raises(monkeypatch):
    monkeypatch.setenv("NOKKER_MEMORY_MODE", "telepathie")
    try:
        build()
        assert False, "devait lever ValueError"
    except ValueError:
        pass


def test_build_passes_kwargs_to_mcp():
    mem = build(mode="mcp", url="https://autre.invalid/mcp", token="t")
    assert mem.url == "https://autre.invalid/mcp"


def test_recall_text_starts_with_the_note_description_then_the_matched_snippet():
    """20.10.2026 : un extrait seul, pris au milieu d'une note, a fait dire au modèle « Nokker est un moteur de recherche
    souverain ». La description dit ce qu'EST la note ; l'extrait suit."""
    from nokker.memory.corthexis import _parse_mcp_recall
    rows = [{"note_name": "nokker", "score": 0.9, "description": "Nokker — harness d'agents de Alice.",
             "snippet": "Décision 18.10 : recherche web souveraine."}]
    (r,) = _parse_mcp_recall(rows, "c'est quoi nokker ?", 0.4)
    assert r.text == "Nokker — harness d'agents de Alice. — extrait : Décision 18.10 : recherche web souveraine."
