"""Câblage du format anthropic : config du palier (éteinte par défaut), choix du provider, noyau, usage, Lokkal.

Le premier bloc prouve « config absente = comportement inchangé ». Aucun appel vers l'extérieur."""
from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

from nokker.api.server import build_kernel, default_model_applier, lokkal_provider
from nokker.config import TierConfig, load_config
from nokker.core.turn import LLMCall, Message, Tier, TurnInput, Usage
from nokker.ledger.costs import InMemoryLedger, cost_for_call
from nokker.ledger.prices import load_from_yaml
from nokker.local.setup import LocalSetup
from nokker.providers.anthropic import AnthropicProvider
from nokker.providers.select import tier_provider
from nokker.providers.sokkan import SokkanProvider
from nokker.store.pg import _dec_usage, _enc_usage
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

from .. import test_loop_contract as loop
from .anthropic_upstream import MODEL, FakeAnthropic, message

ROOT = Path(__file__).resolve().parents[3]
SHIPPED = ROOT / "deploy" / "lokkal" / "core-config.yaml"
TIERS = {"tiers": {
    "ship": {"base_url": "https://router.example/v1", "model": "qwen/qwen3.8-27b", "api_key_env": "T_SHIP_KEY",
             "cache": "prefix", "sampling": {"temperature": 0.3}, "reasoning_off_body": {"reasoning_effort": "none"},
             "fallback_model": "qwen/spare"},
    "deep": {"base_url": "https://api.example", "model": "claude-test-1", "api_key_env": "T_DEEP_KEY"},
}}


def _config(tmp_path, raw):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_config(str(path))


def _fields(p) -> dict:
    return {f.name: getattr(p, f.name) for f in dataclasses.fields(p)}


# ───────────────────── drapeau éteint : config absente = comportement inchangé ─────────────────────


def test_absent_format_is_openai_and_builds_the_same_sokkan_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("T_SHIP_KEY", "k-ship")
    cfg = _config(tmp_path, TIERS)
    ship = cfg.tiers["ship"]
    assert ship.format == "openai" and TierConfig(base_url="u", model="m").format == "openai"
    extra = {"reasoning_off_body": ship.reasoning_off_body, "sampling_body": dict(ship.sampling)}
    built = tier_provider(ship, vision=ship.vision, vision_probe=None, **extra)
    before = SokkanProvider(base_url=ship.base_url, api_key=ship.api_key(), model=ship.model, vision=ship.vision,
                            vision_probe=None, **extra)  # la construction d'avant cette branche, mot pour mot
    assert type(built) is SokkanProvider and _fields(built) == _fields(before)
    spare = tier_provider(ship, model=ship.fallback_model, **extra)
    assert type(spare) is SokkanProvider and spare.model == "qwen/spare" and spare.api_key == "k-ship"


def test_absent_format_sends_the_same_request_as_before(tmp_path, monkeypatch):
    monkeypatch.setenv("T_SHIP_KEY", "k-ship")
    ship = _config(tmp_path, TIERS).tiers["ship"]
    extra = {"reasoning_off_body": ship.reasoning_off_body, "sampling_body": dict(ship.sampling)}
    seen = []

    def upstream(req: httpx.Request) -> httpx.Response:
        seen.append((str(req.url), {k: v for k, v in req.headers.items() if k != "user-agent"}, req.content))
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "ok"},
                                                                    "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 1}})

    msgs = [{"role": "system", "content": "Tu es Atlas."}, {"role": "user", "content": "Bonjour"}]
    tools = [{"type": "function", "function": {"name": "t", "description": "d", "parameters": {"type": "object"}}}]
    for p in (tier_provider(ship, **extra),
              SokkanProvider(base_url=ship.base_url, api_key=ship.api_key(), model=ship.model, **extra)):
        p._client = httpx.Client(transport=httpx.MockTransport(upstream))
        comp = p.complete(msgs, tools, reasoning=False, session_key="fil-1", max_tokens=64)
        assert comp.usage == Usage(prompt=3, completion=1) and comp.usage.cache_write == 0
    assert seen[0] == seen[1]  # URL, en-têtes et corps identiques à l'octet
    assert seen[0][0] == "https://router.example/v1/chat/completions"


def test_shipped_config_builds_only_sokkan_providers(monkeypatch):
    monkeypatch.delenv("ROUTER_KEY", raising=False)
    cfg = load_config(str(SHIPPED))
    cfg.pg_dsn_env = "TEST_NOKKER_ABSENT_DSN_VAR"
    cfg.mcp_tool_servers = []
    assert all(t.format == "openai" for t in cfg.tiers.values())
    kernel = build_kernel(cfg)
    assert kernel.providers
    for provider in kernel.providers.values():
        inner = getattr(provider, "primary", provider)
        assert type(inner) is SokkanProvider


def test_anthropic_module_is_not_imported_without_the_flag():
    code = ("import sys\nfrom nokker.config import TierConfig\nfrom nokker.providers.select import tier_provider\n"
            "p = tier_provider(TierConfig(base_url='https://r.example/v1', model='m'))\n"
            "assert type(p).__name__ == 'SokkanProvider'\n"
            "assert 'nokker.providers.anthropic' not in sys.modules\n")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr


def test_usage_json_and_cost_are_unchanged_without_cache_writes():
    usage = Usage(prompt=1000, completion=200, cached=400, cached_reported=True)
    assert _enc_usage(usage) == {"prompt": 1000, "completion": 200, "cached": 400, "reasoning": 0,
                                 "cached_reported": True, "reasoning_reported": False}  # aucune clé de plus
    assert _dec_usage(_enc_usage(usage)) == usage


def test_kernel_passes_no_new_keyword_to_other_providers():
    k, _tools, ship, _deep = loop.kernel([Message("assistant", "Bonjour.")])  # `complete` sans `stable_prefix`
    k.context.stable_prefix = lambda: loop.PREFIX
    assert k.run_turn(TurnInput("t1", "salut")).reply == "Bonjour."
    assert len(ship.seen) == 1


# ───────────────────────────── drapeau allumé : `format: anthropic` ─────────────────────────────


def test_format_anthropic_builds_the_anthropic_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("T_DEEP_KEY", "sk-ant-faux")
    raw = json.loads(json.dumps(TIERS))
    raw["tiers"]["deep"].update(format="anthropic", cache="prefix", auth="bearer", anthropic_version="2023-06-01",
                                vision="auto", sampling={"temperature": 0.2},
                                reasoning_off_body={"thinking": {"type": "disabled"}})
    deep = _config(tmp_path, raw).tiers["deep"]
    p = tier_provider(deep, vision=deep.vision, vision_probe=object(), sampling_body=dict(deep.sampling),
                      reasoning_off_body=deep.reasoning_off_body)
    assert type(p) is AnthropicProvider and isinstance(p, SokkanProvider)
    assert (p.base_url, p.api_key, p.model) == ("https://api.example", "sk-ant-faux", "claude-test-1")
    assert (p.cache, p.auth, p.anthropic_version) == ("prefix", "bearer", "2023-06-01")
    assert p.vision_probe is None and p.session_header is False
    assert p.reasoning_off_body == {"thinking": {"type": "disabled"}}
    plain = tier_provider(_config(tmp_path, {"tiers": {"deep": {**TIERS["tiers"]["deep"], "format": "anthropic"}}}
                                  ).tiers["deep"])
    assert (plain.cache, plain.auth, plain.anthropic_version) == ("none", "x-api-key", "2026-10-21")
    assert plain.reasoning_off_body == {} and plain.reasoning_on_body == {"thinking": {"type": "adaptive"}}


@pytest.mark.parametrize("field,value", [("format", "gemini"), ("auth", "basic")])
def test_unknown_format_or_auth_is_a_config_error(tmp_path, field, value):
    raw = json.loads(json.dumps(TIERS))
    raw["tiers"]["deep"][field] = value
    with pytest.raises(ValueError, match=field):
        _config(tmp_path, raw)


def test_build_kernel_serves_an_anthropic_tier_next_to_an_openai_one(tmp_path, monkeypatch):
    monkeypatch.setenv("ROUTER_KEY", "rk-faux")
    monkeypatch.setenv("T_DEEP_KEY", "sk-ant-faux")
    raw = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    raw["tiers"]["deep"] = {"base_url": "https://api.example", "model": "claude-test-1", "format": "anthropic",
                            "api_key_env": "T_DEEP_KEY", "cache": "prefix", "fallback_model": "claude-test-0"}
    cfg = _config(tmp_path, raw)
    cfg.pg_dsn_env = "TEST_NOKKER_ABSENT_DSN_VAR"
    cfg.mcp_tool_servers = []
    kernel = build_kernel(cfg)
    deep = kernel.providers[Tier.DEEP]
    assert type(deep.primary) is AnthropicProvider and type(deep.fallback) is AnthropicProvider  # repli sur lenteur
    assert (deep.primary.api_key, deep.primary.cache, deep.fallback.model) == ("sk-ant-faux", "prefix",
                                                                               "claude-test-0")
    assert type(getattr(kernel.providers[Tier.SHIP], "primary", kernel.providers[Tier.SHIP])) is SokkanProvider
    # Le préfixe stable que le noyau désigne est bien la tête du message système assemblé.
    with tenant_scope(INTERNAL_TENANT):
        prefix = kernel.context.stable_prefix()
        assert prefix and kernel.context.system_parts("")[0] == prefix


def test_a_turn_through_the_kernel_with_tools_cache_and_usage():
    """Tour complet par la vraie boucle : appel d'outil, résultat renvoyé, points de cache, usage au ledger."""
    prefix = loop.PREFIX

    class Ctx(loop.Ctx):
        def assemble(self, turn, history, recall, loaded, tier):
            a = super().assemble(turn, history, recall, loaded, tier)
            a.messages[0]["content"] = prefix + "\n\n[Profil]\nTutoiement."
            a.tools = [loop.Tools.specs["calendar_list"].to_openai()]
            return a

        def stable_prefix(self):
            return prefix

    with FakeAnthropic() as up:
        up.reply(message([{"type": "tool_use", "id": "toolu_01A", "name": "calendar_list", "input": {}}],
                         stop_reason="tool_use",
                         usage={"input_tokens": 20, "cache_creation_input_tokens": 500,
                                "cache_read_input_tokens": 0, "output_tokens": 9}))
        up.reply(message([{"type": "text", "text": "Rien de prévu."}],
                         usage={"input_tokens": 30, "cache_creation_input_tokens": 0,
                                "cache_read_input_tokens": 500, "output_tokens": 4}))
        k, tools, _ship, _deep = loop.kernel([])
        k.context = Ctx()
        k.ledger = InMemoryLedger(load_from_yaml())
        k.providers[Tier.SHIP] = AnthropicProvider(base_url=up.url, api_key="sk-ant-faux", model=MODEL,
                                                   cache="prefix", retry_backoff_s=0.0)
        turn = k.run_turn(TurnInput("t1", "mon agenda ?"))
        first, second = (r["body"] for r in up.requests)
    assert turn.reply == "Rien de prévu." and tools.called == ["calendar_list"]
    for body in (first, second):
        assert body["system"][0] == {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}
        assert body["system"][1]["text"] == "\n\n[Profil]\nTutoiement." and "cache_control" not in body["system"][1]
        assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in json.dumps(body["messages"])
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]
    assert second["messages"][1]["content"] == [{"type": "tool_use", "id": "toolu_01A", "name": "calendar_list",
                                                 "input": {}}]
    assert second["messages"][2]["content"][0]["type"] == "tool_result"
    assert second["messages"][2]["content"][0]["tool_use_id"] == "toolu_01A"
    records = k.ledger.fetch_calls(thread_id="t1")
    assert [(r.prompt_tokens, r.cached_tokens, r.cache_write_tokens, r.cached_reported) for r in records] == [
        (520, 0, 500, True), (530, 500, 0, True)]


def test_cache_writes_reach_usage_store_and_ledger_without_changing_the_cost():
    prices = load_from_yaml()
    model = prices.models()[0]
    usage = Usage(prompt=1240, completion=31, cached=900, cached_reported=True, cache_write=300)
    plain = Usage(prompt=1240, completion=31, cached=900, cached_reported=True)

    def call(u):
        return LLMCall(tier=Tier.DEEP, model=model, reasoning=False, prefix_sha256="x", layer_tokens={}, usage=u,
                       latency_s=1.0, route_reason="défaut")

    assert cost_for_call(call(usage), prices) == cost_for_call(call(plain), prices)  # même formule, mêmes entrées
    assert _enc_usage(usage)["cache_write"] == 300 and _dec_usage(_enc_usage(usage)) == usage
    ledger = InMemoryLedger(prices)
    ledger.record("t1", 0, call(usage))
    ledger.record("t1", 1, call(plain))
    rows = ledger.fetch_calls(thread_id="t1")
    assert [(r.cached_tokens, r.cache_write_tokens, r.new_tokens) for r in rows] == [(900, 300, 340), (900, 0, 340)]


# ───────────────────────────── Lokkal : entrée byok au format anthropic ─────────────────────────────


class _Vault:
    def __init__(self):
        self.data = {}

    def get(self, name):
        return self.data.get(name)

    def put(self, name, secret):
        self.data[name] = secret

    def delete(self, name):
        self.data.pop(name, None)

    def names(self):
        return sorted(self.data)


def _setup(tmp_path, calls):
    return LocalSetup(state_path=tmp_path / "setup.json", vault=_Vault(), detect=lambda: [],
                      test=lambda base, key: calls.append(("openai", base)) or {"ok": True, "models": ["m1"]},
                      test_anthropic=lambda base, key: calls.append(("anthropic", base)) or {"ok": True,
                                                                                             "models": ["claude-a"]})


def test_byok_entry_without_format_is_stored_and_served_as_before(tmp_path):
    calls = []
    setup = _setup(tmp_path, calls)
    view = setup.put_models({"byok": [{"name": "perso", "base_url": "https://ex.test/v1", "model": "m1",
                                       "api_key": "sk-k"}], "default": "byok:perso"})
    assert calls == [("openai", "https://ex.test/v1")]
    assert setup.state["byok"] == [{"name": "perso", "base_url": "https://ex.test/v1", "model": "m1", "ok": True,
                                    "models": ["m1"]}]
    assert "format" not in view["models"]["byok"][0] and setup.default_format() == "openai"
    assert setup.provider_for_default() == ("https://ex.test/v1", "sk-k", "m1")


def test_byok_entry_with_anthropic_format_keeps_its_key_in_the_vault(tmp_path):
    calls = []
    setup = _setup(tmp_path, calls)
    view = setup.put_models({"byok": [{"name": "claude", "base_url": "https://api.example", "model": "claude-a",
                                       "api_key": "sk-ant-faux", "format": "anthropic"}],
                             "default": "byok:claude"})
    assert calls == [("anthropic", "https://api.example")]
    assert view["models"]["byok"][0]["format"] == "anthropic" and view["models"]["byok"][0]["has_key"] is True
    assert "sk-ant-faux" not in json.dumps(view) and "sk-ant-faux" not in (tmp_path / "setup.json").read_text()
    assert setup.default_format() == "anthropic"
    assert setup.provider_for_default() == ("https://api.example", "sk-ant-faux", "claude-a")
    assert setup.test_model({"base_url": "https://api.example", "format": "anthropic"})["models"] == ["claude-a"]
    with pytest.raises(ValueError, match="format"):
        setup.put_models({"byok": [{"name": "x", "base_url": "https://x.test", "format": "gemini"}]})


def test_anthropic_endpoint_probe_lists_models_from_a_local_upstream():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    from nokker.local.setup import test_anthropic_endpoint as probe

    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):  # noqa: N802
            seen.update(path=self.path, key=self.headers.get("x-api-key"), version=self.headers.get("anthropic-version"),
                        bearer=self.headers.get("authorization"))
            ok = self.headers.get("x-api-key") == "sk-ant-faux"
            body = json.dumps({"data": [{"type": "model", "id": "claude-b"}, {"type": "model", "id": "claude-a"}]}
                              if ok else {"type": "error", "error": {"type": "authentication_error",
                                                                     "message": "invalid x-api-key"}}).encode()
            self.send_response(200 if ok else 401)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        good = probe(base, "sk-ant-faux")
        assert good["ok"] is True and good["models"] == ["claude-a", "claude-b"]
        assert seen == {"path": "/v1/models", "key": "sk-ant-faux", "version": "2026-10-21", "bearer": None}
        assert probe(base + "/v1", "mauvaise")["error"] == "HTTP 401" and seen["path"] == "/v1/models"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_lokkal_default_model_in_anthropic_format_is_served_without_the_router(monkeypatch):
    monkeypatch.delenv("ROUTER_KEY", raising=False)
    cfg = load_config(str(SHIPPED))
    cfg.pg_dsn_env = "TEST_NOKKER_ABSENT_DSN_VAR"
    cfg.mcp_tool_servers = []
    kernel = build_kernel(cfg)
    cfg.judge_explicit = False  # juge non choisi en config : il suit d'ordinaire le modèle choisi
    judge_before = (kernel.reviewer.transport.base_url, kernel.reviewer.transport.model)
    fmt = ["anthropic"]
    use = default_model_applier(cfg, kernel, lambda: None, format_fn=lambda: fmt[0])
    use("https://api.example", "sk-ant-faux", "claude-a")
    for tier in (Tier.SHIP, Tier.DEEP):
        p = kernel.providers[tier]
        assert type(p) is AnthropicProvider
        assert (p.base_url, p.api_key, p.model) == ("https://api.example", "sk-ant-faux", "claude-a")
    # Le juge parle /chat/completions : il ne suit pas un modèle au format anthropic.
    assert (kernel.reviewer.transport.base_url, kernel.reviewer.transport.model) == judge_before
    fmt[0] = "openai"  # sans format (ou sans `format_fn`) : le comportement d'avant
    use("https://ex.test/v1", "sk-k", "m1")
    assert type(kernel.providers[Tier.SHIP]) is SokkanProvider and kernel.reviewer.transport.model == "m1"
    default_model_applier(cfg, kernel, lambda: None)("https://ex.test/v1", "sk-k", "m2")
    assert type(kernel.providers[Tier.SHIP]) is SokkanProvider and kernel.providers[Tier.SHIP].model == "m2"


def test_thread_model_catalog_keeps_the_anthropic_format_of_its_byok_entry(tmp_path, monkeypatch):
    """Modèle choisi pour UN fil (`model_catalog`, beta-core) : une entrée byok `format: anthropic` est servie en
    `/v1/messages` ; une entrée sans format, le Router et un modèle inconnu restent au format OpenAI."""
    monkeypatch.delenv("ROUTER_KEY", raising=False)
    setup = _setup(tmp_path, [])
    setup.put_models({"byok": [{"name": "claude", "base_url": "https://api.example", "model": "claude-a",
                                "api_key": "sk-ant-faux", "format": "anthropic"},
                               {"name": "perso", "base_url": "https://ex.test/v1", "model": "m1",
                                "api_key": "sk-k"}]})
    assert setup.model_format("claude-a") == "anthropic"
    assert setup.model_format("m1") == "openai" and setup.model_format("inconnu") == "openai"
    cfg = load_config(str(SHIPPED))
    base, key, _ = setup.model_catalog()["claude-a"]
    p = lokkal_provider(cfg, base, key, "claude-a", fmt=setup.model_format("claude-a"))
    assert type(p) is AnthropicProvider and (p.base_url, p.model) == ("https://api.example", "claude-a")
    base, key, _ = setup.model_catalog()["m1"]
    assert type(lokkal_provider(cfg, base, key, "m1", fmt=setup.model_format("m1"))) is SokkanProvider
    assert type(lokkal_provider(cfg, base, key, "m1")) is SokkanProvider  # sans `fmt` : comme avant
