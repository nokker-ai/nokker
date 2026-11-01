"""Annulation PENDANT un appel modèle en flux (D6, e2e du 1.11) : `cancel()` coupe le flux au prochain morceau, ferme la
réponse HTTP de l'amont, et le tour finit sur « Annulé à ta demande. » (texte partiel non gardé), messages bien formés."""
from __future__ import annotations

import json
import threading

import httpx

from nokker.core.loop import CANCEL_REPLY
from nokker.core.turn import TurnInput
from nokker.providers.hedged import HedgedProvider
from nokker.providers.sokkan import SokkanProvider
from tests.unit.test_pause_steer import WAIT, kernel


class Sink:
    def __init__(self):
        self.parts: list[str] = []
        self.resets = 0

    def delta(self, text):
        self.parts.append(text)

    def reset(self):
        self.resets += 1


class SlowStream(httpx.SyncByteStream):
    """Un morceau, puis attend le feu vert du test avant d'envoyer la suite ; note la fermeture de la réponse."""

    def __init__(self, go: threading.Event, sent: threading.Event):
        self.go, self.sent, self.closed, self.after = go, sent, False, 0

    def __iter__(self):
        yield _chunk("Je commence une très longue réponse " + "mot " * 60)  # au-delà de la retenue : affiché
        self.sent.set()
        self.go.wait(WAIT)
        for _ in range(50):
            self.after += 1
            yield _chunk("et encore ")
        yield b'data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n'

    def close(self):
        self.closed = True


def _chunk(text: str) -> bytes:
    return f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'content': text}}]})}\n\n".encode()


def _provider(stream_obj, calls):
    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=stream_obj)

    p = SokkanProvider(base_url="https://amont.invalid/v1", api_key="k", model="m", retry_backoff_s=0.0)
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    return p


def _run_cancelled(provider, go, sent, k, sink):
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("turn", k.run_turn(
        TurnInput(thread_id="th_c", user_text="écris beaucoup", channel="cli"), stream=sink)))
    th.start()
    assert sent.wait(WAIT)
    k.cancel("th_c")
    go.set()
    th.join(WAIT)
    assert not th.is_alive()
    return out["turn"]


def test_cancel_cuts_the_stream_at_the_next_piece_and_closes_the_response():
    go, sent = threading.Event(), threading.Event()
    body, calls = SlowStream(go, sent), []
    k, _, _ = kernel([], provider=_provider(body, calls))
    sink = Sink()
    turn = _run_cancelled(None, go, sent, k, sink)
    assert body.closed and body.after <= 1  # coupé au premier morceau d'après l'annulation
    assert len(calls) == 1  # ni rejeu, ni repli de palier
    assert turn.error == "cancelled" and turn.reply == CANCEL_REPLY and not turn.escalated
    assert [m.role for m in turn.messages] == ["assistant"] and turn.messages[0].content == CANCEL_REPLY
    assert "très longue" not in turn.reply  # texte partiel non gardé
    assert sink.resets >= 1  # le client vide le texte partiel déjà affiché
    assert any(v.rule == "loop:cancelled_stream" for v in turn.verdicts)
    assert "th_c" not in k._cancelled  # l'annulation est consommée : le tour suivant n'en hérite pas


def test_cancel_through_the_hedged_provider_too():
    go, sent = threading.Event(), threading.Event()
    body, calls = SlowStream(go, sent), []
    fallback_calls: list = []
    primary = _provider(body, calls)
    primary.detached = lambda: primary  # même transport factice (le vrai `detached` ouvre un client réseau)
    hedged = HedgedProvider(primary, _provider(SlowStream(threading.Event(), threading.Event()), fallback_calls),
                            hedge_after_s=5.0)
    k, _, _ = kernel([], provider=hedged)
    turn = _run_cancelled(None, go, sent, k, Sink())
    assert turn.error == "cancelled" and turn.reply == CANCEL_REPLY and body.closed
    assert fallback_calls == []  # une annulation n'est pas une panne du primaire


def test_messages_stay_well_formed_when_cancelled_after_tools():
    """Annulé pendant le 2e appel (après un outil) : assistant(tool_calls) → tool → assistant « annulé »."""
    go, sent = threading.Event(), threading.Event()
    body = SlowStream(go, sent)
    first = {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                                                "function": {"name": "read",
                                                                             "arguments": "{}"}}]},
                          "finish_reason": "tool_calls"}]}
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, content=f"data: {json.dumps(first)}\n\ndata: [DONE]\n\n".encode())
        return httpx.Response(200, stream=body)

    p = SokkanProvider(base_url="https://amont.invalid/v1", api_key="k", model="m", retry_backoff_s=0.0)
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    k, _, _ = kernel([], provider=p)
    turn = _run_cancelled(None, go, sent, k, Sink())
    assert [m.role for m in turn.messages] == ["assistant", "tool", "assistant"]
    assert turn.messages[0].tool_calls[0].id == turn.messages[1].tool_call_id == "c1"
    assert turn.messages[-1].content == CANCEL_REPLY and turn.error == "cancelled"
