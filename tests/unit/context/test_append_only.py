"""Lot compact-cache, point 3 : la fenêtre envoyée est APPEND-ONLY entre deux événements (clôture d'épisode), et ce qui
change à l'événement ne touche plus le message système. Mesuré contre le faux amont Anthropic qui compte `cache_read` /
`cache_write` d'après la stabilité du préfixe, octet pour octet (`tests/unit/providers/anthropic_upstream.py::CacheModel`)."""
from __future__ import annotations

import pytest

from nokker.context.types import ContextBudgets
from nokker.core.turn import ToolResult

from .cache_scenario import run
from .harness import BIG_MARK, make_session

LEGACY = {"retroactive_shed": True, "shed_heads": True, "memory_as_message": False, "compaction_step": 3000}


def _scenario(tmp_path, n=40, close_after=25, **over):
    bodies: list = []
    rows = run(ContextBudgets(**over), tmp_path, n_turns=n, close_after=close_after, bodies=bodies)
    return rows, bodies


def test_prefix_is_byte_identical_between_events_and_cache_read_covers_the_previous_call(tmp_path):
    rows, _ = _scenario(tmp_path)
    assert any(r["event"] == "close" for r in rows), "le scénario doit contenir une clôture"
    closed_at = next(i for i, r in enumerate(rows) if r["event"] == "close")
    for i in range(1, len(rows)):
        if i == closed_at + 1:  # l'appel qui suit la clôture : la fenêtre est remplacée, vérifié ci-dessous
            continue
        assert rows[i]["read"] >= 0.9 * rows[i - 1]["total"], (i, rows[i - 1], rows[i])
        assert rows[i]["read"] == rows[i - 1]["total"], (i, "append-only : tout l'appel précédent est relu")


def test_closure_keeps_system_and_tools_cached_and_rewrites_only_the_new_window(tmp_path):
    rows, bodies = _scenario(tmp_path)
    closed_at = next(i for i, r in enumerate(rows) if r["event"] == "close")
    before, after = bodies[closed_at], bodies[closed_at + 1]
    assert before["system"] == after["system"], "la clôture ne modifie plus le message système"
    assert before["tools"] == after["tools"]
    first = after["messages"][0]["content"][0]["text"]
    assert first.startswith("[Mémoire de la session]") and "ep_" in first  # le résumé, en tête des messages
    prev, nxt = rows[closed_at], rows[closed_at + 1]
    system_and_tools = nxt["read"]
    assert system_and_tools > 5000  # le préfixe hôte (~7 k) + outils restent lus
    assert nxt["write"] < 0.35 * prev["total"], (prev, nxt)  # seule la nouvelle fenêtre est réécrite


def test_old_layout_rewrites_the_system_at_closure(tmp_path):
    """Avant : le résumé est le dernier bloc du système, la clôture le réécrit."""
    _, bodies = _scenario(tmp_path, memory_as_message=False)
    texts = [b["system"][-1]["text"] for b in bodies]
    assert any("[Mémoire de la session]" in t for t in texts) and "[Mémoire de la session]" not in texts[0]


def test_turn_heads_are_never_removed_from_old_messages(tmp_path):
    bodies: list = []
    run(ContextBudgets(), tmp_path, n_turns=30, close_after=None, bodies=bodies)
    for b in bodies:
        first_user = b["messages"][0]["content"][0]["text"]
        assert first_user.startswith("[Date et heure actuelles]"), first_user[:60]  # messages[0] jamais réécrit


def test_no_retroactive_rewrite_the_view_never_shortens_a_sent_message(tmp_path):
    bodies: list = []
    run(ContextBudgets(), tmp_path, n_turns=30, close_after=None, bodies=bodies)
    for a, b in zip(bodies, bodies[1:], strict=False):
        for ma, mb in zip(a["messages"], b["messages"], strict=False):
            ta = [x for x in ma["content"] if x["type"] != "thinking"]
            tb = [x for x in mb["content"] if x["type"] != "thinking"]
            assert [{k: v for k, v in x.items() if k != "cache_control"} for x in ta] \
                == [{k: v for k, v in x.items() if k != "cache_control"} for x in tb]


def test_legacy_flags_restore_the_rewriting_behaviour(tmp_path):
    rows, bodies = _scenario(tmp_path, n=60, close_after=None, **LEGACY)
    assert any(r["event"] == "shed" for r in rows)
    shed_at = next(i for i, r in enumerate(rows) if r["event"] == "shed")
    assert rows[shed_at + 1]["read"] < 0.9 * rows[shed_at]["total"]  # le délestage rétroactif casse le cache aval
    assert not bodies[shed_at + 1]["messages"][0]["content"][0]["text"].startswith("[Date et heure")


def test_new_defaults_rewrite_less_than_the_legacy_flags_on_the_same_scenario(tmp_path):
    (tmp_path / "n").mkdir()
    (tmp_path / "o").mkdir()
    new, _ = _scenario(tmp_path / "n", n=60, close_after=None)
    old, _ = _scenario(tmp_path / "o", n=60, close_after=None, **LEGACY)
    assert sum(r["write"] for r in new) < sum(r["write"] for r in old)
    assert sum(r["event"] == "shed" for r in new) == 0


# ───────────── admission : la forme d'un résultat est décidée à son arrivée ─────────────


def _mid_result(i: int, chars: int = 2400) -> ToolResult:
    body = "".join(f"ligne {i}-{n:04d} : disque hote21 à 91 pourcent, rien d'anormal\n" for n in range(60))[:chars]
    return ToolResult(call_id=f"c{i}", name="big_tool", content=body)


def test_mid_size_results_are_final_at_arrival_within_a_per_turn_budget(tmp_path):
    s = make_session(ContextBudgets(shed_min_tokens=300, reservoir_min_tokens=1000, admit_turn_budget=1000,
                                    reservoir_excerpt_chars=500), tmp_path=tmp_path)
    first = s.manager.admit_result_in(s.thread, 7, _mid_result(1))
    assert first.content == _mid_result(1).content and first.reservoir_id is None  # dans le budget du tour : en clair
    second = s.manager.admit_result_in(s.thread, 7, _mid_result(2))
    assert second.reservoir_id and second.reservoir_id.startswith("rs_") and "recall_context" in second.content
    other_turn = s.manager.admit_result_in(s.thread, 8, _mid_result(3))
    assert other_turn.reservoir_id is None  # le budget est PAR tour
    tiny = s.manager.admit_result_in(s.thread, 7, ToolResult(call_id="t", name="big_tool", content="ok " * 20))
    assert tiny.reservoir_id is None


def test_extract_that_would_not_shorten_keeps_the_result_whole(tmp_path):
    s = make_session(ContextBudgets(shed_min_tokens=300, reservoir_min_tokens=1000, admit_turn_budget=0,
                                    reservoir_excerpt_chars=2300), tmp_path=tmp_path)
    out = s.manager.admit_result_in(s.thread, 1, _mid_result(1, chars=2400))
    assert out.reservoir_id is None


def test_retroactive_mode_leaves_mid_results_to_compact(tmp_path):
    s = make_session(ContextBudgets(retroactive_shed=True, admit_turn_budget=0), tmp_path=tmp_path)
    assert s.manager.admit_result_in(s.thread, 1, _mid_result(1)).reservoir_id is None


def test_without_retroactive_shed_the_state_never_records_a_compaction_before_closure(tmp_path):
    s = make_session(ContextBudgets(current_budget=10 ** 6), tmp_path=tmp_path, tool_size=2600)
    for i in range(10):
        s.say(f"logs {i} {BIG_MARK}")
    st = s.manager.state(s.thread)
    assert st.compaction_version == 0 and not st.shed and st.heads_shed_before == 0


# ───────────── résumé de session figé ─────────────


def test_memory_message_is_frozen_between_events_and_follows_pins(tmp_path):
    s = make_session(ContextBudgets(current_budget=400, episodes_budget=500), tmp_path=tmp_path)
    for i in range(12):
        s.say(f"sujet {i} : on parle de la migration du disque et des sauvegardes nocturnes, avec beaucoup de détails.")
    closed = s.manager.episodes.list(s.thread, closed_only=True)
    assert closed
    before = s.manager.frozen_memory_block(s.thread)
    assert before.startswith("[Mémoire de la session]")
    ep = closed[-1]
    ep.summary = "RÉSUMÉ RAFFINÉ ASYNCHRONE"
    s.manager.episodes.save(ep)
    assert s.manager.frozen_memory_block(s.thread) == before  # un résumé raffiné ne réécrit pas la requête
    assert "RÉSUMÉ RAFFINÉ" in s.manager.memory_block(s.thread)  # le bloc vivant, lui, le montre
    s.manager.pin(s.thread, closed[0].id)
    assert s.manager.frozen_memory_block(s.thread) != before  # un événement (épinglage) le renouvelle


@pytest.mark.parametrize("as_message", [True, False])
def test_memory_block_is_in_exactly_one_place(tmp_path, as_message):
    s = make_session(ContextBudgets(current_budget=400, episodes_budget=500, memory_as_message=as_message),
                     tmp_path=tmp_path)
    for i in range(12):
        s.say(f"sujet {i} : on parle de la migration du disque et des sauvegardes nocturnes, avec beaucoup de détails.")
    msgs = s.provider.seen[-1]
    in_system = "[Mémoire de la session]" in msgs[0]["content"]
    as_user = msgs[1]["role"] == "user" and msgs[1]["content"].startswith("[Mémoire de la session]")
    assert in_system == (not as_message) and as_user == as_message
