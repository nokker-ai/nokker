import pytest

from nokker.api import server
from nokker.billing import http
from nokker.config import NokkerConfig, load_config


@pytest.fixture(autouse=True)
def _reset():
    yield
    http.configure(None)


def cfg(**kw):
    return NokkerConfig(billing_prices={"solo": "price_solo"}, app_url="https://app.nokker.ai", **kw)


def test_off_by_default_even_with_secrets(monkeypatch):
    monkeypatch.setenv("NOKKER_STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("NOKKER_STRIPE_WEBHOOK_SECRET", "whsec_x")
    server.configure_billing(cfg())
    assert http.service() is None


def test_enabled_needs_both_secrets(monkeypatch):
    monkeypatch.delenv("NOKKER_STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("NOKKER_STRIPE_SECRET_KEY", "sk_test_x")
    server.configure_billing(cfg(billing_enabled=True))
    assert http.service() is None
    monkeypatch.setenv("NOKKER_STRIPE_WEBHOOK_SECRET", "whsec_x")
    server.configure_billing(cfg(billing_enabled=True))
    assert http.service() is not None


def test_live_key_refused_unless_billing_live(monkeypatch):
    monkeypatch.setenv("NOKKER_STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("NOKKER_STRIPE_WEBHOOK_SECRET", "whsec_x")
    with pytest.raises(RuntimeError):
        server.configure_billing(cfg(billing_enabled=True))
    server.configure_billing(cfg(billing_enabled=True, billing_live=True))
    assert http.service() is not None


def test_yaml_keys_load(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("billing_enabled: true\nbilling_prices: {solo: price_1}\nbilling_plans: {solo: {deep_per_day: 40}}\n")
    c = load_config(str(p))
    assert c.billing_enabled and c.billing_prices == {"solo": "price_1"} and c.billing_plans["solo"]["deep_per_day"] == 40


def test_beta_coupon_key_is_empty_by_default_and_reaches_checkout(monkeypatch, tmp_path):
    assert NokkerConfig().billing_coupon == ""
    p = tmp_path / "c.yaml"
    p.write_text("billing_enabled: true\nbilling_prices: {solo: price_1}\nbilling_coupon: COUPON_TEST\n")
    assert load_config(str(p)).billing_coupon == "COUPON_TEST"
    monkeypatch.setenv("NOKKER_STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("NOKKER_STRIPE_WEBHOOK_SECRET", "whsec_x")
    server.configure_billing(cfg(billing_enabled=True, billing_coupon="COUPON_TEST"))
    assert http.service().coupon == "COUPON_TEST"
    server.configure_billing(cfg(billing_enabled=True))
    assert http.service().coupon == ""
