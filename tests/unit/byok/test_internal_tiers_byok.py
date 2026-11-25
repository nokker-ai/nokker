"""Paliers INTERNES sous BYOK : quand la base du palier est celle du point d'accès BYOK de la personne, il sert avec SA
clé (jamais par le lien Nokker : quota de compte, données chez un tiers) ; sur une autre base, sa clé n'y part jamais ;
modèle du palier absent chez elle : le modèle BYOK, raisonnement coupé ; échec : événement hôte `<module>.failed
{reason}` à classe fermée, repli inchangé."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from nokker.api.server import (_watch_internal_tiers, build_kernel, configure_guide, default_model_applier,
                               internal_tiers)
from nokker.config import load_config
from nokker.core.internal_tiers import failure_reason
from nokker.core.turn import Tier
from nokker.guide import http as guide_http
from nokker.local.setup import ROUTER_URL

SHIPPED = Path(__file__).resolve().parents[3] / "deploy" / "lokkal" / "core-config.yaml"
INFERENCE = "https://app.nokker.ai/v1/inference"
LINK_KEY = "rk-LINK-0123456789abcdef"
BYOK_KEY = "sk-" + "BYOK-" + "0123456789" + "abcdef"
OTHER = "https://byok.example/v1"
MODULES = {"gate", "judge", "advice", "auto_review", "outcomes", "classifier", "guide"}


class Hub:
    def __init__(self):
        self.events: list[tuple[str, dict, str | None]] = []

    def host_event(self, name, data, thread_id=None):
        self.events.append((name, dict(data), thread_id))


@pytest.fixture
def world(monkeypatch):
    monkeypatch.delenv("ROUTER_KEY", raising=False)
    cfg = load_config(str(SHIPPED))
    cfg.pg_dsn_env = "TEST_NOKKER_ABSENT_DSN_VAR"
    cfg.mcp_tool_servers = []
    cfg.judge_enabled = True  # portes du juge allumées : le juge des portes existe
    cfg.route = {**(cfg.route or {}), "classifier": "model"}
    cfg.outcomes_enabled = True
    cfg.auto_review = {"enabled": True, "tier": "judge"}
    kernel = build_kernel(cfg)
    configure_guide(cfg, kernel)
    yield cfg, kernel
    guide_http.configure(None)


def apply(world, base, key, model="byok-model", models=None, link=(INFERENCE, LINK_KEY)):
    cfg, kernel = world
    default_model_applier(cfg, kernel, lambda: link, models_fn=lambda: models)(base, key, model)
    return cfg, kernel


def tiers(world):
    cfg, kernel = world
    return {m: (t, c, j) for m, t, c, j in internal_tiers(cfg, kernel)}


def test_every_internal_tier_is_listed(world):
    assert set(tiers(world)) == MODULES


@pytest.mark.parametrize("module", sorted(MODULES))
def test_byok_same_base_uses_the_byok_key_never_the_link(world, module):
    cfg, _k = apply(world, ROUTER_URL, BYOK_KEY, models=["qwen/qwen3.5-397b-a17b", "qwen/qwen3.6-35b-a3b-eu",
                                                           "qwen/qwen3.8-27b"])
    t, c, _j = tiers(world)[module]
    assert (t.base_url, t.api_key) == (ROUTER_URL, BYOK_KEY)
    assert t.model == c.model  # le modèle du palier existe chez la personne : gardé


@pytest.mark.parametrize("module", sorted(MODULES))
def test_byok_same_base_model_absent_takes_the_byok_model(world, module):
    _cfg, _k = apply(world, ROUTER_URL, BYOK_KEY, models=["byok-model"])
    t, _c, _j = tiers(world)[module]
    assert (t.base_url, t.api_key, t.model) == (ROUTER_URL, BYOK_KEY, "byok-model")


def test_unknown_catalog_small_tiers_take_the_byok_model_but_the_review_judge_keeps_its_own(world):
    apply(world, ROUTER_URL, BYOK_KEY, models=None)
    got = tiers(world)
    for module in ("classifier", "guide"):
        assert got[module][0].model == "byok-model"
    for module in ("judge", "advice", "gate", "auto_review", "outcomes"):  # juge : qualité inchangée sans preuve
        t, c, _ = got[module]
        assert (t.api_key, t.model) == (BYOK_KEY, c.model)
    _cfg, kernel = world
    p = kernel.providers[Tier.SMALL]
    assert (p.base_url, p.api_key, p.model) == (ROUTER_URL, BYOK_KEY, "byok-model")
    assert p.reasoning_off_body == {"reasoning_effort": "none"} and p.vision is False


def test_small_provider_keeps_its_model_when_the_catalog_has_it(world):
    cfg, kernel = apply(world, ROUTER_URL, BYOK_KEY, models=["qwen/qwen3.8-27b"])
    p = kernel.providers[Tier.SMALL]
    assert (p.base_url, p.api_key, p.model) == (ROUTER_URL, BYOK_KEY, cfg.tiers["small"].model)


@pytest.mark.parametrize("module", sorted(MODULES))
def test_byok_other_base_goes_through_the_link_and_the_byok_key_never_follows(world, module):
    apply(world, OTHER, BYOK_KEY, models=["byok-model"])
    t, c, _j = tiers(world)[module]
    assert (t.base_url, t.api_key, t.model) == (INFERENCE, LINK_KEY, c.model)


def test_the_byok_key_only_ever_reaches_the_byok_base(world):
    for base in (ROUTER_URL, OTHER):
        apply(world, base, BYOK_KEY, models=["byok-model"])
        cfg, kernel = world
        everything = [(t.base_url, t.api_key) for _m, t, _c, _j in internal_tiers(cfg, kernel)]
        for tier, p in kernel.providers.items():
            if tier is Tier.SMALL:
                everything.append((p.base_url, p.api_key))
        assert all(b.rstrip("/") == base for b, k in everything if k == BYOK_KEY)


def test_the_link_key_never_reaches_the_byok_base_when_the_base_is_the_same(world):
    apply(world, ROUTER_URL, BYOK_KEY, models=["byok-model"])
    cfg, kernel = world
    for _m, t, _c, _j in internal_tiers(cfg, kernel):
        assert t.api_key != LINK_KEY
    assert kernel.providers[Tier.SMALL].api_key != LINK_KEY


def test_no_byok_the_link_is_unchanged(world):
    cfg, kernel = apply(world, INFERENCE, LINK_KEY, model="")
    p = kernel.providers[Tier.SMALL]
    assert (p.base_url, p.api_key, p.model) == (INFERENCE, LINK_KEY, cfg.tiers["small"].model)
    for module, (t, c, _j) in tiers(world).items():
        assert (t.base_url, t.api_key) == (INFERENCE, LINK_KEY) and t.model == c.model, module


def test_byok_without_any_link_serves_the_tiers_on_the_same_base(world):
    cfg, kernel = world
    default_model_applier(cfg, kernel, lambda: None, models_fn=lambda: ["byok-model"])(ROUTER_URL, BYOK_KEY,
                                                                                       "byok-model")
    for module, (t, _c, _j) in tiers(world).items():
        assert (t.base_url, t.api_key) == (ROUTER_URL, BYOK_KEY), module


# ── échec : événement hôte à classe fermée ──
def _quota(request: httpx.Request) -> httpx.Response:
    return httpx.Response(403, json={"error": {"code": "quota_exceeded", "message": "150 messages par jour",
                                               "base": str(request.url)}})


@pytest.mark.parametrize("module", sorted(MODULES))
def test_a_failing_internal_tier_publishes_its_closed_event_and_still_raises(world, caplog, module):
    cfg, kernel = apply(world, ROUTER_URL, BYOK_KEY, models=["byok-model"])
    hub = Hub()
    _watch_internal_tiers(cfg, kernel, hub)
    t, _c, _j = tiers(world)[module]
    inner = t.primary if hasattr(t, "primary") else t
    inner._client = httpx.Client(transport=httpx.MockTransport(_quota))
    inner.retry_wait_s = 0
    caplog.set_level(logging.WARNING)
    with pytest.raises(RuntimeError):
        t.post({"messages": [{"role": "user", "content": "x"}], "max_tokens": 3})
    assert hub.events == [(f"{module}.failed", {"reason": "quota exceeded (HTTP 403)"}, None)]
    blob = json.dumps(hub.events) + caplog.text
    for secret in (BYOK_KEY, LINK_KEY, "router.sokkan.ch", "byok.example", "150 messages"):
        assert secret not in blob


@pytest.mark.parametrize("exc,reason", [
    (RuntimeError("HTTP 403 juge (m): quota_exceeded"), "quota exceeded (HTTP 403)"),
    (RuntimeError("HTTP 403 juge (m): forbidden"), "forbidden (HTTP 403)"),
    (RuntimeError("HTTP 429 juge"), "rate limited (HTTP 429)"),
    (RuntimeError("HTTP 401 juge"), "unauthorized (HTTP 401)"),
    (RuntimeError("HTTP 502 juge"), "service unavailable (HTTP 502)"),
    (RuntimeError("HTTP 500 juge"), "server error (HTTP 500)"),
    (TimeoutError("https://secret.example/x?token=abc"), "timeout"),
    (httpx.ReadTimeout("boom"), "timeout"),
    (SimpleNamespace(code="no_tier"), "no small tier"),
    (ValueError("https://secret.example Bearer sk-1"), "error"),
])
def test_failure_reason_is_a_closed_class(exc, reason):
    if isinstance(exc, SimpleNamespace):
        class E(Exception):
            code = "no_tier"
        exc = E("aucun modèle")
    assert failure_reason(exc) == reason
