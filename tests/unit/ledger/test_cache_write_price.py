"""Prix d'écriture de cache (`cache_write`, optionnel) : jetons écrits en cache facturés à leur prix quand l'entrée en
porte un ; sans ce champ, coût inchangé (les écritures restent au prix d'entrée)."""
from __future__ import annotations

import textwrap

import pytest

from nokker.core.turn import LLMCall, Tier, Usage
from nokker.ledger.costs import InMemoryLedger, cost_for_call
from nokker.ledger.prices import PriceCatalog, PriceEntry, load_from_yaml
from nokker.ledger.turn_cost import row_from_call, summarize

M = "acme/m1"


def _catalog(write: float | None, write_1h: float | None = None) -> PriceCatalog:
    entry = PriceEntry(model=M, prompt_usd_per_m=3.0, cached_usd_per_m=0.3, completion_usd_per_m=15.0, source="test",
                       cache_write_usd_per_m=write, cache_write_1h_usd_per_m=write_1h)
    return PriceCatalog({M: entry}, chf_usd_rate=1.0)


def _call(write: int, *, prompt: int = 10_000, cached: int = 4_000, completion: int = 1_000,
          ttl: str = "5m") -> LLMCall:
    return LLMCall(tier=Tier.SHIP, model=M, reasoning=False, prefix_sha256="x", layer_tokens={},
                   usage=Usage(prompt=prompt, completion=completion, cached=cached, cached_reported=True,
                               cache_write=write, cache_ttl=ttl),
                   latency_s=1.0, route_reason="test")


def test_write_price_applies_to_written_tokens_only():
    # 3 000 écrits à 3,75, 3 000 neufs à 3, 4 000 lus à 0,3, 1 000 sortis à 15 (USD/M)
    chf, source = cost_for_call(_call(3_000), _catalog(3.75))
    assert source == "catalog"
    assert chf == pytest.approx((3_000 * 3.75 + 3_000 * 3.0 + 4_000 * 0.3 + 1_000 * 15.0) / 1e6)


def test_no_write_price_keeps_the_previous_cost():
    with_writes, _ = cost_for_call(_call(3_000), _catalog(None))
    without, _ = cost_for_call(_call(0), _catalog(None))
    assert with_writes == without == pytest.approx((6_000 * 3.0 + 4_000 * 0.3 + 1_000 * 15.0) / 1e6)


def test_write_price_without_written_tokens_changes_nothing():
    assert cost_for_call(_call(0), _catalog(3.75)) == cost_for_call(_call(0), _catalog(None))


def test_written_tokens_never_exceed_the_non_cached_input():
    chf, _ = cost_for_call(_call(99_999), _catalog(3.75))  # compte incohérent de l'amont : borné à l'entrée neuve
    assert chf == pytest.approx((6_000 * 3.75 + 4_000 * 0.3 + 1_000 * 15.0) / 1e6)


def test_ledger_and_turn_cost_use_the_write_price():
    prices = _catalog(3.75)
    ledger = InMemoryLedger(prices)
    ledger.record("t1", 0, _call(3_000))
    rec = ledger.fetch_calls(thread_id="t1")[0]
    assert rec.cache_write_tokens == 3_000 and rec.chf == pytest.approx(cost_for_call(_call(3_000), prices)[0])
    got = summarize([row_from_call(_call(3_000))], prices)["cost"]
    assert got["USD"] == pytest.approx((3_000 * 3.75 + 3_000 * 3.0 + 4_000 * 0.3 + 1_000 * 15.0) / 1e6, abs=1e-6)


def test_yaml_cache_write_field_is_optional(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text(textwrap.dedent("""
        models:
          - {id: a/with, prompt: "3", cached: "0.3", completion: "15", cache_write: "3.75"}
          - {id: a/without, prompt: "3", cached: "0.3", completion: "15"}
    """), encoding="utf-8")
    cat = load_from_yaml(f)
    assert cat.get("a/with").cache_write_usd_per_m == 3.75 and cat.get("a/without").cache_write_usd_per_m is None


def test_shipped_grid_is_unchanged():
    assert all(load_from_yaml().get(m).cache_write_usd_per_m is None for m in load_from_yaml().models())


def _expected(price: float) -> float:
    return (3_000 * price + 3_000 * 3.0 + 4_000 * 0.3 + 1_000 * 15.0) / 1e6


def test_one_hour_cache_uses_the_one_hour_price_when_there_is_one():
    assert cost_for_call(_call(3_000, ttl="1h"), _catalog(3.75, 6.0))[0] == pytest.approx(_expected(6.0))
    assert cost_for_call(_call(3_000, ttl="5m"), _catalog(3.75, 6.0))[0] == pytest.approx(_expected(3.75))
    assert cost_for_call(_call(3_000), _catalog(3.75, 6.0))[0] == pytest.approx(_expected(3.75))  # défaut : 5 min


def test_one_hour_cache_falls_back_to_the_five_minute_price_then_to_the_input_price():
    assert cost_for_call(_call(3_000, ttl="1h"), _catalog(3.75))[0] == pytest.approx(_expected(3.75))
    assert cost_for_call(_call(3_000, ttl="1h"), _catalog(None))[0] == pytest.approx(_expected(3.0))
    # un tarif 1 h seul ne s'applique pas à un cache de 5 minutes : repli sur le prix d'entrée
    assert cost_for_call(_call(3_000, ttl="5m"), _catalog(None, 6.0))[0] == pytest.approx(_expected(3.0))


def test_ttl_reaches_the_record_turn_cost_and_the_turn_json_without_changing_old_json():
    from nokker.store.pg import _dec_usage, _enc_usage

    prices = _catalog(3.75, 6.0)
    ledger = InMemoryLedger(prices)
    ledger.record("t1", 0, _call(3_000, ttl="1h"))
    rec = ledger.fetch_calls(thread_id="t1")[0]
    assert rec.cache_ttl == "1h" and rec.chf == pytest.approx(_expected(6.0))
    assert summarize([row_from_call(_call(3_000, ttl="1h"))], prices)["cost"]["USD"] == pytest.approx(_expected(6.0), abs=1e-6)
    u = Usage(prompt=10, cache_write=5, cache_ttl="1h")
    assert _enc_usage(u)["cache_ttl"] == "1h" and _dec_usage(_enc_usage(u)) == u
    assert "cache_ttl" not in _enc_usage(Usage(prompt=10, cache_write=5))  # 5m : clé non écrite
    assert _dec_usage({"prompt": 1}).cache_ttl == "5m"


def test_yaml_and_router_one_hour_price_are_optional(tmp_path):
    from nokker.ledger.prices import _entries_from_models

    f = tmp_path / "p.yaml"
    f.write_text('models:\n  - {id: a/x, prompt: "3", cached: "0.3", completion: "15", cache_write: "3.75", '
                 'cache_write_1h: "6"}\n  - {id: a/y, prompt: "3", cached: "0.3", completion: "15"}\n', encoding="utf-8")
    cat = load_from_yaml(f)
    assert cat.get("a/x").cache_write_1h_usd_per_m == 6.0 and cat.get("a/y").cache_write_1h_usd_per_m is None
    got = _entries_from_models([{"id": "m", "pricing": {"prompt": 1, "cached": 0.1, "completion": 2, "cache_write_1h": 2}}],
                               "pricing", "r:{model}")
    assert got["m"].cache_write_1h_usd_per_m == 2.0 and got["m"].cache_write_usd_per_m is None
