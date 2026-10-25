"""`SokkanProvider` — aucun réseau : `httpx.MockTransport` simule le Router."""
from __future__ import annotations

import json
import time

import httpx
import pytest

from nokker.core.turn import Tier
from nokker.providers.sokkan import SokkanProvider


def _provider(handler) -> SokkanProvider:
    p = SokkanProvider(base_url="https://router.sokkan.ch/v1", api_key="k", model="qwen/qwen3.8-27b")
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    return p


def test_cached_tokens_reported_when_field_present():
    def handler(request):
        return httpx.Response(200, json={
            "model": "qwen/qwen3.8-27b",
            "choices": [{"message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                      "prompt_tokens_details": {"cached_tokens": 3}},
        })
    comp = _provider(handler).complete([{"role": "user", "content": "ping"}], [], reasoning=False,
                                        session_key="t1")
    assert comp.usage.cached == 3 and comp.usage.cached_reported is True


def test_cached_tokens_not_reported_when_field_absent():
    def handler(request):
        return httpx.Response(200, json={
            "model": "m", "choices": [{"message": {"content": "x"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        })
    comp = _provider(handler).complete([], [], reasoning=False, session_key="t1")
    assert comp.usage.cached == 0 and comp.usage.cached_reported is False


def test_reasoning_tokens_parsed_when_present():
    def handler(request):
        return httpx.Response(200, json={
            "model": "m", "choices": [{"message": {"content": "x"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "completion_tokens_details": {"reasoning_tokens": 42}},
        })
    comp = _provider(handler).complete([], [], reasoning=True, session_key="t1")
    assert comp.usage.reasoning == 42


def test_reasoning_off_sends_reasoning_effort_none():
    # reasoning_effort: "none" est la SEULE variante vérifiée en réel qui coupe le raisonnement sans
    # échec HTTP sur le Router (OVH qwen3.8-27b/qwen3.5-397b-a17b, 17.10.2026) — chat_template_kwargs et
    # reasoning.enabled renvoient 400 upstream sur ces modèles.
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "x"}}],
                                          "usage": {}})
    _provider(handler).complete([], [], reasoning=False, session_key="t1")
    assert seen["body"]["reasoning_effort"] == "none"


def test_reasoning_on_does_not_send_reasoning_effort():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "x"}}],
                                          "usage": {}})
    _provider(handler).complete([], [], reasoning=True, session_key="t1")
    assert "reasoning_effort" not in seen["body"]


def test_reasoning_off_body_is_overridable_for_llamacpp_hote21():
    # hote21 (llama.cpp, Qwen3.6-35B-A3B) accepte lui chat_template_kwargs.enable_thinking — un provider
    # pointé sur hote21 doit pouvoir surcharger reasoning_off_body sans toucher le code.
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "x"}}],
                                          "usage": {}})
    p = _provider(handler)
    p.reasoning_off_body = {"chat_template_kwargs": {"enable_thinking": False}}
    p.complete([], [], reasoning=False, session_key="t1")
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_session_key_sent_as_header():
    seen = {}

    def handler(request):
        seen["header"] = request.headers.get("x-session-id")
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "x"}}],
                                          "usage": {}})
    _provider(handler).complete([], [], reasoning=False, session_key="fil-42")
    assert seen["header"] == "fil-42"


def test_empty_api_key_sends_no_authorization_header():
    # Régression réelle (hote21:8006, llama.cpp sans auth) : `Bearer ` (vide) est un en-tête HTTP
    # illégal pour httpx/http.client — un amont sans clé ne doit recevoir AUCUN en-tête Authorization.
    seen = {}

    def handler(request):
        seen["has_auth"] = "authorization" in request.headers
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "x"}}],
                                          "usage": {}})
    p = SokkanProvider(base_url="http://hote21.invalid/v1", api_key="", model="qwen3.6-35b-a3b")
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    p.complete([], [], reasoning=False, session_key="t1")
    assert seen["has_auth"] is False


def test_tool_calls_parsed():
    def handler(request):
        return httpx.Response(200, json={
            "model": "m",
            "choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "calendar_list", "arguments": '{"when":"today"}'}}]}}],
            "usage": {},
        })
    comp = _provider(handler).complete([], [{"type": "function", "function": {"name": "calendar_list"}}],
                                        reasoning=False, session_key="t1")
    assert len(comp.message.tool_calls) == 1
    tc = comp.message.tool_calls[0]
    assert tc.name == "calendar_list" and tc.arguments == {"when": "today"}


def test_retries_on_network_error_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "ok"}}],
                                          "usage": {}})
    p = _provider(handler)
    p.retry_backoff_s = 0.0
    comp = p.complete([], [], reasoning=False, session_key="t1")
    assert comp.message.content == "ok" and calls["n"] == 3


def test_raises_after_retries_exhausted():
    def handler(request):
        raise httpx.ConnectError("toujours en panne")
    p = _provider(handler)
    p.retry_backoff_s = 0.0
    with pytest.raises(RuntimeError):
        p.complete([], [], reasoning=False, session_key="t1")


def test_tier_enum_has_no_frontier():
    assert {t.value for t in Tier} == {"local", "small", "ship", "deep"}  # X8 : petit palier, jamais « frontier »


def test_attempts_is_one_on_first_try_success():
    comp = _provider(lambda r: httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "x"}}],
                                                          "usage": {}})).complete([], [], reasoning=False,
                                                                                   session_key="t1")
    assert comp.attempts == 1


def test_attempts_reflects_retry_count_on_eventual_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(502, json={"error": "upstream"})
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "ok"}}], "usage": {}})
    p = _provider(handler)
    p.retry_backoff_s = 0.0
    comp = p.complete([], [], reasoning=False, session_key="t1")
    assert comp.attempts == 2 and calls["n"] == 2


def test_total_budget_stops_retrying_before_max_retries_on_repeated_504():
    """P1-S1 (diag latence, 17.10.2026) — mesuré en réel : un 504 de router.sokkan.ch veut dire que le
    broker a DÉJÀ attendu tout son budget interne (first_token_timeout_s, 60 s par défaut) sans qu'aucun
    amont ne réponde ; réessayer une fois peut réussir (mesuré : 8,5-20,9 s), mais un 2e 504 d'affilée ne
    doit PAS déclencher un 3e essai (ça pousserait un seul tour à ~180 s, exactement le mécanisme derrière
    les 64 s/135 s observés en F7). Chaque essai simulé consomme un peu de temps RÉEL (sleep court) pour
    que le budget s'épuise sans dépendre d'une horloge simulée."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        time.sleep(0.03)
        return httpx.Response(504, json={"error": "no upstream answered in time"})
    p = _provider(handler)
    p.retry_backoff_s = 0.0
    p.max_retries = 2          # 3 essais possibles normalement
    p.total_budget_s = 0.05    # épuisé après ~2 essais de 0,03s
    with pytest.raises(RuntimeError, match="budget total"):
        p.complete([], [], reasoning=False, session_key="t1")
    assert calls["n"] == 2  # pas de 3e essai : le budget a coupé la boucle avant


def test_total_budget_does_not_shorten_the_single_observed_recovery_case():
    """Le cas RÉELLEMENT mesuré (1 essai à ~60s/504 + 1 réessai réussi en 8,5-20,9s) doit continuer à
    réussir avec le budget par défaut (80s) — le garde-fou ne coupe qu'un 2e échec d'affilée, jamais le
    1er réessai qui a des chances réelles de réussir."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(504, json={"error": "no upstream answered in time"})
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "ok"}}], "usage": {}})
    p = _provider(handler)
    p.retry_backoff_s = 0.0
    comp = p.complete([], [], reasoning=False, session_key="t1")
    assert comp.message.content == "ok" and comp.attempts == 2


def test_concurrency_429_waits_for_a_free_slot_without_spending_retries(monkeypatch):
    """20.10.2026 : « too many concurrent requests for this key (max 4) » — une place se libère quand un autre appel
    finit ; on attend au lieu d'abandonner (3 réessais en 3 s puis escalade, vu en réel)."""
    sleeps = []
    monkeypatch.setattr("nokker.providers.sokkan.time.sleep", lambda s: sleeps.append(s))
    answers = iter([429] * 5 + [200])

    def handler(request):
        code = next(answers)
        if code == 429:
            return httpx.Response(429, json={"error": {"message": "too many concurrent requests for this key (max 4)"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    p = SokkanProvider(base_url="https://r/v1", api_key="k", model="m", max_retries=2)
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    comp = p.complete([{"role": "user", "content": "x"}], [], reasoning=False, session_key="s")
    assert comp.message.content == "ok"
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 8.0]
    assert comp.attempts == 6  # 5 × 429 + 1 : chaque requête HTTP compte (verdict router:retried au ledger)


def test_closing_a_detached_provider_stops_its_retries():
    """HedgedProvider ferme le primaire abandonné : l'appel en vol va à son terme (un recv bloqué ne se réveille pas
    d'un autre thread), mais AUCUN réessai ne part ensuite — la place de la clé n'est plus tenue jusqu'à 3 × 60 s.
    L'original reste utilisable."""
    import http.server
    import socketserver
    import threading
    import time as _t

    hits = []

    class Failing(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            hits.append(1)
            _t.sleep(0.5)
            self.send_response(504)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Failing)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = SokkanProvider(base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1", api_key="", model="m",
                              max_retries=2, retry_backoff_s=0.2)
        twin = base.detached()
        assert twin._client is not base._client
        out = {}

        def run():
            try:
                twin.complete([{"role": "user", "content": "x"}], [], reasoning=False, session_key="s")
            except Exception as e:  # noqa: BLE001
                out["error"] = e

        th = threading.Thread(target=run)
        th.start()
        _t.sleep(0.1)
        twin.close()
        th.join(5)
        assert not th.is_alive() and "error" in out
        assert len(hits) == 1  # sans fermeture : 3 appels (504 réessayé deux fois)
        assert not base._client.is_closed
    finally:
        srv.shutdown()
