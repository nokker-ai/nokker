"""Mêmes comportements que `test_costs.py`, sur le backend Postgres jetable (deliverable 6 : « backends
mémoire + Postgres jetable »). Lancé seulement si `pg_ledger` (conftest.py) trouve une base joignable."""
from __future__ import annotations

import pytest

from nokker.core.turn import Decision, Gate, Tier
from tests.unit.ledger.test_costs import make_call, make_verdict


def test_migrate_is_idempotent(pg_ledger):
    pg_ledger.migrate()
    pg_ledger.migrate()  # ne doit pas lever (CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS)


def test_record_and_fetch_calls_roundtrip(pg_ledger):
    thread = f"{pg_ledger.test_prefix}-fil1"
    pg_ledger.record(thread, 0, make_call(tier=Tier.SHIP, model="qwen/qwen3.8-27b"))
    pg_ledger.record(thread, 1, make_call(tier=Tier.DEEP, model="qwen/qwen3.5-397b-a17b"))

    calls = pg_ledger.fetch_calls(thread_id=thread)
    assert len(calls) == 2
    by_tier = {c.tier for c in calls}
    assert by_tier == {"ship", "deep"}
    # layer_tokens doit faire l'aller-retour JSONB -> dict sans transformation manuelle côté appelant.
    assert all(isinstance(c.layer_tokens, dict) and c.layer_tokens for c in calls)


def test_record_verdict_roundtrip(pg_ledger):
    thread = f"{pg_ledger.test_prefix}-fil2"
    pg_ledger.record_verdict(thread, 0, make_verdict(decision=Decision.ESCALATE, gate=Gate.SWITCH, layer=2))
    verdicts = pg_ledger.fetch_verdicts(thread_id=thread)
    assert len(verdicts) == 1
    assert verdicts[0].decision == "escalate"
    assert verdicts[0].gate == "switch"
    assert verdicts[0].layer == 2


def test_cost_for_call_priority_is_applied_on_insert(pg_ledger):
    """`record()` doit calculer et stocker `chf` avec la même priorité que le backend mémoire :
    `call.chf` du Router si posé, sinon la grille catalogue."""
    thread = f"{pg_ledger.test_prefix}-fil3"
    pg_ledger.record(thread, 0, make_call(chf=0.00321))
    pg_ledger.record(thread, 1, make_call(chf=None, model="qwen/qwen3.8-27b", prompt=1_000_000,
                                           cached=0, completion=0))
    calls = sorted(pg_ledger.fetch_calls(thread_id=thread), key=lambda c: c.turn_index)
    assert calls[0].chf == pytest.approx(0.00321)
    assert calls[0].cost_source == "router"
    assert calls[1].chf == pytest.approx(0.20 / 1.25)
    assert calls[1].cost_source == "catalog"


def test_aggregates_match_in_memory_semantics(pg_ledger):
    thread = f"{pg_ledger.test_prefix}-fil4"
    pg_ledger.record(thread, 0, make_call(chf=0.01, tier=Tier.SHIP))
    pg_ledger.record(thread, 0, make_call(chf=0.02, tier=Tier.SHIP))
    pg_ledger.record(thread, 1, make_call(chf=0.09, tier=Tier.DEEP))
    pg_ledger.record_verdict(thread, 1, make_verdict(decision=Decision.HITL))

    agg = pg_ledger.aggregates(thread_id=thread)
    assert agg.n_calls == 3
    assert agg.n_turns == 2
    assert agg.chf_total == pytest.approx(0.12)
    assert agg.chf_median_per_turn == pytest.approx(0.06)
    assert set(agg.by_tier) == {"ship", "deep"}
    assert agg.verdicts_by_decision == {"hitl": 1}


def test_fetch_calls_filters_by_day(pg_ledger):
    import datetime as dt

    thread = f"{pg_ledger.test_prefix}-fil5"
    pg_ledger.record(thread, 0, make_call())
    with pg_ledger._connect() as conn:
        conn.execute(
            "UPDATE ledger_entries SET created_at = created_at - INTERVAL '1 day' WHERE thread_id = %s",
            (thread,),
        )
    today = dt.datetime.now(dt.timezone.utc).date()
    assert pg_ledger.fetch_calls(thread_id=thread, day=today) == []
    assert len(pg_ledger.fetch_calls(thread_id=thread, day=today - dt.timedelta(days=1))) == 1
