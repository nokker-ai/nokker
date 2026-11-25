"""En-têtes de réponse du Router : `X-Model-Served` (modèle servi, prioritaire) et `X-Sokkan-Upstream` (id de l'amont).
Un amont Router sans en-tête : `model_served` NULL (le corps est réécrit) ; un amont direct : le corps, comme avant.
Faux amont HTTP (httpx.MockTransport), aucun réseau."""
from __future__ import annotations

import json

import httpx
import pytest

from nokker.byok.service import ByokProvider
from nokker.core.model_served import clean_served, is_router_url, served_from_headers
from nokker.core.ports import Completion
from nokker.core.turn import Message, Tier, TurnInput
from nokker.providers.sokkan import SokkanProvider
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

from . import test_loop_contract as loop
from .traces import test_traces as tr

MSGS = [{"role": "user", "content": "ping"}]
ROUTER = "https://router.sokkan.ch/v1"
HDR = {"X-Model-Served": "qwen/qwen3.8-27b", "X-Sokkan-Upstream": "aws-ship"}


def _prov(handler, url=ROUTER, cls=SokkanProvider, **kw):
    p = cls(base_url=url, api_key="k", model="qwen/qwen3.8-27b", retry_backoff_s=0.0, **kw)
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    return p


def _body(model="autre/modele"):
    return {"model": model, "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def _json(headers=None, model="autre/modele"):
    return lambda r: httpx.Response(200, json=_body(model), headers=headers or {})


def _sse(model="autre/modele", headers=None):
    chunks = [{"model": model, "choices": [{"index": 0, "delta": {"content": "po"}, "finish_reason": None}]},
              {"choices": [{"index": 0, "delta": {"content": "ng"}, "finish_reason": "stop"}]},
              {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}, "[DONE]"]
    raw = b"".join(f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n".encode() for c in chunks)
    return lambda r: httpx.Response(200, content=raw, headers={"Content-Type": "text/event-stream", **(headers or {})})


def _ask(p, stream=False):
    return p.complete(MSGS, [], reasoning=False, session_key="s", on_delta=(lambda x: None) if stream else None)


@pytest.mark.parametrize("stream", [False, True])
def test_header_wins_over_a_body_that_says_something_else(stream):
    p = _prov(_sse(headers=HDR) if stream else _json(HDR))
    c = _ask(p, stream)
    assert c.model_served == "qwen/qwen3.8-27b" and c.upstream == "aws-ship"
    assert c.model == "autre/modele"  # `model` du corps : inchangé


@pytest.mark.parametrize("stream", [False, True])
def test_header_names_are_case_insensitive(stream):
    h = {"x-model-served": "m-lower", "X-SOKKAN-UPSTREAM": "fallback-397b"}
    c = _ask(_prov(_sse(headers=h) if stream else _json(h)), stream)
    assert (c.model_served, c.upstream) == ("m-lower", "fallback-397b")


@pytest.mark.parametrize("stream", [False, True])
def test_router_without_header_is_null_even_if_the_body_names_a_model(stream):
    c = _ask(_prov(_sse() if stream else _json()), stream)
    assert c.model_served is None and c.upstream is None


@pytest.mark.parametrize("stream", [False, True])
def test_explicit_router_flag_on_another_host_and_explicit_false_on_the_router_host(stream):
    h = _sse() if stream else _json()
    assert _ask(_prov(h, url="https://pre.invalid/v1", router=True), stream).model_served is None
    assert _ask(_prov(h, router=False), stream).model_served == "autre/modele"


@pytest.mark.parametrize("stream", [False, True])
def test_direct_upstream_keeps_the_body_and_the_header_still_wins(stream):
    h = _sse if stream else _json
    assert _ask(_prov(h(), url="http://vllm.invalid/v1"), stream).model_served == "autre/modele"
    assert _ask(_prov(h(), url="http://vllm.invalid/v1", cls=ByokProvider), stream).model_served == "autre/modele"
    # un point d'accès BYOK sur l'hôte du Router n'est jamais « le Router »
    assert _ask(_prov(h(), cls=ByokProvider), stream).model_served == "autre/modele"
    c = _ask(_prov(h(headers=HDR), url="http://vllm.invalid/v1"), stream)
    assert c.model_served == "qwen/qwen3.8-27b"


@pytest.mark.parametrize("stream", [False, True])
def test_invalid_long_or_control_headers_are_null_or_truncated(stream):
    h = _sse if stream else _json
    bad = _ask(_prov(h(headers={"X-Model-Served": "   ", "X-Sokkan-Upstream": ""})), stream)
    assert bad.model_served is None and bad.upstream is None
    long = _ask(_prov(h(headers={"X-Model-Served": "m" * 900, "X-Sokkan-Upstream": "u" * 900})), stream)
    assert long.model_served == "m" * 200 and long.upstream == "u" * 200
    assert clean_served("a\x07b\x1fc ") == "abc"


def test_helpers():
    assert served_from_headers(None) == (None, None) and served_from_headers({}) == (None, None)
    assert served_from_headers(httpx.Headers(HDR)) == ("qwen/qwen3.8-27b", "aws-ship")
    assert is_router_url(ROUTER) and is_router_url("https://ROUTER.sokkan.ch") and not is_router_url("http://x.invalid")
    assert not is_router_url("") and not is_router_url("https://evil.invalid/router.sokkan.ch")


def test_hedged_provider_keeps_the_header_of_the_retained_completion():
    from nokker.providers.hedged import HedgedProvider

    def prov(headers):  # `detached()` rebâtit le client : la fabrique garde le faux amont
        return SokkanProvider(base_url=ROUTER, api_key="k", model="m", client_factory=lambda t: httpx.Client(
            transport=httpx.MockTransport(_json(headers)), timeout=t))

    primary = prov({"X-Model-Served": "m-primary", "X-Sokkan-Upstream": "aws-ship"})
    fallback = prov({"X-Model-Served": "m-fallback", "X-Sokkan-Upstream": "fallback-397b"})
    c = HedgedProvider(primary, fallback, hedge_after_s=30.0).complete(MSGS, [], reasoning=False, session_key="s")
    assert (c.model_served, c.upstream) == ("m-primary", "aws-ship")


# ───────────────────────────── noyau : événement, traces ─────────────────────────────


class Served(loop.ScriptedProvider):
    def __init__(self, script, served, upstream, model="qwen3.8-27b"):
        super().__init__(script, model=model)
        self.served, self.up = list(served), list(upstream)

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        comp = super().complete(messages, tools, reasoning=reasoning, session_key=session_key, max_tokens=max_tokens)
        return Completion(message=comp.message, usage=comp.usage, model=comp.model, latency_s=comp.latency_s,
                          model_served=self.served.pop(0), upstream=self.up.pop(0))


class Obs:
    def __init__(self):
        self.events = []

    def route_model_mismatch(self, turn, data):
        self.events.append(dict(data))


def _run(served, upstream):
    script = [Message("assistant", tool_calls=[loop.tc("calendar_list", day=str(i))]) for i in range(len(served) - 1)]
    script.append(Message("assistant", "voilà"))
    k, *_ = loop.kernel([])
    k.providers[Tier.SHIP] = Served(script, served, upstream)
    k.observer = Obs()
    k.run_turn(TurnInput("t1", "mon agenda ?"))
    return k


def test_mismatch_from_a_fallback_header_fires_once_and_carries_the_upstream():
    k = _run(["qwen/qwen3.5-397b-a17b", "qwen/qwen3.5-397b-a17b"], ["fallback-397b", "fallback-397b"])
    assert len(k.observer.events) == 1
    ev = k.observer.events[0]
    assert ev["model_served"] == "qwen/qwen3.5-397b-a17b" and ev["upstream"] == "fallback-397b"


def test_equivalent_names_and_unknown_served_model_fire_nothing():
    k = _run(["qwen/QWEN3.8-27b", "qwen3.8-27b-20261101"], ["aws-ship", "aws-ship"])
    assert k.observer.events == []
    k = _run([None, None], [None, None])  # amont Router sans en-tête : model_served NULL, pas d'événement
    assert k.observer.events == [] and [c.model_served for c in k.ledger.calls] == [None, None]


def test_event_without_upstream_has_no_upstream_key():
    k = _run(["qwen/qwen3.5-397b-a17b"], [None])
    assert "upstream" not in k.observer.events[0]


class TracedProvider(tr.Provider):
    def __init__(self, script, served, up):
        super().__init__(script)
        self.served, self.up = list(served), list(up)

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        comp = super().complete(messages, tools, reasoning=reasoning, session_key=session_key)
        return Completion(message=comp.message, usage=comp.usage, model=comp.model, latency_s=comp.latency_s,
                          model_served=self.served.pop(0), upstream=self.up.pop(0))


def test_traces_carry_the_upstream_per_call_and_export_it_after_model_served(tmp_path):
    rec = tr.recorder()
    k, _ = tr.make_kernel([], rec)
    k.providers[Tier.SHIP] = TracedProvider(list(tr.THREE_CALLS), ["a", None, "c"], ["aws-ship", None, "fallback-397b"])
    with tenant_scope(INTERNAL_TENANT):
        k.run_turn(TurnInput("fil", "mon agenda ?"))
    assert [r["meta"]["upstream"] for r in rec.sink.rows] == ["aws-ship", None, "fallback-397b"]
    lot = tr.export(tr.source_from(rec.sink.rows), tmp_path, tr.opts(), exporter_sha="abc1234").path
    metas = [ln["meta"] for ln in tr.read_lines(lot)]
    assert [m["upstream"] for m in metas] == ["aws-ship", None, "fallback-397b"]
    assert list(metas[0])[-2:] == ["model_served", "upstream"]
    old = [{**r, "meta": {k_: v for k_, v in r["meta"].items() if k_ != "upstream"}} for r in rec.sink.rows]
    lot2 = tr.export(tr.source_from(old), tmp_path / "b", tr.opts(), exporter_sha="abc1234").path
    assert [ln["meta"]["upstream"] for ln in tr.read_lines(lot2)] == [None, None, None]


def test_turn_json_roundtrips_upstream():
    from nokker.core.turn import LLMCall, Usage
    from nokker.store.pg import _dec_call, _enc_call

    c = LLMCall(tier=Tier.SHIP, model="m", reasoning=False, prefix_sha256="", layer_tokens={}, usage=Usage(1, 1),
                latency_s=0.1, route_reason="r", upstream="aws-ship")
    assert _dec_call(_enc_call(c)).upstream == "aws-ship"
    assert _dec_call({k: v for k, v in _enc_call(c).items() if k != "upstream"}).upstream is None


def test_tier_router_flag_parses_and_reaches_the_provider(tmp_path):
    from nokker.config import load_config
    from nokker.providers.select import tier_provider

    f = tmp_path / "c.yaml"
    f.write_text("tiers:\n  ship: {base_url: 'https://pre.invalid/v1', model: m, router: true}\n"
                 "  deep: {base_url: 'https://router.sokkan.ch/v1', model: m}\n")
    cfg = load_config(str(f))
    assert tier_provider(cfg.tiers["ship"]).behind_router is True
    assert cfg.tiers["deep"].router is None and tier_provider(cfg.tiers["deep"]).behind_router is True
    f.write_text("tiers:\n  ship: {base_url: 'https://x.invalid/v1', model: m, router: 'oui'}\n")
    with pytest.raises(ValueError):
        load_config(str(f))


# ───────────────────────────── X-Nokker-Client ─────────────────────────────


@pytest.fixture
def tag():
    from nokker.providers import sokkan

    before = sokkan._client_tag
    yield sokkan.reset_client_tag  # pose sans verrou : l'étiquette réelle ne se pose qu'une fois (voir plus bas)
    sokkan.reset_client_tag(before)


def _spy(stream, seen):
    inner = _sse() if stream else _json()

    def handler(r):
        seen.append({k.lower(): v for k, v in r.headers.items()})
        return inner(r)
    return handler


@pytest.mark.parametrize("stream", [False, True])
def test_client_header_is_sent_to_the_router_default_and_configured(stream, tag):
    seen: list = []
    _ask(_prov(_spy(stream, seen)), stream)
    assert seen[-1]["x-nokker-client"] == "nokker"
    tag("canary")
    _ask(_prov(_spy(stream, seen)), stream)
    assert seen[-1]["x-nokker-client"] == "canary"


@pytest.mark.parametrize("stream", [False, True])
def test_client_header_never_goes_to_byok_direct_upstreams_or_anthropic(stream, tag):
    from nokker.providers.anthropic import AnthropicProvider

    tag("canary")
    seen: list = []
    _ask(_prov(_spy(stream, seen), cls=ByokProvider), stream)  # BYOK, même sur l'hôte du Router
    _ask(_prov(_spy(stream, seen), url="http://vllm.invalid/v1"), stream)
    assert seen and all("x-nokker-client" not in h for h in seen)
    a = AnthropicProvider(base_url="https://router.sokkan.ch", api_key="k", model="m")
    assert "x-nokker-client" not in {k.lower() for k in a._headers("id")}


def test_client_tag_validation_is_strict_and_invalid_is_a_config_error(tmp_path, tag):
    from nokker.config import client_tag_of, load_config

    assert client_tag_of({}) == "nokker" and client_tag_of(None) == "nokker"
    assert client_tag_of({"client_tag": "canary-2"}) == "canary-2" and client_tag_of({"client_tag": "a" * 32})
    for bad in ("", "Canary", "a" * 33, "a b", "a\n", "a_b", "é", 7, ["x"], "tenant@x.ch"):
        with pytest.raises(ValueError):
            client_tag_of({"client_tag": bad})
    f = tmp_path / "c.yaml"
    f.write_text("route: {client_tag: Bad Tag}\n")
    with pytest.raises(ValueError):
        load_config(str(f))
    from nokker.providers import sokkan

    tag()  # remet « nokker » et lève le verrou
    f.write_text("route: {client_tag: lokkal}\n")
    load_config(str(f))
    assert sokkan._client_tag == "lokkal"


def test_client_tag_is_set_once_a_second_load_config_never_drifts_it(tmp_path, tag, caplog):
    import logging

    from nokker.config import load_config
    from nokker.providers import sokkan

    tag()
    f = tmp_path / "c.yaml"
    f.write_text("route: {client_tag: canary}\n")
    load_config(str(f))
    f.write_text("route: {client_tag: beta}\n")
    with caplog.at_level(logging.WARNING, logger="nokker.providers.sokkan"):
        load_config(str(f))  # autre valeur : WARNING, la valeur d'origine est gardée
    assert sokkan._client_tag == "canary" and any("beta" in r.getMessage() for r in caplog.records)
    caplog.clear()
    f.write_text("route: {client_tag: canary}\n")
    with caplog.at_level(logging.WARNING, logger="nokker.providers.sokkan"):
        load_config(str(f))  # même valeur : rien
    assert sokkan._client_tag == "canary" and not caplog.records
    f.write_text("route: {}\n")  # sans étiquette = « nokker » : différent de canary, donc ignoré aussi
    load_config(str(f))
    assert sokkan._client_tag == "canary"
    f.write_text("route: {client_tag: Bad}\n")  # invalide : erreur, et rien ne bouge
    with pytest.raises(ValueError):
        load_config(str(f))
    assert sokkan._client_tag == "canary"


def test_the_nokker_link_is_a_router(tag):
    link = "https://app.nokker.ai/v1/inference"
    assert is_router_url(link) and is_router_url(link + "/") and is_router_url("https://cloud.example.invalid/v1/inference")
    assert not is_router_url("https://app.nokker.ai/v1") and not is_router_url("https://x.invalid/v1/inference/other")
    seen: list = []
    p = _prov(_spy(False, seen), url=link)
    assert p.behind_router and _ask(p).model_served is None  # le Router réécrit `model` : NULL sans en-tête
    assert seen[-1]["x-nokker-client"] == "nokker"
    served = _ask(_prov(_json(HDR), url=link))
    assert (served.model_served, served.upstream) == ("qwen/qwen3.8-27b", "aws-ship")
    s = _ask(_prov(_sse(headers=HDR), url=link), True)
    assert (s.model_served, s.upstream) == ("qwen/qwen3.8-27b", "aws-ship")
