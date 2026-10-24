from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from nokker.ledger import prices


def test_load_from_yaml_default_file_has_known_models():
    cat = prices.load_from_yaml()
    assert cat.chf_usd_rate == pytest.approx(1.25)
    assert "qwen/qwen3.6-35b-a3b-eu" in cat.models()
    assert "local/magnitude" in cat.models()


def test_cost_usd_matches_catalog_prices():
    cat = prices.load_from_yaml()
    # qwen3.8-27b : 0.20 / 0.05 / 2.50 USD/M (catalog.json, 17.10.2026)
    usd = cat.cost_usd("qwen/qwen3.8-27b", new_tokens=1_000_000, cached_tokens=0, completion_tokens=0)
    assert usd == pytest.approx(0.20)
    usd_cached = cat.cost_usd("qwen/qwen3.8-27b", new_tokens=0, cached_tokens=1_000_000, completion_tokens=0)
    assert usd_cached == pytest.approx(0.05)
    usd_out = cat.cost_usd("qwen/qwen3.8-27b", new_tokens=0, cached_tokens=0, completion_tokens=1_000_000)
    assert usd_out == pytest.approx(2.50)


def test_cost_chf_uses_configured_rate():
    cat = prices.load_from_yaml()
    usd = cat.cost_usd("qwen/qwen3.8-27b", 1_000_000, 0, 0)
    chf = cat.cost_chf("qwen/qwen3.8-27b", 1_000_000, 0, 0)
    assert chf == pytest.approx(usd / 1.25)


def test_unknown_model_returns_none():
    cat = prices.load_from_yaml()
    assert cat.cost_usd("nobody/unknown-model", 100, 0, 0) is None
    assert cat.cost_chf("nobody/unknown-model", 100, 0, 0) is None
    assert cat.get("nobody/unknown-model") is None


def test_local_magnitude_is_free():
    cat = prices.load_from_yaml()
    assert cat.cost_usd("local/magnitude", 10_000, 500, 2_000) == 0.0


def _fake_router_response(payload: dict) -> io.BytesIO:
    buf = io.BytesIO(json.dumps(payload).encode("utf-8"))
    return buf


def test_load_from_router_parses_pricing_field():
    payload = {
        "object": "list",
        "data": [
            {"id": "qwen/qwen3.8-27b", "pricing": {"currency": "USD", "unit": "1M tokens",
                                                     "prompt": "0.20", "cached": "0.05", "completion": "2.50"}},
            {"id": "no-pricing/model"},  # doit être ignoré, pas planter
        ],
    }
    with patch("urllib.request.urlopen") as m:
        m.return_value.__enter__.return_value = _fake_router_response(payload)
        cat = prices.load_from_router("https://router.example.invalid")
    assert cat.models() == ["qwen/qwen3.8-27b"]
    assert cat.cost_usd("qwen/qwen3.8-27b", 1_000_000, 0, 0) == pytest.approx(0.20)
    assert cat.source.startswith("router:")


def test_load_from_router_raises_on_empty_catalog():
    payload = {"object": "list", "data": []}
    with patch("urllib.request.urlopen") as m:
        m.return_value.__enter__.return_value = _fake_router_response(payload)
        with pytest.raises(ValueError):
            prices.load_from_router("https://router.example.invalid")


def test_load_prefers_router_but_falls_back_on_network_error():
    with patch("nokker.ledger.prices.load_from_router", side_effect=OSError("pas de réseau dans ce test")):
        cat = prices.load(prefer_router=True)
    assert cat.source.startswith("yaml:")
    assert "qwen/qwen3.8-27b" in cat.models()


def test_load_without_prefer_router_never_calls_network():
    with patch("nokker.ledger.prices.load_from_router") as m:
        prices.load(prefer_router=False)
    m.assert_not_called()
