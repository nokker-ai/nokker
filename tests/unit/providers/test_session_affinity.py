"""Affinité de cache de préfixe : `X-Session-Id` du Router = empreinte courte de la clé de session (jamais l'id du fil),
stable pour un fil, distincte entre deux fils, ABSENTE vers tout autre amont. Réponse : `X-Model-Served` et
`X-Sokkan-Upstream` lus en flux comme hors flux ; `cached_reported` vrai seulement si `cached_tokens` est présent."""
from __future__ import annotations

import json

import httpx
import pytest

from nokker.providers.sokkan import SokkanProvider, session_affinity_id

MSGS = [{"role": "user", "content": "ping"}]
ROUTER = "https://router.sokkan.ch/v1"
HDR = {"X-Model-Served": "qwen/qwen3.8-27b", "X-Sokkan-Upstream": "aws-ship"}


def _prov(seen: list, url=ROUTER, usage=None, stream=False, **kw):
    usage = usage if usage is not None else {"prompt_tokens": 10, "completion_tokens": 1}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers)
        if stream:
            chunks = [{"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]},
                      {"choices": [], "usage": usage}, "[DONE]"]
            raw = b"".join(f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n".encode() for c in chunks)
            return httpx.Response(200, content=raw, headers={"Content-Type": "text/event-stream", **HDR})
        body = {"model": "x", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": usage}
        return httpx.Response(200, json=body, headers=HDR)

    p = SokkanProvider(base_url=url, api_key="k", model="m", retry_backoff_s=0.0, **kw)
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    return p


def _ask(p, key, stream=False):
    return p.complete(MSGS, [], reasoning=False, session_key=key, on_delta=(lambda x: None) if stream else None)


@pytest.mark.parametrize("stream", [False, True])
def test_header_is_a_stable_hash_per_thread(stream):
    seen: list = []
    p = _prov(seen, stream=stream)
    _ask(p, "th_alice", stream)
    _ask(p, "th_alice", stream)
    _ask(p, "th_bob", stream)
    a1, a2, b = (h.get("X-Session-Id") for h in seen)
    assert a1 == a2 and a1 != b
    assert a1 == session_affinity_id("th_alice") and "th_alice" not in a1 and len(a1) == 24


@pytest.mark.parametrize("stream", [False, True])
def test_no_header_towards_another_upstream(stream):
    seen: list = []
    _ask(_prov(seen, url="https://api.exemple.test/v1", stream=stream), "th_alice", stream)
    assert "x-session-id" not in seen[0]
    seen.clear()
    _ask(_prov(seen, stream=stream, session_header=False), "th_alice", stream)  # BYOK : drapeau coupé
    assert "x-session-id" not in seen[0]
    seen.clear()
    _ask(_prov(seen, url="https://api.exemple.test/v1", stream=stream, router=True), "th_alice", stream)
    assert "x-session-id" in seen[0]  # palier déclaré Router : l'affinité part


@pytest.mark.parametrize("stream", [False, True])
def test_served_and_upstream_recorded(stream):
    c = _ask(_prov([], stream=stream), "s", stream)
    assert c.model_served == "qwen/qwen3.8-27b" and c.upstream == "aws-ship"


@pytest.mark.parametrize("stream", [False, True])
def test_cached_reported_only_when_the_field_is_present(stream):
    absent = _ask(_prov([], stream=stream), "s", stream)
    assert absent.usage.cached_reported is False and absent.usage.cached == 0
    zero = _ask(_prov([], stream=stream, usage={"prompt_tokens": 10, "completion_tokens": 1,
                                                  "prompt_tokens_details": {"cached_tokens": 0}}), "s", stream)
    assert zero.usage.cached_reported is True and zero.usage.cached == 0
    some = _ask(_prov([], stream=stream, usage={"prompt_tokens": 10, "completion_tokens": 1,
                                                  "prompt_tokens_details": {"cached_tokens": 7}}), "s", stream)
    assert some.usage.cached_reported is True and some.usage.cached == 7


def test_estimate_is_measured_against_prompt_tokens():
    """Le calibrateur reçoit le ratio réel/estimé de chaque appel (l'estimateur a sous-estimé de ~10 k sur 30 k)."""
    from nokker.context.tokens import CALIBRATOR

    CALIBRATOR.reset()
    p = _prov([], usage={"prompt_tokens": 30000, "completion_tokens": 1})
    _ask(p, "s")
    assert CALIBRATOR.factor("m") > 1.5  # 30 000 comptés par l'amont pour un message de quelques jetons : borné à 2,5
    CALIBRATOR.reset()
