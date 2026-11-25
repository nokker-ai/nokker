"""Identifiant d'appel posé par le noyau : en-tête `X-Nokker-Call-Id` seulement si le palier le demande, ledger dans tous
les cas, un identifiant par appel (un repli sur un autre palier est un autre appel)."""
from __future__ import annotations

import re

import httpx
import pytest
import yaml

from nokker.config import TierConfig, load_config
from nokker.core.turn import LLMCall, Tier, TurnInput, Usage, new_call_id
from nokker.ledger.costs import InMemoryLedger
from nokker.ledger.prices import load_from_yaml
from nokker.providers.anthropic import AnthropicProvider
from nokker.providers.hedged import HedgedProvider
from nokker.providers.select import tier_provider
from nokker.providers.sokkan import SokkanProvider

from .. import test_loop_contract as loop
from .anthropic_upstream import MODEL, FakeAnthropic, message

ID = re.compile(r"^nc_[0-9a-f]{16}$")
HEADER = "x-nokker-call-id"
OK = {"model": "m", "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
      "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
MSGS = [{"role": "user", "content": "ping"}]


def _sokkan(seen: list, *, status: list[int] | None = None, **kw) -> SokkanProvider:
    codes = list(status or [])

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append({k.lower(): v for k, v in req.headers.items()})
        return httpx.Response(codes.pop(0) if codes else 200, json=OK)

    p = SokkanProvider(base_url="https://amont.invalid/v1", api_key="k", model="m", retry_backoff_s=0.0, **kw)
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    return p


def test_ids_have_the_documented_shape_and_are_unique():
    ids = {new_call_id() for _ in range(2000)}
    assert len(ids) == 2000 and all(ID.match(i) for i in ids)
    a, b = (LLMCall(tier=Tier.SHIP, model="m", reasoning=False, prefix_sha256="", layer_tokens={}, usage=Usage(),
                    latency_s=0.0, route_reason="t") for _ in range(2))
    assert ID.match(a.call_id) and a.call_id != b.call_id


def test_header_is_off_by_default_but_the_completion_still_carries_the_id():
    seen: list = []
    comp = _sokkan(seen).complete(MSGS, [], reasoning=False, session_key="th-secret")
    assert HEADER not in seen[0] and ID.match(comp.call_id)
    assert SokkanProvider(base_url="u", api_key="", model="m").send_call_id is False


def test_header_sent_only_when_asked_and_never_carries_thread_or_person():
    seen: list = []
    p = _sokkan(seen, send_call_id=True)
    c1 = p.complete(MSGS, [], reasoning=False, session_key="th-secret")
    c2 = p.complete(MSGS, [], reasoning=False, session_key="th-secret")
    assert [s[HEADER] for s in seen] == [c1.call_id, c2.call_id] and c1.call_id != c2.call_id
    assert ID.match(seen[0][HEADER]) and "th-secret" not in seen[0][HEADER]


def test_http_retry_of_one_call_keeps_its_id():
    seen: list = []
    comp = _sokkan(seen, status=[500], send_call_id=True).complete(MSGS, [], reasoning=False, session_key="s")
    assert len(seen) == 2 and seen[0][HEADER] == seen[1][HEADER] == comp.call_id


def test_fallback_to_another_tier_is_another_call_with_its_own_id():
    seen_a: list = []
    seen_b: list = []
    inner = _sokkan(seen_a, status=[400], send_call_id=True)  # 400 : jamais rejoué, le repli prend la main

    class Primary:  # sans `detached` : HedgedProvider l'appelle tel quel (son client de test est gardé)
        model = "m"

        def complete(self, *a, **kw):
            return inner.complete(*a, **kw)

    primary = Primary()
    fallback = _sokkan(seen_b, send_call_id=True)
    comp = HedgedProvider(primary, fallback, hedge_after_s=30.0).complete(MSGS, [], reasoning=False, session_key="s")
    assert seen_a[0][HEADER] != seen_b[0][HEADER]
    assert comp.call_id == seen_b[0][HEADER]


def test_anthropic_header_follows_the_same_rule():
    with FakeAnthropic() as up:
        for _ in range(3):
            up.reply(message([{"type": "text", "text": "ok"}]))
        off = AnthropicProvider(base_url=up.url, api_key="sk-ant-faux", model=MODEL, retry_backoff_s=0.0)
        on = AnthropicProvider(base_url=up.url, api_key="sk-ant-faux", model=MODEL, retry_backoff_s=0.0,
                               send_call_id=True)
        c0 = off.complete(MSGS, [], reasoning=False, session_key="th-secret")
        c1 = on.complete(MSGS, [], reasoning=False, session_key="th-secret")
        c2 = on.complete(MSGS, [], reasoning=False, session_key="th-secret")
        h = [r["headers"] for r in up.requests]
    assert HEADER not in h[0] and ID.match(c0.call_id)
    assert (h[1][HEADER], h[2][HEADER]) == (c1.call_id, c2.call_id) and c1.call_id != c2.call_id


def test_config_flag_defaults_to_false_is_strict_and_reaches_the_provider(tmp_path):
    def cfg(extra):
        path = tmp_path / "c.yaml"
        path.write_text(yaml.safe_dump({"tiers": {"ship": {"base_url": "https://r.example/v1", "model": "m", **extra}}}),
                        encoding="utf-8")
        return load_config(str(path)).tiers["ship"]

    assert TierConfig(base_url="u", model="m").send_call_id is False and cfg({}).send_call_id is False
    on = cfg({"send_call_id": True})
    assert on.send_call_id is True and tier_provider(on).send_call_id is True
    assert tier_provider(cfg({})).send_call_id is False
    assert tier_provider(cfg({"send_call_id": True, "format": "anthropic"})).send_call_id is True
    with pytest.raises(ValueError, match="send_call_id"):
        cfg({"send_call_id": "false"})


def test_ledger_records_the_call_id_of_each_model_call_of_a_turn():
    with FakeAnthropic() as up:
        up.reply(message([{"type": "tool_use", "id": "toolu_01A", "name": "calendar_list", "input": {}}],
                         stop_reason="tool_use"))
        up.reply(message([{"type": "text", "text": "Rien de prévu."}]))
        k, _tools, _ship, _deep = loop.kernel([])
        k.ledger = InMemoryLedger(load_from_yaml())
        k.providers[Tier.SHIP] = AnthropicProvider(base_url=up.url, api_key="sk-ant-faux", model=MODEL,
                                                   retry_backoff_s=0.0, send_call_id=True)
        k.run_turn(TurnInput("t1", "mon agenda ?"))
        sent = [r["headers"][HEADER] for r in up.requests]
    rows = sorted(k.ledger.fetch_calls(thread_id="t1"), key=lambda r: r.turn_index)
    assert len(sent) == 2 and [r.call_id for r in rows] == sent and len(set(sent)) == 2
    assert all(ID.match(i) for i in sent)


def test_ledger_has_an_id_even_without_header_or_provider_id():
    ledger = InMemoryLedger(load_from_yaml())
    call = LLMCall(tier=Tier.SHIP, model="m", reasoning=False, prefix_sha256="", layer_tokens={}, usage=Usage(),
                   latency_s=0.0, route_reason="t")
    ledger.record("t1", 0, call)
    assert ledger.fetch_calls(thread_id="t1")[0].call_id == call.call_id and ID.match(call.call_id)


# ───────────── cohabitation avec les champs de palier du lot précédent (délais, durée du cache, effort) ─────────────


def _tier(tmp_path, **extra):
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"tiers": {"ship": {
        "base_url": "https://r.example/v1", "model": "claude-test-1", "format": "anthropic", "cache": "prefix", **extra}}}),
        encoding="utf-8")
    return load_config(str(path)).tiers["ship"]


def test_tier_fields_of_both_sets_coexist(tmp_path):
    cfg = _tier(tmp_path, timeout_s=90, total_budget_s=40, cache_ttl="1h", effort="low", send_call_id=True,
                reasoning_on_body={"thinking": {"type": "adaptive"}})
    assert (cfg.timeout_s, cfg.total_budget_s, cfg.cache_ttl, cfg.effort, cfg.send_call_id) == (90.0, 40.0, "1h", "low", True)
    p = tier_provider(cfg)
    assert (p.timeout_s, p.total_budget_s, p.cache_ttl, p.effort, p.send_call_id) == (90.0, 40.0, "1h", "low", True)
    assert p.reasoning_on_body == {"thinking": {"type": "adaptive"}}
    d = _tier(tmp_path)
    assert (d.timeout_s, d.total_budget_s, d.cache_ttl, d.effort, d.send_call_id) == (180.0, 80.0, "", "", False)


def test_call_id_header_and_one_hour_cache_point_share_one_request_and_price_the_write(tmp_path):
    from nokker.ledger.prices import PriceCatalog, PriceEntry

    prices = PriceCatalog({MODEL_PRICED: PriceEntry(MODEL_PRICED, 3.0, 0.3, 15.0, "t", cache_write_usd_per_m=3.75,
                                                    cache_write_1h_usd_per_m=6.0)}, chf_usd_rate=1.0)
    usage = {"input_tokens": 100, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 0, "output_tokens": 10}
    costs = {}
    for ttl in ("1h", "5m", ""):
        with FakeAnthropic() as up:
            up.reply(message([{"type": "text", "text": "ok"}], usage=usage, model=MODEL_PRICED))
            p = tier_provider(_tier(tmp_path, cache_ttl=ttl, send_call_id=True, model="x"), model=MODEL_PRICED)
            p.base_url, p.api_key = up.url, "sk-ant-faux"
            comp = p.complete([{"role": "system", "content": "s"}, {"role": "user", "content": "q"}], [],
                              reasoning=False, session_key="th")
            req = up.requests[0]
        assert req["headers"][HEADER] == comp.call_id and ID.match(comp.call_id)
        assert ("1h" in str(req["body"])) == (ttl == "1h")  # la durée part dans les points de cache, l'en-tête reste
        assert comp.usage.cache_ttl == ("1h" if ttl == "1h" else "5m")
        ledger = InMemoryLedger(prices)
        call = LLMCall(tier=Tier.SHIP, model=MODEL_PRICED, reasoning=False, prefix_sha256="", layer_tokens={},
                       usage=comp.usage, latency_s=0.1, route_reason="t", call_id=comp.call_id)
        ledger.record("t1", 0, call)
        rec = ledger.fetch_calls(thread_id="t1")[0]
        assert (rec.cache_ttl, rec.call_id) == (comp.usage.cache_ttl, comp.call_id)
        costs[ttl] = rec.chf
    base = (100 * 3.0 + 10 * 15.0) / 1e6
    assert costs["1h"] == pytest.approx(base + 1000 * 6.0 / 1e6)
    assert costs["5m"] == costs[""] == pytest.approx(base + 1000 * 3.75 / 1e6)  # vide ou 5m : prix 5 min


def test_without_write_prices_the_cost_does_not_change_with_the_ttl():
    from nokker.ledger.prices import PriceCatalog, PriceEntry

    prices = PriceCatalog({"m": PriceEntry("m", 3.0, 0.3, 15.0, "t")}, chf_usd_rate=1.0)
    from nokker.ledger.costs import cost_for_call

    def c(ttl):
        return cost_for_call(LLMCall(tier=Tier.SHIP, model="m", reasoning=False, prefix_sha256="", layer_tokens={},
                                     usage=Usage(prompt=1100, completion=10, cache_write=1000, cache_ttl=ttl),
                                     latency_s=0.0, route_reason="t"), prices)
    assert c("1h") == c("5m")


MODEL_PRICED = "claude-priced-1"


def test_gate_headers_and_call_id_header_share_one_request():
    from nokker.providers import gate

    gate.registry()._by_thread.clear()
    gate.registry().set("th-gate", {"X-Gate-Session": "sess-1"})
    try:
        for proto in ("openai", "anthropic"):
            if proto == "openai":
                seen: list = []
                p = _sokkan(seen, send_call_id=True, gate=True)
                comp = p.complete(MSGS, [], reasoning=False, session_key="th-gate")
                h = seen[0]
            else:
                with FakeAnthropic() as up:
                    up.reply(message([{"type": "text", "text": "ok"}]))
                    p = AnthropicProvider(base_url=up.url, api_key="k", model=MODEL, send_call_id=True, gate=True)
                    comp = p.complete(MSGS, [], reasoning=False, session_key="th-gate")
                    h = up.requests[0]["headers"]
            assert h[HEADER] == comp.call_id and h["x-gate-session"] == "sess-1"
        off: list = []
        _sokkan(off, send_call_id=True).complete(MSGS, [], reasoning=False, session_key="th-gate")
        assert "x-gate-session" not in off[0] and HEADER in off[0]  # palier sans gate : seul l'identifiant d'appel part
    finally:
        gate.registry()._by_thread.clear()


def test_gate_flag_and_call_id_flag_coexist_in_one_tier(tmp_path):
    cfg = _tier(tmp_path, gate=True, send_call_id=True, timeout_s=60, cache_ttl="1h", effort="low")
    assert (cfg.gate, cfg.send_call_id, cfg.timeout_s, cfg.cache_ttl, cfg.effort) == (True, True, 60.0, "1h", "low")
    with pytest.raises(ValueError, match="gate"):
        _tier(tmp_path, gate="false")
    p = tier_provider(cfg, gate=cfg.gate)
    assert p.gate is True and p.send_call_id is True
