"""Un quota d'inférence atteint (403/429 `quota_exceeded`) n'est pas une panne pour le fournisseur : une seule requête, ni
attente de « clé saturée », ni réessai, ni appel du repli d'un fournisseur à couverture (`HedgedProvider`)."""
from __future__ import annotations

import httpx
import pytest

from nokker.core.loop import person_failure_class
from nokker.core.quota_error import quota_in_chain
from nokker.core.turn import Tier, TurnInput
from nokker.providers.sokkan import SokkanProvider
from tests.unit.test_loop_contract import kernel
from tests.unit.test_quota_class import BODY_BARE, BODY_RETRY, RETRY, URL, _Failing


def _provider(handler, **kw):
    p = SokkanProvider(base_url=URL, api_key="k", model="m", retry_backoff_s=0.0, **kw)
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    return p


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("stream", [False, True])
def test_provider_does_not_retry_nor_wait_on_a_quota_refusal(status, stream, monkeypatch):
    calls = []
    monkeypatch.setattr("nokker.providers.sokkan.time.sleep", lambda s: calls.append(("sleep", s)))

    def handler(req):
        calls.append(req)
        return httpx.Response(status, text=BODY_RETRY)

    p = _provider(handler)
    with pytest.raises(RuntimeError) as e:
        p.complete([{"role": "user", "content": "x"}], [], reasoning=False, session_key="s",
                   on_delta=(lambda t: None) if stream else None)
    assert len(calls) == 1 and person_failure_class(e.value) == "quota" and quota_in_chain(e.value) == {"retry_at": RETRY}


def test_provider_still_retries_a_plain_429(monkeypatch):
    calls = []
    monkeypatch.setattr("nokker.providers.sokkan.time.sleep", lambda s: None)

    def handler(req):
        calls.append(req)
        if len(calls) < 3:
            return httpx.Response(429, text="too many")
        return httpx.Response(200, json={"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                                      "finish_reason": "stop"}], "usage": {}})

    out = _provider(handler).complete([{"role": "user", "content": "x"}], [], reasoning=False, session_key="s")
    assert len(calls) == 3 and out.message.content == "ok"


def test_whole_turn_through_the_real_provider_asks_the_upstream_once(monkeypatch):
    calls = []
    monkeypatch.setattr("nokker.providers.sokkan.time.sleep", lambda s: None)
    p = _provider(lambda req: (calls.append(req), httpx.Response(403, text=BODY_RETRY))[1])
    k, _t, _s, _d = kernel([])
    k.providers = {Tier.SHIP: p, Tier.DEEP: p}
    turn = k.run_turn(TurnInput("t_real", "salut"))
    assert len(calls) == 1 and turn.failure["code"] == "quota_exceeded" and turn.failure["retry_at"] == RETRY


def test_hedged_provider_does_not_call_its_fallback_on_a_quota_refusal():
    from nokker.providers.hedged import HedgedProvider

    fallback = _Failing(RuntimeError("ne doit pas être appelé"))
    primary = _Failing(RuntimeError(f"HTTP 403 de {URL}: {BODY_BARE}"))
    h = HedgedProvider(primary=primary, fallback=fallback, hedge_after_s=5.0)
    with pytest.raises(RuntimeError):
        h.complete([{"role": "user", "content": "x"}], [], reasoning=False, session_key="s")
    assert primary.calls == 1 and fallback.calls == 0


@pytest.mark.parametrize("stream", [False, True])
def test_provider_tags_the_error_from_the_whole_body_and_keeps_the_message_short(stream, monkeypatch):
    import json as _json

    monkeypatch.setattr("nokker.providers.sokkan.time.sleep", lambda s: None)
    body = _json.dumps({"noise": "x" * 8000, "error": {"code": "quota_exceeded", "retry_at": RETRY}})
    calls = []
    p = _provider(lambda req: (calls.append(req), httpx.Response(429, text=body))[1])
    with pytest.raises(RuntimeError) as e:
        p.complete([{"role": "user", "content": "x"}], [], reasoning=False, session_key="s",
                   on_delta=(lambda t: None) if stream else None)
    assert len(calls) == 1 and len(str(e.value)) < 700
    assert e.value.error_code == "quota_exceeded" and person_failure_class(e.value) == "quota"
    assert quota_in_chain(e.value) == {"retry_at": RETRY}
