"""`tiers.<nom>.max_tokens` à travers la boucle du noyau : la valeur du palier est celle de l'appel ; le défaut de la boucle
(`Kernel.max_tokens`, 16384 en session de code) ne s'applique que si le palier n'en pose pas. Avant : la boucle
imposait 16384 et la valeur du palier était ignorée (`provider.complete(max_tokens=16384)` prime sur `max_tokens_default`)."""
from __future__ import annotations

import json

import httpx
import pytest

from nokker.config import load_config
from nokker.core.loop import RouteDecision
from nokker.core.turn import Tier, TurnInput
from nokker.providers.select import tier_provider

from .. import test_loop_contract as loop
from ..test_loop_contract import ScriptedProvider

OPENAI_OK = {"model": "m", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
ANTH_OK = {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "model": "m",
           "usage": {"input_tokens": 1, "output_tokens": 1}}


def _ship(tmp_path, tier_yaml: str, anthropic: bool):
    fmt = "    format: anthropic\n    anthropic_version: '2023-06-01'\n" if anthropic else ""
    p = tmp_path / "c.yaml"
    p.write_text(f"tiers:\n  ship:\n    base_url: 'https://a.example/v1'\n    model: m\n{fmt}{tier_yaml}")
    return load_config(str(p)).tiers["ship"]


def _run(provider, anthropic: bool, *, reasoning: bool, kernel_cap=16384) -> dict:
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=ANTH_OK if anthropic else OPENAI_OK)

    provider._client = httpx.Client(transport=httpx.MockTransport(handler))
    k, *_ = loop.kernel([])
    k.max_tokens = kernel_cap
    k.providers[Tier.SHIP] = provider
    k.policy = type("P", (), {"choose": lambda self, st: RouteDecision(tier=Tier.SHIP, reasoning=reasoning, reason="r")})()
    assert k.run_turn(TurnInput("t1", "salut")).reply == "ok"
    return seen[0]


@pytest.mark.parametrize("anthropic", [False, True])
@pytest.mark.parametrize("reasoning", [False, True])
def test_tier_max_tokens_is_the_value_of_the_call(tmp_path, anthropic, reasoning):
    extra = {}
    if anthropic and reasoning:
        extra["reasoning_on_body"] = {"thinking": {"type": "enabled", "budget_tokens": 4000}}
    p = tier_provider(_ship(tmp_path, "    max_tokens: 8192\n", anthropic), **extra)
    body = _run(p, anthropic, reasoning=reasoning)
    assert body["max_tokens"] == 8192
    if anthropic and reasoning:  # max_tokens reste au-dessus du budget de réflexion
        assert body["thinking"]["budget_tokens"] < body["max_tokens"]


@pytest.mark.parametrize("anthropic", [False, True])
def test_without_tier_field_the_loop_default_still_applies(tmp_path, anthropic):
    p = tier_provider(_ship(tmp_path, "", anthropic))
    assert _run(p, anthropic, reasoning=False)["max_tokens"] == 16384
    p = tier_provider(_ship(tmp_path, "", anthropic))
    assert _run(p, anthropic, reasoning=False, kernel_cap=None)["max_tokens"] == 2048


def test_loop_cap_helper_ignores_providers_without_the_marker():
    k, *_ = loop.kernel([])
    k.max_tokens = 16384
    assert k._loop_max_tokens(ScriptedProvider([])) == 16384
