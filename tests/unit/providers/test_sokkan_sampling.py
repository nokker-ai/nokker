"""Échantillonnage fixe par palier (`tiers.<nom>.sampling`) : envoyé à l'amont, absent par défaut, validé à la lecture."""
from __future__ import annotations

import json

import httpx
import pytest

from nokker.config import load_config, sampling_of
from nokker.providers.sokkan import SokkanProvider

_OK = {"model": "nina", "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
       "usage": {"prompt_tokens": 3, "completion_tokens": 1}}


def _sent_body(**kw) -> dict:
    seen: dict = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_OK)

    p = SokkanProvider(base_url="https://nina.example/v1", api_key="k", model="nina", **kw)
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    p.complete([{"role": "user", "content": "x"}], [], reasoning=False, session_key="t")
    return seen


def test_sampling_body_sent():
    body = _sent_body(sampling_body={"temperature": 0.3})
    assert body["temperature"] == 0.3 and body["model"] == "nina"


def test_no_sampling_by_default():
    assert "temperature" not in _sent_body()


def test_sampling_of_rejects_unknown_keys():
    with pytest.raises(ValueError):
        sampling_of({"temperature": 0.3, "model": "x"})
    assert sampling_of({"temperature": "0.3", "seed": 7}) == {"temperature": 0.3, "seed": 7}
    assert sampling_of(None) == {}


def test_tier_yaml_reads_sampling_and_context(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("tiers:\n  nina:\n    base_url: https://nina.example/v1\n    model: nina\n    api_key_env: NINA_KEY\n"
                   "    max_context_tokens: 32768\n    sampling: {temperature: 0.3}\n", encoding="utf-8")
    t = load_config(str(cfg)).tiers["nina"]
    assert t.sampling == {"temperature": 0.3} and t.max_context_tokens == 32768 and t.api_key_env == "NINA_KEY"
