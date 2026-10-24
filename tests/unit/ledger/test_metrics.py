from __future__ import annotations

from nokker.core.turn import Decision, Gate, Tier
from nokker.ledger import metrics
from nokker.ledger.costs import InMemoryLedger
from nokker.ledger.prices import load_from_yaml
from tests.unit.ledger.test_costs import make_call, make_verdict


def test_render_is_valid_prometheus_text_exposition():
    ledger = InMemoryLedger(prices=load_from_yaml())
    ledger.record("f", 0, make_call(tier=Tier.SHIP, model="qwen/qwen3.8-27b", chf=0.01, latency_s=1.2))
    ledger.record("f", 1, make_call(tier=Tier.DEEP, model="qwen/qwen3.5-397b-a17b", chf=0.09, latency_s=4.5))
    ledger.record_verdict("f", 0, make_verdict(decision=Decision.ALLOW))
    ledger.record_verdict("f", 1, make_verdict(decision=Decision.ESCALATE, gate=Gate.SWITCH, layer=2))

    text = metrics.render(ledger)

    assert text.endswith("\n")
    for line in text.splitlines():
        assert line.startswith("#") or " " in line  # chaque ligne de métrique a au moins un espace (nom valeur)

    assert 'nokker_ledger_calls_total{tier="ship",model="qwen/qwen3.8-27b"} 1' in text
    assert 'nokker_ledger_calls_total{tier="deep",model="qwen/qwen3.5-397b-a17b"} 1' in text
    assert "nokker_ledger_chf_total" in text
    assert "nokker_ledger_call_latency_seconds_bucket" in text
    assert "nokker_ledger_call_latency_seconds_count 2" in text
    assert 'nokker_ledger_verdicts_total{gate="action",decision="allow"} 1' in text
    assert 'nokker_ledger_verdicts_total{gate="switch",decision="escalate"} 1' in text


def test_render_on_empty_ledger_does_not_crash():
    ledger = InMemoryLedger(prices=load_from_yaml())
    text = metrics.render(ledger)
    assert "nokker_ledger_call_latency_seconds_count 0" in text


def test_histogram_buckets_are_cumulative():
    ledger = InMemoryLedger(prices=load_from_yaml())
    ledger.record("f", 0, make_call(latency_s=0.2))
    ledger.record("f", 1, make_call(latency_s=20.0))
    text = metrics.render(ledger)
    lines = {line.split(" ")[0]: int(line.split(" ")[1]) for line in text.splitlines()
              if line.startswith("nokker_ledger_call_latency_seconds_bucket")}
    assert lines['nokker_ledger_call_latency_seconds_bucket{le="0.5"}'] == 1
    assert lines['nokker_ledger_call_latency_seconds_bucket{le="+Inf"}'] == 2
