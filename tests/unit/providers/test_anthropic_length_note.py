"""Coupure `max_tokens` en réflexion : la note de relance (rôle user) suit un message user ou un résultat d'outil ; le
provider Anthropic la fusionne dans le message user précédent (alternance stricte, `tool_result` avant le texte)."""
from __future__ import annotations

import json

from nokker.core.turn import TurnInput
from nokker.providers.anthropic import AnthropicProvider, to_messages_body

from ..test_length_reasoning import _kernel
from .anthropic_upstream import MODEL, FakeAnthropic, message

CUT = [{"type": "thinking", "thinking": "je calcule", "signature": "sig-1"},
       {"type": "text", "text": "…donc la réponse est <think>je vérifie"}]


def _run(first_replies):
    with FakeAnthropic() as up:
        for r in first_replies:
            up.reply(r)
        k, _ship, _tools = _kernel([], reasoning=True)
        k.providers[next(iter(k.providers))] = AnthropicProvider(base_url=up.url, api_key="sk-ant-faux", model=MODEL,
                                                                  retry_backoff_s=0.0)
        turn = k.run_turn(TurnInput("t1", "calcule", lang="fr"))
        bodies = [r["body"] for r in up.requests]
    return turn, bodies


def _strict(body):
    roles = [m["role"] for m in body["messages"]]
    assert all(a != b for a, b in zip(roles, roles[1:])), roles
    assert roles[0] == "user"


def test_note_after_a_user_text_is_merged_into_that_message():
    turn, (first, second) = _run([message(CUT, stop_reason="max_tokens"),
                                  message([{"type": "text", "text": "42."}])])
    assert turn.reply == "42." and "thinking" not in json.dumps(second["messages"])
    _strict(second)
    assert [m["role"] for m in second["messages"]] == ["user"]
    blocks = second["messages"][0]["content"]
    assert all(b["type"] == "text" for b in blocks) and len(blocks) == len(first["messages"][0]["content"]) + 1
    assert blocks[:-1] == first["messages"][0]["content"]
    assert "donc la réponse est" not in json.dumps(second["messages"])


def test_note_after_a_tool_result_goes_after_the_tool_results_in_the_same_user_message():
    turn, bodies = _run([message([{"type": "tool_use", "id": "toolu_01A", "name": "calendar_list", "input": {}}],
                                 stop_reason="tool_use"),
                         message(CUT, stop_reason="max_tokens"),
                         message([{"type": "text", "text": "Rien."}])])
    assert turn.reply == "Rien."
    last = bodies[-1]
    _strict(last)
    assert [m["role"] for m in last["messages"]] == ["user", "assistant", "user"]
    kinds = [b["type"] for b in last["messages"][-1]["content"]]
    assert kinds[0] == "tool_result" and kinds[-1] == "text" and kinds.count("text") == 1
    assert kinds.index("text") > max(i for i, k in enumerate(kinds) if k == "tool_result")


def test_history_without_length_gives_the_same_body_bytes():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "a"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                                                                 "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"}, {"role": "assistant", "content": "fait"},
            {"role": "user", "content": [{"type": "text", "text": "b"}]}]
    body = to_messages_body(msgs, [], model=MODEL, max_tokens=100)
    assert json.dumps(body, sort_keys=True) == json.dumps(
        {"model": MODEL, "max_tokens": 100, "system": [{"type": "text", "text": "S"}], "messages": [
            {"role": "user", "content": [{"type": "text", "text": "a"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "f", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "fait"}]},
            {"role": "user", "content": [{"type": "text", "text": "b"}]}]}, sort_keys=True)
