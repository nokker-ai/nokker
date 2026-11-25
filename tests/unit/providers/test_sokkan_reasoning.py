"""Réflexion de l'amont dans `SokkanProvider` (docs/NOKKAN-HOST-API.md §3).

Contrat RÉEL : `tests/fixtures/providers/vllm-0.32-reasoning-stream.sse` est la réponse brute du vLLM 0.32 local
(modèle 27B, `stream: true`, réflexion active), capturée le 11.11.2026 : la réflexion y arrive dans `delta.reasoning`,
la réponse dans `delta.content`. La forme non streamée (`message.reasoning`) vient de la documentation de vLLM
(« Reasoning Outputs »), `reasoning_content` de son ancien nom, encore servi par d'autres amonts compatibles.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from nokker.providers.sokkan import SokkanProvider, StreamInterrupted
from tests.unit.providers.test_sokkan_stream import _BreaksAfter, _chunk, _provider, _sse

CAPTURE = Path(__file__).parents[2] / "fixtures" / "providers" / "vllm-0.32-reasoning-stream.sse"
THOUGHT = ("Nous devons répondre uniquement par le nombre. 17*23 = 17*20 + 17*3 = 340 + 51 = 391. L'utilisateur "
           "demande de répondre par le nombre seul. Pas besoin d'outils. Final: 391.\n")


def _capture_provider() -> SokkanProvider:
    body = CAPTURE.read_bytes()
    return _provider(lambda request: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body))


def _complete(p, **kw):
    return p.complete([{"role": "user", "content": "x"}], [], reasoning=True, session_key="s", **kw)


def test_real_vllm_capture_reasoning_goes_to_its_own_channel_never_into_content():
    text, think = [], []
    comp = _complete(_capture_provider(), on_delta=text.append, on_reasoning_delta=think.append)
    assert "".join(think) == THOUGHT and comp.reasoning == THOUGHT
    assert "".join(text) == "391" and comp.message.content == "391"  # la réponse seule
    assert len(think) > 5  # arrivée morceau par morceau, pas d'un bloc
    assert comp.usage.reasoning == 67 and comp.usage.reasoning_reported and comp.finish_reason == "stop"
    # le corps reconstitué (`raw`) ne porte pas la réflexion : rien ne peut la remettre dans un message
    assert comp.raw["choices"][0]["message"] == {"role": "assistant", "content": "391"}
    assert "Nous devons" not in json.dumps(comp.raw, ensure_ascii=False)


def test_real_vllm_capture_without_the_callback_is_the_same_completion_as_before():
    text = []
    comp = _complete(_capture_provider(), on_delta=text.append)
    assert "".join(text) == "391" and comp.message.content == "391" and comp.message.tool_calls == []
    assert comp.raw == {"model": "qwen3.8-27b",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "391"},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 71, "total_tokens": 144, "completion_tokens": 73,
                                  "prompt_tokens_details": {"cached_tokens": 0, "created_cache_tokens": 0},
                                  "completion_tokens_details": {"reasoning_tokens": 67}}}
    assert comp.reasoning == THOUGHT  # rendue à côté du message ; lue seulement drapeau allumé


@pytest.mark.parametrize("key", ["reasoning", "reasoning_content"])
def test_non_stream_reasoning_field_of_the_message(key):
    def handler(request):
        return httpx.Response(200, json={"model": "m", "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": "391", key: "17*23 = 391."}}], "usage": {"prompt_tokens": 5}})

    comp = _complete(_provider(handler))
    assert comp.reasoning == "17*23 = 391." and comp.message.content == "391"
    assert comp.message.to_openai() == {"role": "assistant", "content": "391"}  # ce qui retourne au modèle


@pytest.mark.parametrize("key", ["reasoning", "reasoning_content"])
def test_stream_reasoning_under_both_names(key):
    stream = [_chunk({"role": "assistant", "content": ""}), _chunk({key: "Je calcule. "}), _chunk({key: "391."}),
              _chunk({"content": "391"}), _chunk({}, finish="stop"), "[DONE]"]
    think = []
    comp = _complete(_provider(lambda r: httpx.Response(200, content=_sse(stream))), on_delta=lambda t: None,
                     on_reasoning_delta=think.append)
    assert think == ["Je calcule. ", "391."] and comp.reasoning == "Je calcule. 391." and comp.message.content == "391"


def test_same_text_under_both_names_is_not_doubled():
    stream = [_chunk({"reasoning": "Je calcule.", "reasoning_content": "Je calcule."}), _chunk({"content": "391"}),
              _chunk({}, finish="stop"), "[DONE]"]
    comp = _complete(_provider(lambda r: httpx.Response(200, content=_sse(stream))), on_delta=lambda t: None)
    assert comp.reasoning == "Je calcule."


def test_no_reasoning_from_upstream_is_an_empty_string():
    stream = [_chunk({"content": "391"}), _chunk({}, finish="stop"), "[DONE]"]
    assert _complete(_provider(lambda r: httpx.Response(200, content=_sse(stream))),
                     on_delta=lambda t: None, on_reasoning_delta=lambda t: None).reasoning == ""


def _breaking(calls):
    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, stream=_BreaksAfter(_sse([_chunk({"reasoning": "Je réfléchis "})])))
        return httpx.Response(200, content=_sse([_chunk({"reasoning": "Je recommence."}), _chunk({"content": "ok"}),
                                                 _chunk({}, finish="stop"), "[DONE]"]))

    return handler


def test_flux_cut_after_reasoning_shown_to_the_client_is_never_silently_retried():
    calls = {"n": 0}
    think = []
    with pytest.raises(StreamInterrupted):
        _complete(_provider(_breaking(calls)), on_delta=lambda t: None, on_reasoning_delta=think.append)
    assert calls["n"] == 1 and think == ["Je réfléchis "]


def test_flux_cut_during_reasoning_nobody_saw_is_retried_as_before():
    calls = {"n": 0}
    comp = _complete(_provider(_breaking(calls)), on_delta=lambda t: None)
    assert calls["n"] == 2 and comp.message.content == "ok"
    assert comp.reasoning == "Je recommence."  # la réflexion de l'essai abandonné n'est pas gardée
