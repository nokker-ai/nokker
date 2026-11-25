"""`tiers.<nom>.reasoning_on_body` et `tiers.<nom>.effort` : la réflexion se règle par palier, sans code. Absents : corps
de requête inchangé."""
from __future__ import annotations

import json

import httpx
import pytest
import yaml

from nokker.config import TierConfig, load_config
from nokker.providers.select import tier_provider

from .anthropic_upstream import MODEL, FakeAnthropic, message

MSG = [{"role": "user", "content": "x"}]


def _cfg(tmp_path, **tier):
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"tiers": {"deep": {"base_url": "https://a.example", "model": MODEL, **tier}}}))
    return load_config(str(path)).tiers["deep"]


def _anthropic_bodies(cfg, **kw):
    with FakeAnthropic() as up:
        cfg.base_url = up.url
        p = tier_provider(cfg)
        for reasoning in (True, False):
            up.reply(message([{"type": "text", "text": "ok"}]))
            p.complete(MSG, [], reasoning=reasoning, session_key="t")
        return [r["body"] for r in up.requests]


def test_absent_fields_change_nothing(tmp_path):
    cfg = _cfg(tmp_path, format="anthropic")
    assert cfg.reasoning_on_body is None and cfg.effort == ""
    on, off = _anthropic_bodies(cfg)
    assert on["thinking"] == {"type": "adaptive"} and "output_config" not in on
    assert "thinking" not in off and "output_config" not in off


def test_reasoning_on_body_comes_from_the_tier_config(tmp_path):
    cfg = _cfg(tmp_path, format="anthropic", reasoning_on_body={"thinking": {"type": "enabled", "budget_tokens": 4096}})
    on, off = _anthropic_bodies(cfg)
    assert on["thinking"] == {"type": "enabled", "budget_tokens": 4096} and "thinking" not in off


def test_effort_reaches_every_call_of_an_anthropic_tier(tmp_path):
    cfg = _cfg(tmp_path, format="anthropic", effort="low")
    on, off = _anthropic_bodies(cfg)
    assert on["output_config"] == {"effort": "low"} == off["output_config"]
    assert on["thinking"] == {"type": "adaptive"}


def test_an_explicit_output_config_in_a_reasoning_body_wins_over_effort(tmp_path):
    cfg = _cfg(tmp_path, format="anthropic", effort="low", reasoning_on_body={"output_config": {"effort": "high"}})
    on, off = _anthropic_bodies(cfg)
    assert on["output_config"] == {"effort": "high"} and off["output_config"] == {"effort": "low"}


def test_openai_tier_gets_reasoning_on_body_in_its_request(tmp_path):
    cfg = _cfg(tmp_path, reasoning_on_body={"reasoning_effort": "low"})
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    p = tier_provider(cfg)
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    p.complete(MSG, [], reasoning=True, session_key="t")
    p.complete(MSG, [], reasoning=False, session_key="t")
    assert sent[0]["reasoning_effort"] == "low" and sent[1]["reasoning_effort"] == "none"
    # sans le champ : aucun `reasoning_effort` quand la réflexion est demandée (défaut amont)
    p2 = tier_provider(_cfg(tmp_path))
    p2._client = httpx.Client(transport=httpx.MockTransport(handler))
    p2.complete(MSG, [], reasoning=True, session_key="t")
    assert "reasoning_effort" not in sent[2]


@pytest.mark.parametrize("field,bad", [("effort", "enorme"), ("effort", 3), ("reasoning_on_body", "adaptive"),
                                       ("reasoning_on_body", [1])])
def test_invalid_values_are_config_errors(tmp_path, field, bad):
    with pytest.raises(ValueError, match=field):
        _cfg(tmp_path, **{field: bad})


def test_tier_config_defaults():
    t = TierConfig(base_url="u", model="m")
    assert t.reasoning_on_body is None and t.effort == ""
