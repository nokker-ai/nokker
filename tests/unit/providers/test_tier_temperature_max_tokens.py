"""`tiers.<nom>.temperature` et `tiers.<nom>.max_tokens` : validés à la lecture de la config, posés dans le corps de la requête
de chaque format de palier (openai-compat / sokkan, anthropic) ; absents : le corps d'avant (max_tokens 2048)."""
from __future__ import annotations

import json

import httpx
import pytest

from nokker.api.server import config_tier_provider
from nokker.config import load_config
from nokker.providers.select import tier_provider


def _cfg(tmp_path, tier_yaml: str):
    p = tmp_path / "c.yaml"
    p.write_text(f"tiers:\n  ship:\n    base_url: 'https://a.example/v1'\n    model: m\n{tier_yaml}")
    return load_config(str(p))


def _sent(provider, anthropic=False):
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        if anthropic:
            return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                                             "model": "m", "usage": {"input_tokens": 1, "output_tokens": 1}})
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    provider._client = httpx.Client(transport=httpx.MockTransport(handler))
    provider.complete([{"role": "user", "content": "salut"}], [], reasoning=False, session_key="k")
    return seen[0]


def test_absent_fields_keep_the_body_from_before(tmp_path):
    cfg = _cfg(tmp_path, "")
    ship = cfg.tiers["ship"]
    assert ship.temperature is None and ship.max_tokens is None
    body = _sent(tier_provider(ship))
    assert body["max_tokens"] == 2048 and "temperature" not in body


def test_openai_compat_body_carries_both(tmp_path):
    ship = _cfg(tmp_path, "    temperature: 0.3\n    max_tokens: 4096\n").tiers["ship"]
    assert ship.temperature == 0.3 and ship.max_tokens == 4096
    body = _sent(tier_provider(ship))
    assert body["temperature"] == 0.3 and body["max_tokens"] == 4096
    body = _sent(config_tier_provider(_cfg(tmp_path, "    temperature: 0\n    max_tokens: 131072\n"), "ship"))
    assert body["temperature"] == 0.0 and body["max_tokens"] == 131072


def test_anthropic_body_carries_both(tmp_path):
    ship = _cfg(tmp_path, "    format: anthropic\n    anthropic_version: '2023-06-01'\n    temperature: 0.7\n    max_tokens: 1500\n").tiers["ship"]
    body = _sent(tier_provider(ship), anthropic=True)
    assert body["temperature"] == 0.7 and body["max_tokens"] == 1500


def test_caller_max_tokens_wins_over_the_tier_field(tmp_path):
    ship = _cfg(tmp_path, "    max_tokens: 4096\n").tiers["ship"]
    provider = tier_provider(ship)
    seen: list[dict] = []
    provider._client = httpx.Client(transport=httpx.MockTransport(lambda r: (seen.append(json.loads(r.content)), httpx.Response(
        200, json={"model": "m", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))[1]))
    provider.complete([{"role": "user", "content": "x"}], [], reasoning=False, session_key="k", max_tokens=512)
    assert seen[0]["max_tokens"] == 512


@pytest.mark.parametrize("line", ["temperature: -0.1", "temperature: 2.1", "temperature: 'chaud'", "temperature: true",
                                  "max_tokens: 0", "max_tokens: 131073", "max_tokens: 1.5", "max_tokens: '100'",
                                  "max_tokens: true"])
def test_invalid_values_are_a_config_error(tmp_path, line):
    with pytest.raises(ValueError, match="temperature|max_tokens"):
        _cfg(tmp_path, f"    {line}\n")


def test_boundaries_are_accepted_and_double_temperature_is_refused(tmp_path):
    ship = _cfg(tmp_path, "    temperature: 2\n    max_tokens: 1\n").tiers["ship"]
    assert ship.temperature == 2.0 and ship.max_tokens == 1
    assert _cfg(tmp_path, "    temperature: 0.5\n    sampling: {temperature: 0.5}\n").tiers["ship"].sampling == {"temperature": 0.5}
    with pytest.raises(ValueError, match="deux valeurs"):
        _cfg(tmp_path, "    temperature: 0.5\n    sampling: {temperature: 0.9}\n")
