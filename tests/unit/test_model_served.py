"""Modèle RENVOYÉ par l'amont (`model_served`) : lecture par provider (non-flux et flux), borne, ledger, traces, événement
`route.model_mismatch` (comparaison tolérante, une fois par tour, jamais bloquant). Aucun réseau."""
from __future__ import annotations

import json

import httpx
import pytest

from nokker.core.model_served import MODEL_SERVED_MAX, clean_served, normalize_model, same_model
from nokker.core.ports import Completion
from nokker.core.turn import LLMCall, Message, Tier, TurnInput, Usage
from nokker.ledger.costs import InMemoryLedger
from nokker.ledger.prices import load_from_yaml
from nokker.providers.anthropic import AnthropicProvider
from nokker.providers.sokkan import SokkanProvider
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

from . import test_loop_contract as loop
from .providers.anthropic_upstream import MODEL, FakeAnthropic, message, stream_of
from .traces import test_traces as tr

MSGS = [{"role": "user", "content": "ping"}]


# ───────────────────────────── valeur : bornée, jamais inventée ─────────────────────────────


def test_clean_served_keeps_a_string_bounds_it_and_never_invents():
    assert clean_served("qwen/qwen3.8-27b") == "qwen/qwen3.8-27b"
    assert clean_served("  m\x00\n\x1b[31m-x \u0085") == "m[31m-x"  # contrôle retirés, bords nettoyés
    assert clean_served("a" * 500) == "a" * MODEL_SERVED_MAX and MODEL_SERVED_MAX == 200
    for bad in (None, "", "   ", "\x00\x01", 12, ["m"], {"m": 1}, True):
        assert clean_served(bad) is None


# ───────────────────────────── providers : non-flux, flux, absent ─────────────────────────────


def _sokkan(handler) -> SokkanProvider:
    p = SokkanProvider(base_url="https://amont.invalid/v1", api_key="k", model="qwen/qwen3.8-27b", retry_backoff_s=0.0)
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    return p


def _body(**extra) -> dict:
    return {"choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}, **extra}


def _sse(chunks: list) -> bytes:
    return b"".join(f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n".encode() for c in chunks)


def _stream_response(chunks):
    return lambda r: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=_sse(chunks))


def test_openai_compat_non_stream_reads_the_response_model_and_null_when_absent():
    served = _sokkan(lambda r: httpx.Response(200, json=_body(model="vendor/fallback-397b"))).complete(
        MSGS, [], reasoning=False, session_key="s")
    assert served.model_served == "vendor/fallback-397b" == served.model
    absent = _sokkan(lambda r: httpx.Response(200, json=_body())).complete(MSGS, [], reasoning=False, session_key="s")
    assert absent.model_served is None and absent.model == "qwen/qwen3.8-27b"  # le palier n'est JAMAIS recopié
    for odd in (None, "", 7, ["x"]):
        c = _sokkan(lambda r, o=odd: httpx.Response(200, json=_body(model=o))).complete(
            MSGS, [], reasoning=False, session_key="s")
        assert c.model_served is None


def test_openai_compat_non_stream_truncates_a_long_value():
    c = _sokkan(lambda r: httpx.Response(200, json=_body(model="m" * 900))).complete(
        MSGS, [], reasoning=False, session_key="s")
    assert c.model_served == "m" * 200


def _chunk(delta, finish=None, model=None, usage=None):
    obj = {"object": "chat.completion.chunk", "choices": [] if delta is None else
           [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if model is not None:
        obj["model"] = model
    if usage is not None:
        obj["usage"] = usage
    return obj


def test_openai_compat_stream_reads_the_model_of_the_chunks_and_null_when_absent():
    chunks = [_chunk({"role": "assistant", "content": ""}, model="vendor/fallback-397b"), _chunk({"content": "po"}),
              _chunk({"content": "ng"}), _chunk({}, finish="stop"),
              _chunk(None, usage={"prompt_tokens": 1, "completion_tokens": 1}), "[DONE]"]
    seen: list[str] = []
    c = _sokkan(_stream_response(chunks)).complete(MSGS, [], reasoning=False, session_key="s", on_delta=seen.append)
    assert c.model_served == "vendor/fallback-397b" and c.message.content == "pong" and seen == ["po", "ng"]
    assert "model" in c.raw
    # Aucun chunk ne porte `model` : NULL, et le corps reconstitué n'en invente pas.
    bare = [_chunk({"content": "ok"}), _chunk({}, finish="stop"), "[DONE]"]
    n = _sokkan(_stream_response(bare)).complete(MSGS, [], reasoning=False, session_key="s", on_delta=lambda p: None)
    assert n.model_served is None and "model" not in n.raw and n.model == "qwen/qwen3.8-27b"
    # `model` d'un chunk tardif (le dernier gagne, comme avant) ; chaîne trop longue bornée.
    late = [_chunk({"content": "ok"}), _chunk({}, finish="stop", model="z" * 400), "[DONE]"]
    assert _sokkan(_stream_response(late)).complete(
        MSGS, [], reasoning=False, session_key="s", on_delta=lambda p: None).model_served == "z" * 200


def test_byok_provider_inherits_the_openai_compat_reading():
    from nokker.byok.service import ByokProvider

    p = ByokProvider(base_url="https://client.invalid/v1", api_key="k", model="gpt-x", retry_backoff_s=0.0)
    p._client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_body(model="gpt-x-2026-01-01"))))
    assert p.complete(MSGS, [], reasoning=False, session_key="s").model_served == "gpt-x-2026-01-01"
    p._client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_body())))
    assert p.complete(MSGS, [], reasoning=False, session_key="s").model_served is None


def _anthropic(up) -> AnthropicProvider:
    return AnthropicProvider(base_url=up.url, api_key="sk-ant-faux", model=MODEL, retry_backoff_s=0.0)


def test_anthropic_non_stream_reads_message_model_and_null_when_absent():
    with FakeAnthropic() as up:
        up.reply(message([{"type": "text", "text": "ok"}], model="claude-test-1-20261101"))
        no_model = message([{"type": "text", "text": "ok"}])
        del no_model["model"]
        up.reply(no_model)
        p = _anthropic(up)
        a = p.complete(MSGS, [], reasoning=False, session_key="s")
        b = p.complete(MSGS, [], reasoning=False, session_key="s")
    assert a.model_served == "claude-test-1-20261101"
    assert b.model_served is None and b.model == MODEL


def test_anthropic_stream_reads_message_start_model_and_null_when_absent():
    msg = message([{"type": "text", "text": "ok"}], model="claude-fallback-9")
    with FakeAnthropic() as up:
        up.reply_stream(stream_of(msg))
        events = stream_of(message([{"type": "text", "text": "ok"}]))
        del events[0]["message"]["model"]
        up.reply_stream(events)
        p = _anthropic(up)
        a = p.complete(MSGS, [], reasoning=False, session_key="s", on_delta=lambda s: None)
        b = p.complete(MSGS, [], reasoning=False, session_key="s", on_delta=lambda s: None)
    assert a.model_served == "claude-fallback-9" and a.message.content == "ok"
    assert b.model_served is None and b.model == MODEL and "model" not in b.raw


# ───────────────────────────── comparaison tolérante ─────────────────────────────


@pytest.mark.parametrize("expected,served", [
    ("qwen3.8-27b", "qwen/qwen3.8-27b"), ("qwen/qwen3.8-27b", "qwen3.8-27b"), ("Qwen3.8-27B", "qwen3.8-27b"),
    ("claude-sonnet-5-5", "claude-sonnet-5-5-20261101"), ("claude-sonnet-5-5-20261101", "claude-sonnet-5-5"),
    ("gpt-x", "gpt-x-2026-01-01"), ("claude-sonnet-5-5", "us.anthropic.claude-sonnet-5-5-20261101-v1:0"),
    ("m", "openrouter/vendor/m@20260101"), ("m-latest", "m"), ("m", None), (None, "m"), ("", "m"), ("m", ""),
])
def test_equivalent_names_are_not_a_mismatch(expected, served):
    assert same_model(expected, served) is True


@pytest.mark.parametrize("expected,served", [
    ("qwen3.8-27b", "qwen3.8-35b"), ("qwen3.8-27b", "qwen3-397b-fallback"), ("claude-sonnet-5-5", "claude-sonnet-5-4"),
    ("claude-sonnet-5-5", "claude-opus-5-5-20261101"), ("m", "m2"), ("vendor/a", "vendor/b"),
])
def test_real_differences_are_a_mismatch(expected, served):
    assert same_model(expected, served) is False


def test_normalization_rule_is_exactly_the_documented_one():
    assert normalize_model("  Accounts/Fw/Models/Qwen3.8-27B ") == "qwen3.8-27b"
    assert normalize_model("claude-sonnet-5-5-20261101") == "claude-sonnet-5-5"
    assert normalize_model("claude-sonnet-5-5-2026-11-01") == "claude-sonnet-5-5"
    assert normalize_model("eu.anthropic.claude-x-20261101-v1:0") == "claude-x"
    assert normalize_model("-latest") == "-latest"  # jamais un nom vidé


# ───────────────────────────── ledger ─────────────────────────────


def _call(served=None) -> LLMCall:
    kw = {"model_served": served} if served is not None else {}
    return LLMCall(tier=Tier.SHIP, model="m", reasoning=False, prefix_sha256="x", layer_tokens={},
                   usage=Usage(prompt=10, completion=2), latency_s=0.1, route_reason="t", **kw)


def test_in_memory_ledger_records_model_served_or_none():
    ledger = InMemoryLedger(load_from_yaml())
    ledger.record("t", 0, _call("vendor/fallback-397b"))
    ledger.record("t", 1, _call())
    rows = sorted(ledger.fetch_calls(thread_id="t"), key=lambda r: r.turn_index)
    assert [r.model_served for r in rows] == ["vendor/fallback-397b", None] and rows[0].model == "m"


def test_the_turn_json_roundtrips_model_served_and_old_rows_read_as_none():
    from nokker.store.pg import _dec_call, _enc_call

    c = _call("vendor/fallback-397b")
    assert _enc_call(c)["model_served"] == "vendor/fallback-397b" and _dec_call(_enc_call(c)).model_served == c.model_served
    plain = _enc_call(_call())
    assert "model_served" not in plain and _dec_call(plain).model_served is None


# ───────────────────────────── noyau : ledger et événement ─────────────────────────────


class Served(loop.ScriptedProvider):
    """Provider scripté qui rend un modèle servi par appel (None = l'amont n'a rien dit)."""

    def __init__(self, script, served, model="scripted"):
        super().__init__(script, model=model)
        self.served = list(served)

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        comp = super().complete(messages, tools, reasoning=reasoning, session_key=session_key, max_tokens=max_tokens)
        return Completion(message=comp.message, usage=comp.usage, model=comp.model, latency_s=comp.latency_s,
                          model_served=self.served.pop(0))


class Obs:
    def __init__(self, boom=False):
        self.events, self.boom = [], boom

    def route_model_mismatch(self, turn, data):
        self.events.append((turn.thread_id, turn.index, dict(data)))
        if self.boom:
            raise RuntimeError("observateur en panne")


def _run(served, *, model="qwen3.8-27b", boom=False, thread="t1", calls=3, obs=True):
    script = [Message("assistant", tool_calls=[loop.tc("calendar_list", day=str(i))]) for i in range(calls - 1)]
    script.append(Message("assistant", "voilà"))
    k, *_ = loop.kernel([])
    k.providers[Tier.SHIP] = Served(script, served, model=model)
    k.observer = Obs(boom) if obs else None
    turn = k.run_turn(TurnInput(thread, "mon agenda ?"))
    return k, turn


def test_kernel_ledger_gets_the_served_model_and_never_the_tier_model():
    k, turn = _run(["vendor/fallback-397b", None, "vendor/fallback-397b"])
    assert [c.model_served for c in k.ledger.calls] == ["vendor/fallback-397b", None, "vendor/fallback-397b"]
    assert [c.model for c in turn.calls] == ["qwen3.8-27b"] * 3  # `model` d'avant : inchangé


def test_mismatch_event_is_emitted_once_per_tier_and_served_model_per_turn():
    k, turn = _run(["vendor/fallback-397b", "vendor/fallback-397b", "vendor/other"])
    ev = k.observer.events
    assert [e[2]["model_served"] for e in ev] == ["vendor/fallback-397b", "vendor/other"]
    first = ev[0][2]
    assert first["tier"] == "ship" and first["model_expected"] == "qwen3.8-27b"
    assert first["call_id"] == turn.calls[0].call_id and ev[0][:2] == ("t1", turn.index)
    assert turn.reply  # le tour a fini normalement


def test_mismatch_event_is_emitted_again_on_the_next_turn():
    k, _ = _run(["vendor/fallback-397b"] * 3)
    script = [Message("assistant", "encore")]
    k.providers[Tier.SHIP] = Served(script, ["vendor/fallback-397b"], model="qwen3.8-27b")
    k.run_turn(TurnInput("t1", "et ensuite ?"))
    assert len(k.observer.events) == 2 and k.observer.events[0][1] != k.observer.events[1][1]


def test_no_event_when_equivalent_after_normalization_absent_or_no_configured_model():
    k, _ = _run(["qwen/QWEN3.8-27b-20261101", "qwen3.8-27b", "vendor/qwen3.8-27b"])
    assert k.observer.events == []
    k, _ = _run([None, None, None])
    assert k.observer.events == []
    k, turn = _run(["vendor/fallback-397b"] * 3, model="")  # alias/routeur : aucun modèle configuré
    assert k.observer.events == [] and [c.model_served for c in k.ledger.calls] == ["vendor/fallback-397b"] * 3
    k, turn = _run(["vendor/fallback-397b"] * 3, obs=False)  # pas d'observateur : rien à casser
    assert turn.reply == "voilà"


def test_a_raising_observer_never_blocks_the_turn():
    k, turn = _run(["vendor/fallback-397b", "vendor/other", "vendor/other"], boom=True)
    assert turn.reply == "voilà" and turn.error is None and len(k.observer.events) == 2


def test_a_provider_without_the_field_stays_null():
    k, *_ = loop.kernel([Message("assistant", "ok")])
    k.observer = Obs()
    k.run_turn(TurnInput("t9", "salut"))
    assert [c.model_served for c in k.ledger.calls] == [None] and k.observer.events == []


# ───────────────────────────── traces ─────────────────────────────


class TracedProvider(tr.Provider):
    def __init__(self, script, served):
        super().__init__(script)
        self.served = list(served)

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        comp = super().complete(messages, tools, reasoning=reasoning, session_key=session_key)
        return Completion(message=comp.message, usage=comp.usage, model=comp.model, latency_s=comp.latency_s,
                          model_served=self.served.pop(0))


def test_traces_carry_model_served_per_call_and_export_it_last_in_meta(tmp_path):
    rec = tr.recorder()
    k, _ = tr.make_kernel([], rec)
    k.providers[Tier.SHIP] = TracedProvider(list(tr.THREE_CALLS), ["vendor/fallback-397b", None, "m-ship-20261101"])
    with tenant_scope(INTERNAL_TENANT):
        k.run_turn(TurnInput("fil", "mon agenda ?"))
    rows = rec.sink.rows
    assert [r["meta"]["model_served"] for r in rows] == ["vendor/fallback-397b", None, "m-ship-20261101"]
    assert all(r["meta"]["model"] == "m-ship" for r in rows)
    lot = tr.export(tr.source_from(rows), tmp_path, tr.opts(), exporter_sha="abc1234").path
    metas = [ln["meta"] for ln in tr.read_lines(lot)]
    assert [m["model_served"] for m in metas] == ["vendor/fallback-397b", None, "m-ship-20261101"]
    assert list(metas[0])[-2:] == ["model_served", "upstream"]  # en fin : l'ordre des clés d'avant ne bouge pas
    # Ligne d'avant (sans la clé) : null, rien ne casse.
    old = [{**r, "meta": {k_: v for k_, v in r["meta"].items() if k_ != "model_served"}} for r in rows]
    lot2 = tr.export(tr.source_from(old), tmp_path / "b", tr.opts(), exporter_sha="abc1234").path
    assert [ln["meta"]["model_served"] for ln in tr.read_lines(lot2)] == [None, None, None]
