"""Sérialisation `Turn ↔ JSON` et `MemoryStore` — aucun Postgres réel (voir
`tests/integration/test_pg_real.py` pour le vrai append-only Postgres). Le `Ledger` en mémoire est
`nokker.ledger.costs.InMemoryLedger` (F6) et testé là-bas (`tests/unit/ledger/test_costs.py`), pas ici."""
from __future__ import annotations

import json

from nokker.core.turn import (
    Decision, Effect, Gate, InputCheck, LLMCall, Message, Tier, ToolCall, Turn, Usage, Verdict,
)
from nokker.store.pg import MemoryStore, turn_from_dict, turn_to_dict


def rich_turn() -> Turn:
    t = Turn(thread_id="fil-1", index=2,
             input=InputCheck(text="rappelle-moi le mot de passe", redacted=["pw"], domain_flags={"fiscal"}),
             channel="telegram", sender="alice")
    t.messages = [
        Message(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="memory_search",
                                                                    arguments={"query": "pw"},
                                                                    raw_arguments='{"query":"pw"}')]),
        Message(role="tool", content="[secret:pw]", tool_call_id="c1", name="memory_search"),
        Message(role="assistant", content="Je transmets à Alice."),
    ]
    t.calls = [LLMCall(tier=Tier.SHIP, model="qwen/qwen3.8-27b", reasoning=False, prefix_sha256="abc",
                       layer_tokens={"prefix": 100}, usage=Usage(prompt=10, completion=5, cached=2,
                                                                 cached_reported=True),
                       latency_s=1.2, route_reason="défaut : Ship", chf=0.001)]
    t.verdicts = [Verdict(gate=Gate.SWITCH, decision=Decision.ESCALATE, layer=1, rule="domaine:fiscal",
                          reason="r", probability=0.9, judge_model="m", latency_s=0.01, subject="reply")]
    t.escalated = True
    t.pending_approvals = [ToolCall(id="c2", name="infra_lvextend", arguments={})]
    t.error = None
    return t


def test_turn_round_trip_preserves_everything():
    original = rich_turn()
    restored = turn_from_dict(turn_to_dict(original))
    assert restored == original


def test_turn_round_trip_survives_json_text():
    original = rich_turn()
    as_text = json.dumps(turn_to_dict(original))
    restored = turn_from_dict(json.loads(as_text))
    assert restored == original


def test_round_trip_empty_turn():
    t = Turn(thread_id="t", index=0, input=InputCheck(text=""))
    assert turn_from_dict(turn_to_dict(t)) == t


def test_channel_and_sender_round_trip():
    t = Turn(thread_id="t", index=0, input=InputCheck(text="x"), channel="slack", sender="bruno")
    restored = turn_from_dict(turn_to_dict(t))
    assert restored.channel == "slack" and restored.sender == "bruno"


def test_sender_none_round_trips_to_none():
    t = Turn(thread_id="t", index=0, input=InputCheck(text="x"))
    assert turn_from_dict(turn_to_dict(t)).sender is None
    assert turn_from_dict(turn_to_dict(t)).channel == "api"


def test_memory_store_append_and_history():
    store = MemoryStore()
    t0 = Turn(thread_id="t1", index=0, input=InputCheck(text="a"))
    t1 = Turn(thread_id="t1", index=1, input=InputCheck(text="b"))
    store.append(t0)
    store.append(t1)
    assert [t.index for t in store.history("t1")] == [0, 1]
    assert store.next_index("t1") == 2
    assert store.history("t1", limit=1) == [t1]


def test_memory_store_separates_threads():
    store = MemoryStore()
    store.append(Turn(thread_id="a", index=0, input=InputCheck(text="x")))
    assert store.history("b") == []
    assert store.next_index("b") == 0


def test_effect_enum_default_write_is_prudent():
    assert Effect.WRITE.value == "write"
