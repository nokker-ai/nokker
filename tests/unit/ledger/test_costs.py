from __future__ import annotations

import pytest

from nokker.core.turn import Decision, Gate, LLMCall, Tier, Usage, Verdict
from nokker.ledger.costs import InMemoryLedger, cost_for_call
from nokker.ledger.prices import load_from_yaml


def make_call(*, tier=Tier.SHIP, model="qwen/qwen3.8-27b", prompt=1000, cached=0, completion=200,
              reasoning_tokens=0, cached_reported=False, chf=None, layer_tokens=None, latency_s=1.0,
              route_reason="défaut") -> LLMCall:
    return LLMCall(
        tier=tier, model=model, reasoning=False, prefix_sha256="deadbeef",
        layer_tokens=layer_tokens or {"prefix": 900, "block": 100},
        usage=Usage(prompt=prompt, completion=completion, cached=cached, reasoning=reasoning_tokens,
                    cached_reported=cached_reported),
        latency_s=latency_s, route_reason=route_reason, chf=chf,
    )


def make_verdict(*, gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="r1") -> Verdict:
    return Verdict(gate=gate, decision=decision, layer=layer, rule=rule)


# ───────────────────────── cost_for_call : priorité router > catalogue ─────────────────────────


def test_cost_for_call_prefers_router_chf_when_set():
    prices = load_from_yaml()
    call = make_call(chf=0.00123)
    chf, source = cost_for_call(call, prices)
    assert chf == 0.00123
    assert source == "router"


def test_cost_for_call_falls_back_to_catalog_when_chf_unset():
    prices = load_from_yaml()
    call = make_call(chf=None, model="qwen/qwen3.8-27b", prompt=1_000_000, cached=0, completion=0)
    chf, source = cost_for_call(call, prices)
    assert source == "catalog"
    assert chf == pytest.approx(0.20 / 1.25)  # prix d'entrée neuve, taux 1.25


def test_cost_for_call_subtracts_cached_from_prompt_for_new_tokens():
    prices = load_from_yaml()
    # prompt=1M dont 400k cachés -> 600k neufs à 0.20 + 400k cache à 0.05 (USD/M), converti en CHF
    call = make_call(chf=None, model="qwen/qwen3.8-27b", prompt=1_000_000, cached=400_000, completion=0,
                      cached_reported=True)
    chf, source = cost_for_call(call, prices)
    expected_usd = 600_000 * 0.20 / 1_000_000 + 400_000 * 0.05 / 1_000_000
    assert chf == pytest.approx(expected_usd / 1.25)


def test_cost_for_call_unknown_model_returns_zero_and_unknown_source():
    prices = load_from_yaml()
    call = make_call(chf=None, model="inconnu/modele-jamais-vu")
    chf, source = cost_for_call(call, prices)
    assert chf == 0.0
    assert source == "unknown"


# ───────────────────────── InMemoryLedger : record + fetch ─────────────────────────


def test_record_and_fetch_calls_roundtrip():
    ledger = InMemoryLedger(prices=load_from_yaml())
    ledger.record("fil-1", 0, make_call())
    ledger.record("fil-1", 1, make_call(tier=Tier.DEEP, model="qwen/qwen3.5-397b-a17b"))
    ledger.record("fil-2", 0, make_call())

    all_calls = ledger.fetch_calls()
    assert len(all_calls) == 3
    fil1 = ledger.fetch_calls(thread_id="fil-1")
    assert len(fil1) == 2
    deep_only = ledger.fetch_calls(tier="deep")
    assert len(deep_only) == 1
    assert deep_only[0].model == "qwen/qwen3.5-397b-a17b"


def test_record_verdict_and_fetch():
    ledger = InMemoryLedger(prices=load_from_yaml())
    ledger.record_verdict("fil-1", 0, make_verdict(decision=Decision.ALLOW))
    ledger.record_verdict("fil-1", 0, make_verdict(decision=Decision.ESCALATE, gate=Gate.SWITCH, layer=2))
    verdicts = ledger.fetch_verdicts(thread_id="fil-1")
    assert len(verdicts) == 2
    assert {v.decision for v in verdicts} == {"allow", "escalate"}


# ───────────────────────── Aggregates : A3, A5, T5 ─────────────────────────


def test_aggregates_tokens_transported_median_uses_layer_tokens_sum():
    ledger = InMemoryLedger(prices=load_from_yaml())
    # transportés = somme des couches : 1000 et 3000 -> médiane 2000
    ledger.record("f", 0, make_call(layer_tokens={"a": 1000}))
    ledger.record("f", 1, make_call(layer_tokens={"a": 1000, "b": 2000}))
    agg = ledger.aggregates()
    assert agg.tokens_transported_median == pytest.approx(2000.0)


def test_aggregates_cache_rate_none_when_never_reported():
    ledger = InMemoryLedger(prices=load_from_yaml())
    ledger.record("f", 0, make_call(cached_reported=False, prompt=1000, cached=0))
    agg = ledger.aggregates()
    assert agg.cache_rate is None  # A5 : jamais déduit à 0 % quand non mesuré
    assert agg.cache_measured_calls == 0


def test_aggregates_cache_rate_computed_only_on_measured_calls():
    ledger = InMemoryLedger(prices=load_from_yaml())
    ledger.record("f", 0, make_call(cached_reported=True, prompt=1000, cached=500))
    ledger.record("f", 1, make_call(cached_reported=False, prompt=1000, cached=0))  # ignoré du taux
    agg = ledger.aggregates()
    assert agg.cache_measured_calls == 1
    assert agg.cache_rate == pytest.approx(0.5)


def test_aggregates_chf_median_per_turn():
    ledger = InMemoryLedger(prices=load_from_yaml())
    ledger.record("f", 0, make_call(chf=0.01))
    ledger.record("f", 0, make_call(chf=0.02))  # même tour -> somme 0.03
    ledger.record("f", 1, make_call(chf=0.09))  # tour 2 -> 0.09
    agg = ledger.aggregates()
    assert agg.chf_total == pytest.approx(0.12)
    assert agg.chf_median_per_turn == pytest.approx(0.06)  # médiane(0.03, 0.09)
    assert agg.n_turns == 2
    assert agg.n_calls == 3


def test_aggregates_by_tier_and_verdicts_by_decision():
    ledger = InMemoryLedger(prices=load_from_yaml())
    ledger.record("f", 0, make_call(tier=Tier.SHIP, chf=0.01))
    ledger.record("f", 1, make_call(tier=Tier.DEEP, chf=0.05))
    ledger.record_verdict("f", 0, make_verdict(decision=Decision.ALLOW))
    ledger.record_verdict("f", 1, make_verdict(decision=Decision.ALLOW))
    ledger.record_verdict("f", 1, make_verdict(decision=Decision.HITL))
    agg = ledger.aggregates()
    assert set(agg.by_tier) == {"ship", "deep"}
    assert agg.by_tier["ship"].n_calls == 1
    assert agg.by_tier["deep"].chf_total == pytest.approx(0.05)
    assert agg.verdicts_by_decision == {"allow": 2, "hitl": 1}


def test_aggregates_filter_by_day():
    import datetime as dt

    ledger = InMemoryLedger(prices=load_from_yaml())
    ledger.record("f", 0, make_call())
    # Force une date d'hier sur le dernier enregistrement pour vérifier le filtre --day.
    yesterday = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    old = ledger._calls[-1]
    ledger._calls[-1] = old.__class__(**{**old.__dict__, "created_at": yesterday})
    ledger.record("f", 1, make_call())

    today = dt.datetime.now(dt.timezone.utc).date()
    agg_today = ledger.aggregates(day=today)
    assert agg_today.n_calls == 1
