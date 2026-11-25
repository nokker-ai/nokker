"""`Kernel.stream_upstream` (réglage d'hôte `local.stream_upstream`) : l'appel amont part en flux même sans lecteur de jetons
(`X-Nokker-Stream-Tokens: 1` absent), pour que `cancel {keep_partial: true}`, demandé après le début de l'appel, ait un
partiel à garder.

Constat « avant » : sans cet en-tête, `run_turn` n'avait pas de puits, l'amont était appelé SANS flux (`stream` absent du
corps) et `keep_partial` ne gardait rien (texte de fin d'annulation ordinaire)."""
from __future__ import annotations

import json
import threading

import httpx

from nokker.core import loop as loop_mod
from nokker.core.turn import TurnInput
from nokker.providers.sokkan import SokkanProvider
from tests.unit.test_cancel_stream import Sink, SlowStream, _provider
from tests.unit.test_pause_steer import WAIT, kernel

MARKER = "[Réponse interrompue à la demande de la personne.]"


def _cancel_midway(*, stream_upstream, sink=None):
    go, sent = threading.Event(), threading.Event()
    body, calls = SlowStream(go, sent), []
    k, _, _ = kernel([], provider=_provider(body, calls))
    k.stream_upstream = stream_upstream
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("turn", k.run_turn(
        TurnInput(thread_id="th_u", user_text="écris beaucoup", channel="cli"), stream=sink)))
    th.start()
    assert sent.wait(WAIT)
    k.cancel("th_u", keep_partial=True)  # demandé APRÈS le début de l'appel
    go.set()
    th.join(WAIT)
    assert not th.is_alive()
    return out["turn"], calls


def test_keep_partial_keeps_the_text_without_any_token_reader_when_the_upstream_streams():
    turn, calls = _cancel_midway(stream_upstream=True)
    assert json.loads(calls[0].content)["stream"] is True
    assert turn.reply.startswith("Je commence une très longue réponse mot") and turn.reply.endswith("\n\n" + MARKER)
    assert "et encore" not in turn.reply and turn.error == "cancelled"


def test_what_a_token_client_sees_is_unchanged_by_the_flag():
    on, off = Sink(), Sink()
    turn_on, _ = _cancel_midway(stream_upstream=True, sink=on)
    turn_off, _ = _cancel_midway(stream_upstream=False, sink=off)
    assert on.parts == off.parts and on.resets == off.resets and turn_on.reply == turn_off.reply


def test_default_is_off_and_the_upstream_is_then_called_without_stream():
    calls = []
    p = SokkanProvider(base_url="https://amont.invalid/v1", api_key="k", model="m", retry_backoff_s=0.0)
    p._client = httpx.Client(transport=httpx.MockTransport(lambda r: (calls.append(r), httpx.Response(200, json={
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))[1]))
    k, _, _ = kernel([], provider=p)
    assert k.stream_upstream is False
    k.run_turn(TurnInput(thread_id="th_d", user_text="salut", channel="cli"))
    assert "stream" not in json.loads(calls[0].content)


def test_an_ordinary_turn_with_the_flag_on_ends_the_same_way():
    calls = []
    p = SokkanProvider(base_url="https://amont.invalid/v1", api_key="k", model="m", retry_backoff_s=0.0)
    chunk = ('data: {"choices": [{"index": 0, "delta": {"content": "Bonjour"}}]}\n\n'
             'data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n')
    p._client = httpx.Client(transport=httpx.MockTransport(lambda r: (calls.append(r), httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=chunk.encode()))[1]))
    k, _, _ = kernel([], provider=p)
    k.stream_upstream = True
    turn = k.run_turn(TurnInput(thread_id="th_o", user_text="salut", channel="cli"))
    assert json.loads(calls[0].content)["stream"] is True and turn.reply == "Bonjour"
    assert turn.reply != loop_mod.CANCEL_REPLY
