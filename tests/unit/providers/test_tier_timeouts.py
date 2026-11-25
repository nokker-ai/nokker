"""`tiers.<nom>.timeout_s` et `tiers.<nom>.total_budget_s` : lus, validés, et branchés jusqu'au fournisseur (client HTTP
et plan de réessais). Sans le champ : 180 s et 80 s, comme avant."""
from __future__ import annotations

import http.server
import threading
import time

import httpx
import pytest
import yaml

from nokker.config import TierConfig, load_config
from nokker.providers import sokkan as sokkan_mod
from nokker.providers.select import tier_provider
from nokker.providers.sokkan import SokkanProvider

OK = {"model": "m", "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
      "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
MSG = [{"role": "user", "content": "x"}]


def _load(tmp_path, tier: dict):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"tiers": {"ship": {"base_url": "https://a.example/v1", "model": "m", **tier}}}),
                    encoding="utf-8")
    return load_config(str(path)).tiers["ship"]


def test_defaults_are_the_historical_values(tmp_path):
    cfg = _load(tmp_path, {})
    assert (cfg.timeout_s, cfg.total_budget_s) == (180.0, 80.0)
    assert (TierConfig(base_url="u", model="m").timeout_s, TierConfig(base_url="u", model="m").total_budget_s) == (180.0, 80.0)
    for fmt in ("openai", "anthropic"):
        p = tier_provider(_load(tmp_path, {"format": fmt}))
        assert (p.timeout_s, p.total_budget_s) == (180.0, 80.0)
        assert p._client.timeout.read == 180.0


@pytest.mark.parametrize("fmt", ["openai", "anthropic"])
def test_values_are_read_and_reach_the_provider(tmp_path, fmt):
    cfg = _load(tmp_path, {"format": fmt, "timeout_s": 45, "total_budget_s": 20.5})
    assert (cfg.timeout_s, cfg.total_budget_s) == (45.0, 20.5)
    p = tier_provider(cfg)
    assert (p.timeout_s, p.total_budget_s) == (45.0, 20.5)
    assert (p._client.timeout.read, p._client.timeout.write) == (45.0, 45.0)
    assert tier_provider(cfg, model="spare").timeout_s == 45.0  # le modèle de repli aussi
    assert tier_provider(cfg, timeout_s=7.0).timeout_s == 7.0  # un appelant explicite prime


@pytest.mark.parametrize("key", ["timeout_s", "total_budget_s"])
@pytest.mark.parametrize("bad", [0, -5, 0.2, 99999, "vite", True, float("nan"), [1]])
def test_absurd_values_are_refused(tmp_path, key, bad):
    with pytest.raises(ValueError, match=key):
        _load(tmp_path, {key: bad})


def test_http_client_really_uses_the_tier_timeout(tmp_path):
    """Faux serveur lent (3 s) : avec `timeout_s: 1` l'appel échoue vers 1 s, pas à 3 s."""
    class Slow(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            time.sleep(3)
            try:
                self.send_response(200)
                self.end_headers()
            except OSError:
                pass

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        cfg = _load(tmp_path, {"timeout_s": 1})
        cfg.base_url = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
        p = tier_provider(cfg)
        t0 = time.monotonic()
        with pytest.raises(RuntimeError, match="timed out"):
            p.complete(MSG, [], reasoning=False, session_key="t")
        assert 0.8 < time.monotonic() - t0 < 2.5
    finally:
        httpd.shutdown()
        httpd.server_close()


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.mark.parametrize("budget,calls", [(30.0, 1), (80.0, 2), (170.0, 3)])
def test_total_budget_bounds_the_retries(monkeypatch, tmp_path, budget, calls):
    """Chaque 504 coûte 60 s (le broker a attendu son budget) : le budget total décide combien d'essais partent."""
    clock = Clock()
    monkeypatch.setattr(sokkan_mod.time, "monotonic", clock)
    monkeypatch.setattr(sokkan_mod.time, "sleep", lambda s: None)
    seen = []

    def handler(request):
        seen.append(request)
        clock.t += 60.0
        return httpx.Response(504, text="gateway timeout")

    p = tier_provider(_load(tmp_path, {"total_budget_s": budget, "timeout_s": 600}), retry_backoff_s=0.0)
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        p.complete(MSG, [], reasoning=False, session_key="t")
    assert len(seen) == calls


def test_hand_built_providers_of_the_server_read_the_tier_fields():
    from nokker.api.server import _tier_runtime_extra

    assert _tier_runtime_extra(None) == {}
    cfg = TierConfig(base_url="u", model="m", timeout_s=30.0, total_budget_s=12.0, reasoning_on_body={"reasoning_effort": "low"})
    p = SokkanProvider(base_url="https://a.example/v1", api_key="k", model="m", **_tier_runtime_extra(cfg))
    assert (p.timeout_s, p.total_budget_s, p.reasoning_on_body) == (30.0, 12.0, {"reasoning_effort": "low"})
    d = _tier_runtime_extra(TierConfig(base_url="u", model="m"))
    assert d == {"timeout_s": 180.0, "total_budget_s": 80.0}
