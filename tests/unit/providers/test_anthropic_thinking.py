"""Réflexion rejouée (docs/NOKKAN-ANTHROPIC.md §Réflexion rejouée) : avec la réflexion active, l'API exige que le dernier
message assistant d'un tour d'outils rende ses blocs `thinking` (signature) / `redacted_thinking` (data) avant les
`tool_use`, sinon 400. Le faux amont (`FakeAnthropic(require_thinking=True)`) refuse comme l'API.

Constat « avant » (code d'avant ce lot, même faux amont) : le 2e appel du tour d'outils partait SANS le bloc et
recevait `HTTP 400 … Expected thinking or redacted_thinking, but found tool_use` : le parcours cassait."""
from __future__ import annotations

import json

import httpx
import pytest

from nokker.core.turn import THINKING_KEY, Message, Tier, TurnInput, strip_thinking
from nokker.providers import anthropic as anth
from nokker.providers.anthropic import (
    AnthropicProvider,
    from_message,
    thinking_unusable,
    to_messages_body,
)
from nokker.providers.sokkan import SokkanProvider
from nokker.store.pg import _dec_message, _enc_message

from .. import test_loop_contract as loop
from .anthropic_upstream import MODEL, FakeAnthropic, message, stream_of

USER = {"role": "user", "content": "mon agenda ?"}
TOOL = {"role": "tool", "tool_call_id": "toolu_1", "content": "rien"}
THINK = {"type": "thinking", "thinking": "je regarde l'agenda", "signature": "SIG-1"}
REDACTED = {"type": "redacted_thinking", "data": "ENC-2"}
TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "calendar_list", "input": {}}


def _first(blocks, **kw):
    return message(blocks, stop_reason="tool_use", **kw)


def _second_messages(comp, **extra):
    return [USER, comp.message.to_openai(thinking=True), TOOL]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("blocks,expected", [
    ([THINK, TOOL_USE], [{"type": "thinking", "thinking": "je regarde l'agenda", "signature": "SIG-1"}]),
    ([REDACTED, TOOL_USE], [{"type": "redacted_thinking", "data": "ENC-2"}]),
    ([THINK, REDACTED, {"type": "text", "text": "voyons"}, TOOL_USE],
     [{"type": "thinking", "thinking": "je regarde l'agenda", "signature": "SIG-1"},
      {"type": "redacted_thinking", "data": "ENC-2"}]),
])
def test_tool_turn_with_thinking_is_replayed_in_order_before_the_tool_use(stream, blocks, expected):
    with FakeAnthropic(require_thinking=True) as up:
        p = AnthropicProvider(base_url=up.url, api_key="k", model=MODEL, retry_backoff_s=0.0)
        first = _first(blocks)
        if stream:
            up.reply_stream(stream_of(first))
        else:
            up.reply(first)
        comp = p.complete([USER], [], reasoning=True, session_key="t", **({"on_delta": lambda s: None} if stream else {}))
        up.reply(message([{"type": "text", "text": "Rien."}]))
        done = p.complete(_second_messages(comp), [], reasoning=True, session_key="t")
        body = up.requests[1]["body"]
    assert done.message.content == "Rien."  # le faux amont n'a pas refusé
    content = body["messages"][1]["content"]
    assert content[:len(expected)] == expected
    assert content[-1]["type"] == "tool_use"
    assert "_by" not in json.dumps(body)  # l'étiquette interne ne part jamais
    assert not comp.reasoning  # jamais affichée


def test_the_assistant_message_stored_in_the_kernel_keeps_the_blocks_and_never_shows_them():
    with FakeAnthropic(require_thinking=True) as up:
        up.reply(_first([THINK, TOOL_USE]))
        up.reply(message([{"type": "text", "text": "Rien de prévu."}]))

        class Ctx(loop.Ctx):  # comme le moteur de contexte : les blocs rejoués passent par `to_openai(thinking=True)`
            def assemble(self, turn, history, recall, loaded, tier):
                a = super().assemble(turn, history, recall, loaded, tier)
                a.messages = [m for m in a.messages if m["role"] != "assistant" and m["role"] != "tool"]
                a.messages += [m.to_openai(thinking=True) for m in turn.messages]
                a.tools = [loop.Tools.specs["calendar_list"].to_openai()]
                return a

        k, tools, _s, _d = loop.kernel([])
        k.context = Ctx()
        k.policy = type("P", (), {"choose": lambda self, st: loop.RouteDecision(tier=Tier.SHIP, reasoning=True,
                                                                                reason="réflexion")})()
        k.providers[Tier.SHIP] = AnthropicProvider(base_url=up.url, api_key="k", model=MODEL, retry_backoff_s=0.0)
        turn = k.run_turn(TurnInput("t1", "mon agenda ?"))
        second = up.requests[1]["body"]
    assert turn.reply == "Rien de prévu." and tools.called == ["calendar_list"]
    assert second["messages"][1]["content"][0] == {"type": "thinking", "thinking": "je regarde l'agenda",
                                                   "signature": "SIG-1"}
    stored = next(m for m in turn.messages if m.tool_calls)
    assert [b["type"] for b in stored.thinking_blocks] == ["thinking"]
    assert "je regarde" not in turn.reply and not any("je regarde" in m.content for m in turn.messages)


def test_without_replay_the_same_flow_is_refused_like_before():
    """Constat « avant » : le message assistant à outils sans bloc de réflexion → 400 du faux amont."""
    with FakeAnthropic(require_thinking=True) as up:
        p = AnthropicProvider(base_url=up.url, api_key="k", model=MODEL, retry_backoff_s=0.0)
        up.reply(_first([THINK, TOOL_USE]))
        comp = p.complete([USER], [], reasoning=True, session_key="t")
        up.reply(message([{"type": "text", "text": "ok"}]))
        with pytest.raises(RuntimeError, match="Expected `thinking`"):
            p.complete([USER, comp.message.to_openai(), TOOL], [], reasoning=True, session_key="t")  # forme d'avant


def test_reasoning_off_sends_no_thinking_block_and_no_thinking_parameter():
    with FakeAnthropic(require_thinking=True) as up:
        p = AnthropicProvider(base_url=up.url, api_key="k", model=MODEL, retry_backoff_s=0.0)
        up.reply(_first([THINK, TOOL_USE]))
        comp = p.complete([USER], [], reasoning=True, session_key="t")
        up.reply(message([{"type": "text", "text": "ok"}]))
        p.complete(_second_messages(comp), [], reasoning=False, session_key="t")
        body = up.requests[1]["body"]
    assert "thinking" not in body and body["messages"][1]["content"][0]["type"] == "tool_use"


def test_blocks_of_another_model_are_never_replayed_and_the_call_goes_without_thinking():
    """Changement de palier en milieu de fil : réflexion produite par un autre modèle → ignorée, appel sans réflexion."""
    foreign = Message("assistant", tool_calls=[],  # message produit par un autre palier anthropic
                      thinking_blocks=[{"type": "thinking", "thinking": "autre", "signature": "SIG-X", "_by": "autre-modele"}])
    wire = foreign.to_openai(thinking=True)
    wire["tool_calls"] = [{"id": "toolu_1", "type": "function", "function": {"name": "calendar_list", "arguments": "{}"}}]
    messages = [USER, wire, TOOL]
    assert thinking_unusable(messages, MODEL) and not thinking_unusable(messages, "autre-modele")
    with FakeAnthropic(require_thinking=True) as up:
        p = AnthropicProvider(base_url=up.url, api_key="k", model=MODEL, retry_backoff_s=0.0)
        up.reply(message([{"type": "text", "text": "ok"}]))
        p.complete(messages, [], reasoning=True, session_key="t")
        body = up.requests[0]["body"]
    assert "thinking" not in body and "SIG-X" not in json.dumps(body) and "autre" not in json.dumps(body["messages"])


def test_blocks_reach_no_other_format_provider():
    wire = Message("assistant", "x", thinking_blocks=[{"type": "thinking", "thinking": "t", "signature": "S", "_by": MODEL}]
                   ).to_openai(thinking=True)
    assert THINKING_KEY in wire and THINKING_KEY not in Message("assistant", "x", thinking_blocks=[{"type": "thinking"}]).to_openai()
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    p = SokkanProvider(base_url="https://x.invalid/v1", api_key="k", model="m")
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    p.complete([USER, wire], [], reasoning=False, session_key="t")
    assert THINKING_KEY not in json.dumps(sent[0])
    assert strip_thinking([USER]) == [USER] and strip_thinking([wire])[0] == {"role": "assistant", "content": "x"}
    # le noyau les retire aussi avant d'appeler un fournisseur qui ne les rejoue pas
    k, *_ = loop.kernel([])
    assert not getattr(k.providers[Tier.SHIP], "replays_thinking", False)


def test_only_the_current_tool_turn_is_replayed_not_older_turns():
    old = {"role": "assistant", "content": "", THINKING_KEY: [{"type": "thinking", "thinking": "ancien", "signature": "OLD",
                                                                 "_by": MODEL}],
           "tool_calls": [{"id": "toolu_0", "type": "function", "function": {"name": "a", "arguments": "{}"}}]}
    cur = {"role": "assistant", "content": "", THINKING_KEY: [{"type": "thinking", "thinking": "courant", "signature": "CUR",
                                                                "_by": MODEL}],
           "tool_calls": [{"id": "toolu_1", "type": "function", "function": {"name": "a", "arguments": "{}"}}]}
    msgs = [USER, old, {"role": "tool", "tool_call_id": "toolu_0", "content": "r"},
            {"role": "assistant", "content": "fin"}, {"role": "user", "content": "encore"}, cur, TOOL]
    text = json.dumps(to_messages_body(msgs, [], model=MODEL, max_tokens=10, thinking_model=MODEL))
    assert "CUR" in text and "OLD" not in text
    assert "CUR" not in json.dumps(to_messages_body(msgs, [], model=MODEL, max_tokens=10))  # sans réflexion active


def test_oversized_thinking_is_omitted_and_the_call_goes_without_thinking(monkeypatch):
    monkeypatch.setattr(anth, "MAX_THINKING_CHARS", 50)
    msg, _ = from_message(_first([{"type": "thinking", "thinking": "x" * 80, "signature": "S"}, TOOL_USE]), by=MODEL)
    assert msg.thinking_blocks == [{"type": "omitted", "_by": MODEL}]
    wire = msg.to_openai(thinking=True)
    assert thinking_unusable([USER, wire, TOOL], MODEL)


def test_signature_less_block_is_dropped_and_final_answers_keep_theirs():
    msg, _ = from_message(_first([{"type": "thinking", "thinking": "coupé", "signature": ""}, TOOL_USE]), by=MODEL)
    assert msg.thinking_blocks == []
    final, _ = from_message(message([THINK, {"type": "text", "text": "fin"}]), by=MODEL)
    # gardée aussi sur une réponse finale : la chaîne des blocs rejoués n'a pas de trou (docs/NOKKAN-ANTHROPIC.md)
    assert [b["signature"] for b in final.thinking_blocks] == ["SIG-1"]


def test_stream_assembles_thinking_and_signature_deltas():
    events = stream_of(_first([{"type": "thinking", "thinking": "a", "signature": "SIG"}, TOOL_USE]))
    # le faux flux envoie texte et signature en un delta : on les recolle en plusieurs
    events = [e for e in events if e.get("delta", {}).get("type") not in ("thinking_delta", "signature_delta")]
    at = next(i for i, e in enumerate(events) if e["type"] == "content_block_start" and e["index"] == 0) + 1
    extra = [{"type": "content_block_delta", "index": 0, "delta": {"type": d, "thinking" if d == "thinking_delta" else "signature": v}}
             for d, v in (("thinking_delta", "je "), ("thinking_delta", "pense"), ("signature_delta", "SI"),
                          ("signature_delta", "G"))]
    with FakeAnthropic() as up:
        up.reply_stream(events[:at] + extra + events[at:])
        got = AnthropicProvider(base_url=up.url, api_key="k", model=MODEL).complete(
            [USER], [], reasoning=True, session_key="t", on_delta=lambda s: None)
    assert [{k: v for k, v in b.items() if k not in ("_fp", "_prev")} for b in got.message.thinking_blocks] == [
        {"type": "thinking", "thinking": "je pense", "signature": "SIG", "_by": MODEL}]


def test_storage_is_backward_compatible_json():
    plain = Message("assistant", "x")
    assert "thinking_blocks" not in _enc_message(plain)  # octet pour octet comme avant
    assert _dec_message({"role": "assistant", "content": "x", "tool_calls": []}).thinking_blocks == []
    kept = Message("assistant", "", thinking_blocks=[{"type": "thinking", "thinking": "t", "signature": "S", "_by": MODEL}])
    again = _dec_message(json.loads(json.dumps(_enc_message(kept))))
    assert again.thinking_blocks == kept.thinking_blocks


def test_a_hand_built_provider_replays_through_the_kernel_without_tier_provider():
    """Rejeu porté par la classe : `anthropic_provider(...)` (byok, Lokkal par défaut) et le constructeur direct, sans
    `tier_provider` ni config de palier."""
    from nokker.providers.select import anthropic_provider

    for build in (lambda url: AnthropicProvider(base_url=url, api_key="k", model=MODEL),
                  lambda url: anthropic_provider(base_url=url, api_key="k", model=MODEL)):
        with FakeAnthropic(require_thinking=True) as up:
            up.reply(_first([THINK, REDACTED, TOOL_USE]))
            up.reply(message([{"type": "text", "text": "ok"}]))

            class Ctx(loop.Ctx):
                def assemble(self, turn, history, recall, loaded, tier):
                    a = super().assemble(turn, history, recall, loaded, tier)
                    a.messages = [m for m in a.messages if m["role"] not in ("assistant", "tool")]
                    a.messages += [m.to_openai(thinking=True) for m in turn.messages]
                    return a

            k, _t, _s, _d = loop.kernel([])
            k.context = Ctx()
            k.policy = type("P", (), {"choose": lambda self, st: loop.RouteDecision(tier=Tier.SHIP, reasoning=True,
                                                                                    reason="r")})()
            provider = build(up.url)
            assert provider.replays_thinking
            k.providers[Tier.SHIP] = provider
            turn = k.run_turn(TurnInput("t1", "mon agenda ?"))
            second = up.requests[1]["body"]["messages"][1]["content"]
        assert turn.reply == "ok" and [b["type"] for b in second[:3]] == ["thinking", "redacted_thinking", "tool_use"]
