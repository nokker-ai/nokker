"""`tiers.<nom>.cache_ttl` (format anthropic, `cache: prefix`) : durée des points de cache et 3e point sur le dernier
message. Champ absent : corps de requête identique, octet pour octet, à celui d'avant."""
from __future__ import annotations

import json

import pytest
import yaml

from nokker.config import TierConfig, load_config
from nokker.providers.anthropic import AnthropicProvider, to_messages_body
from nokker.providers.select import tier_provider

from .anthropic_upstream import MODEL, FakeAnthropic, message

PREFIX = "Tu es Nokker."
MSGS = [
    {"role": "system", "content": PREFIX + "\n\n[Profil] Tutoiement."},
    {"role": "user", "content": "mon agenda ?"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "type": "function",
                                                         "function": {"name": "calendar_list", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "t1", "content": "rien"},
]
TOOLS = [{"type": "function", "function": {"name": "calendar_list", "description": "agenda", "parameters": {}}}]
# Le corps d'avant ce lot (cache: prefix), figé : deux points, sans durée.
BEFORE = ('{"model":"claude-test-1","max_tokens":100,"messages":[{"role":"user","content":[{"type":"text","text":"mon agenda ?"}]},'
          '{"role":"assistant","content":[{"type":"tool_use","id":"t1","name":"calendar_list","input":{}}]},'
          '{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"rien"}]}],'
          '"system":[{"type":"text","text":"Tu es Nokker.","cache_control":{"type":"ephemeral"}},'
          '{"type":"text","text":"\\n\\n[Profil] Tutoiement."}],'
          '"tools":[{"name":"calendar_list","description":"agenda","input_schema":{"type":"object","properties":{}},"cache_control":{"type":"ephemeral"}}]}')


def _body(**kw):
    return to_messages_body(MSGS, TOOLS, model=MODEL, max_tokens=100, cache=True, stable_prefix=PREFIX, **kw)


def _points(body):
    return json.dumps(body).count('"cache_control"')


def test_without_the_field_the_body_is_byte_identical_to_before():
    assert json.dumps(_body(), separators=(",", ":"), ensure_ascii=False) == BEFORE
    assert json.dumps(_body(cache_ttl=""), separators=(",", ":"), ensure_ascii=False) == BEFORE


def test_one_hour_marks_the_three_points_with_the_ttl():
    body = _body(cache_ttl="1h")
    one_h = {"type": "ephemeral", "ttl": "1h"}
    assert body["system"][0]["cache_control"] == one_h
    assert body["tools"][-1]["cache_control"] == one_h
    assert body["messages"][-1]["content"][-1] == {"type": "tool_result", "tool_use_id": "t1", "content": "rien",
                                                   "cache_control": one_h}
    assert _points(body) == 3 and "cache_control" not in json.dumps(body["messages"][:-1])


def test_five_minutes_is_the_api_default_with_the_third_point():
    body = _body(cache_ttl="5m")
    assert "ttl" not in json.dumps(body) and _points(body) == 3
    assert body["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_no_cache_means_no_point_whatever_the_ttl():
    body = to_messages_body(MSGS, TOOLS, model=MODEL, max_tokens=100, cache=False, stable_prefix=PREFIX, cache_ttl="1h")
    assert _points(body) == 0


def test_the_last_message_point_moves_with_the_thread_and_stays_under_the_api_limit():
    longer = MSGS + [{"role": "assistant", "content": "Rien."}, {"role": "user", "content": "merci"}]
    body = to_messages_body(longer, TOOLS, model=MODEL, max_tokens=100, cache=True, stable_prefix=PREFIX, cache_ttl="1h")
    assert _points(body) == 3  # l’API en permet 4
    assert body["messages"][-1]["content"][-1]["cache_control"]["ttl"] == "1h"
    assert "cache_control" not in json.dumps(body["messages"][:-1])  # le point du tour d'avant n'est pas resté


def test_provider_sends_the_field_to_the_upstream():
    with FakeAnthropic() as up:
        for ttl in ("", "1h"):
            up.reply(message([{"type": "text", "text": "ok"}]))
            AnthropicProvider(base_url=up.url, api_key="k", model=MODEL, cache="prefix", cache_ttl=ttl).complete(
                MSGS, TOOLS, reasoning=False, session_key="t", stable_prefix=PREFIX)
        plain, hour = (r["body"] for r in up.requests)
    assert _points(plain) == 2 and "ttl" not in json.dumps(plain)
    assert _points(hour) == 3 and json.dumps(hour).count('"ttl": "1h"') == 3


def test_config_reads_validates_and_wires_the_field(tmp_path):
    def load(extra):
        path = tmp_path / "c.yaml"
        path.write_text(yaml.safe_dump({"tiers": {"deep": {"base_url": "https://a.example", "model": MODEL,
                                                           "format": "anthropic", "cache": "prefix", **extra}}}))
        return load_config(str(path)).tiers["deep"]

    assert TierConfig(base_url="u", model="m").cache_ttl == "" and load({}).cache_ttl == ""
    assert load({"cache_ttl": "1h"}).cache_ttl == "1h" and load({"cache_ttl": "5m"}).cache_ttl == "5m"
    assert tier_provider(load({"cache_ttl": "1h"})).cache_ttl == "1h"
    assert tier_provider(load({})).cache_ttl == ""
    for bad in ("2h", "1", 3600):
        with pytest.raises(ValueError, match="cache_ttl"):
            load({"cache_ttl": bad})
