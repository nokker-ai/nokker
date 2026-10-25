"""Coût par tour (X2, docs/LOKKAL.md §7.8) : `turn.cost`, totaux de session et du jour, `GET /v1/local/cost`."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

import pytest

from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.core.turn import InputCheck, LLMCall, Tier, Turn, Usage
from nokker.ledger import prices
from nokker.ledger.costs import InMemoryLedger
from nokker.ledger.turn_cost import ledger_totals, row_from_call, summarize, turn_cost
from nokker.providers.sokkan import _parse_usage
from nokker.threads.memory import InMemoryThreads

OVH = "Qwen3.5-397B-A17B"


def _call(model: str, prompt: int, completion: int, *, cached: int = 0, cached_reported: bool = False,
          reasoning: int = 0, reasoning_reported: bool = False, chf: float | None = None,
          tier: Tier = Tier.DEEP) -> LLMCall:
    return LLMCall(tier=tier, model=model, reasoning=True, prefix_sha256="", layer_tokens={},
                   usage=Usage(prompt=prompt, completion=completion, cached=cached, reasoning=reasoning,
                               cached_reported=cached_reported, reasoning_reported=reasoning_reported),
                   latency_s=1.0, route_reason="test", chf=chf)


@pytest.fixture
def cat():
    return prices.load_from_yaml()


def test_catalog_has_ovh_397b_in_eur_without_cache_discount(cat):
    entry = cat.get(OVH)
    assert entry is not None and entry.currency == "EUR"
    assert (entry.prompt_usd_per_m, entry.cached_usd_per_m, entry.completion_usd_per_m) == (0.60, 0.60, 3.60)
    assert cat.cost(OVH, 1_000_000, 0, 1_000_000) == (pytest.approx(4.20), "EUR")
    # Pas de taux EUR dans le catalogue : jamais converti en USD ni en CHF.
    assert cat.cost_usd(OVH, 1_000_000, 0, 0) is None and cat.cost_chf(OVH, 1_000_000, 0, 0) is None
    # Les entrées du Router restent en USD, converties en CHF comme avant.
    assert cat.get("qwen/qwen3.8-27b").currency == "USD"
    assert cat.cost_chf("qwen/qwen3.8-27b", 1_000_000, 0, 0) == pytest.approx(0.20 / 1.25)


def test_turn_cost_tokens_and_eur_cost_per_model(cat):
    body = turn_cost([_call(OVH, 10_000, 1_000), _call(OVH, 12_000, 400)], cat)
    assert body["calls"] == 2 and body["subagent_calls"] == 0
    assert (body["input_tokens"], body["output_tokens"], body["total_tokens"]) == (22_000, 1_400, 23_400)
    # Amont sans déclaration de cache ni de raisonnement : inconnu, pas zéro.
    assert body["cached_tokens"] is None and body["reasoning_tokens"] is None
    expected = (22_000 * 0.60 + 1_400 * 3.60) / 1_000_000
    assert body["cost"] == {"EUR": round(expected, 6)}
    (m,) = body["models"]
    assert m["model"] == OVH and m["tiers"] == ["deep"] and m["calls"] == 2
    assert m["cost"] == {"EUR": round(expected, 6)} and m["price_source"] == "catalog"
    assert body["unpriced_models"] == []


def test_unknown_model_is_tokens_only_never_an_invented_price(cat):
    body = turn_cost([_call("acme/inconnu-9b", 5_000, 200), _call(OVH, 1_000, 100)], cat)
    assert body["unpriced_models"] == ["acme/inconnu-9b"]
    unknown = next(m for m in body["models"] if m["model"] == "acme/inconnu-9b")
    assert unknown["cost"] is None and unknown["price_source"] is None and unknown["total_tokens"] == 5_200
    assert set(body["cost"]) == {"EUR"}  # le total ne compte que ce qui a un prix
    only_unknown = turn_cost([_call("acme/inconnu-9b", 5_000, 200)], cat)
    assert only_unknown["cost"] == {} and only_unknown["total_tokens"] == 5_200
    assert turn_cost([_call(OVH, 10, 1)], None)["cost"] == {}  # pas de grille : jetons seulement


def test_cached_and_reasoning_when_declared_and_currencies_kept_apart(cat):
    calls = [_call("qwen/qwen3.8-27b", 10_000, 2_000, cached=4_000, cached_reported=True, reasoning=1_500,
                   reasoning_reported=True, tier=Tier.SHIP),
             _call(OVH, 1_000_000, 0)]
    body = turn_cost(calls, cat)
    assert body["cached_tokens"] == 4_000 and body["reasoning_tokens"] == 1_500
    ship = next(m for m in body["models"] if m["model"] == "qwen/qwen3.8-27b")
    usd = (6_000 * 0.20 + 4_000 * 0.05 + 2_000 * 2.50) / 1_000_000
    assert ship["cost"] == {"USD": round(usd, 6)}
    assert body["cost"] == {"EUR": 0.6, "USD": round(usd, 6)}  # deux devises, aucun taux inventé


def test_router_cost_is_taken_as_is_in_chf(cat):
    body = turn_cost([_call("qwen/qwen3.8-27b", 1_000, 10, chf=0.0123)], cat)
    assert body["cost"] == {"CHF": 0.0123} and body["models"][0]["price_source"] == "router"


def test_subagent_calls_count_in_the_turn(cat):
    child = row_from_call(_call(OVH, 2_000, 100))
    body = turn_cost([_call(OVH, 1_000, 50)], cat, subagents=[child])
    assert body["calls"] == 2 and body["subagent_calls"] == 1 and body["total_tokens"] == 3_150


def test_parse_usage_reports_reasoning_only_when_declared():
    assert _parse_usage({"prompt_tokens": 3, "completion_tokens": 2}).reasoning_reported is False
    u = _parse_usage({"prompt_tokens": 3, "completion_tokens": 2, "completion_tokens_details": {"reasoning_tokens": 0}})
    assert u.reasoning_reported is True and u.reasoning == 0


def test_ledger_totals_session_with_subagents_and_day(cat):
    led = InMemoryLedger(cat)
    led.record("th_a", 0, _call(OVH, 1_000, 100))
    led.record("th_a", 1, _call(OVH, 2_000, 200))
    led.record("th_a/sa-x1", 0, _call(OVH, 500, 50))
    led.record("th_ab", 0, _call(OVH, 9_000, 900))  # même préfixe sans /sa- : un autre fil
    led.record("th_b", 0, _call("acme/inconnu-9b", 4_000, 400))
    session = ledger_totals(led, cat, thread_id="th_a")
    assert session["turns"] == 2 and session["calls"] == 3 and session["total_tokens"] == 3_850
    assert session["cost"] == {"EUR": round((3_500 * 0.60 + 350 * 3.60) / 1_000_000, 6)}
    today = ledger_totals(led, cat, day=datetime.now(timezone.utc).date())
    assert today["turns"] == 4 and today["calls"] == 5 and today["unpriced_models"] == ["acme/inconnu-9b"]
    assert summarize([], cat)["cost"] == {} and ledger_totals(led, cat, thread_id="th_zz")["calls"] == 0


def _turn(thread_id: str, index: int, calls: list[LLMCall]) -> Turn:
    t = Turn(thread_id=thread_id, index=index, input=InputCheck(text="x"), reply="ok")
    t.calls = calls
    return t


def test_hub_emits_one_turn_cost_per_session_turn_with_its_subagents(cat):
    events = []
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=lambda t, d: events.append((t, d)),
                    prices=cat)
    hub.turn_done(_turn("th_s/sa-c1", 0, [_call(OVH, 2_000, 100)]))
    assert [t for t, _ in events if t == "turn.cost"] == []  # un tour de sous-agent ne publie pas de coût
    events.clear()
    hub.turn_done(_turn("th_s", 3, [_call(OVH, 1_000, 50)]))
    types = [t for t, _ in events]
    assert types.index("turn.cost") < types.index("turn.done")  # avant la fin : un client non interactif l'a
    (cost,) = [d for t, d in events if t == "turn.cost"]
    assert cost["thread_id"] == "th_s" and cost["turn_id"] == "th_s:3"
    assert cost["calls"] == 2 and cost["subagent_calls"] == 1 and cost["total_tokens"] == 3_150
    assert set(cost["cost"]) == {"EUR"}
    hub.turn_done(_turn("th_s", 4, []))  # tour sans appel modèle : zéro, pas d'erreur
    last = [d for t, d in events if t == "turn.cost"][-1]
    assert last["calls"] == 0 and last["cost"] == {} and last["subagent_calls"] == 0


def test_local_cost_route(tmp_path, cat):
    from nokker.api.server import make_handler
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.local.api import LOCAL_OWNER
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.test_local_api import _api, _req

    kernel, _ = make_kernel_with_approvals(InMemoryApprovals())
    led = InMemoryLedger(cat)
    led.record("th_a", 0, _call(OVH, 1_000, 100))
    led.record("th_a/sa-x", 0, _call(OVH, 1_000, 100))
    led.record("th_b", 0, _call(OVH, 3_000, 300))
    api = _api(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, led, local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert _req(port, "/v1/local/cost")[0] == 401
        assert _req(port, "/v1/local/cost?day=hier", token=api.token)[0] == 400
        status, body = _req(port, "/v1/local/cost?thread_id=th_a", token=api.token)
        assert status == 200
        assert body["session"]["thread_id"] == "th_a" and body["session"]["total_tokens"] == 2_200
        assert body["session"]["turns"] == 1
        assert body["day"]["date"] == datetime.now(timezone.utc).date().isoformat()
        assert body["day"]["total_tokens"] == 5_500 and set(body["day"]["cost"]) == {"EUR"}
        status, body = _req(port, "/v1/local/cost", token=api.token)
        assert status == 200 and body["session"] is None and body["day"]["calls"] == 3
        status, body = _req(port, "/v1/local/cost?day=2020-01-01", token=api.token)
        assert body["day"] == {**body["day"], "date": "2020-01-01", "calls": 0, "cost": {}}
    finally:
        httpd.shutdown()


def test_code_json_cost_is_the_turn_cost_with_this_run_subagents(cat):
    from types import SimpleNamespace

    from nokker.cli import _code_turn_cost

    led = InMemoryLedger(cat)
    led.record("code-1/sa-old", 0, _call(OVH, 99_000, 0))  # sous-agent d'un lancement précédent du même fil
    started = datetime.now(timezone.utc)
    led.record("code-1/sa-new", 0, _call(OVH, 2_000, 100))
    turn = _turn("code-1", 0, [_call(OVH, 1_000, 50)])
    body = _code_turn_cost(SimpleNamespace(ledger=led), "code-1", turn, started)
    assert body["thread_id"] == "code-1" and body["turn_id"] == "code-1:0"
    assert body["calls"] == 2 and body["subagent_calls"] == 1 and body["total_tokens"] == 3_150
    assert body == {"thread_id": "code-1", "turn_id": "code-1:0", **turn_cost(
        turn.calls, cat, subagents=[r for r in map(row_from_call, [_call(OVH, 2_000, 100)])])}
