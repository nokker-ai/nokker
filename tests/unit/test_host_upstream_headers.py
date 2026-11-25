"""`upstream_headers` d'une session (docs/HOST-EMBED.md §7) : validés, gardés en mémoire pour le fil, envoyés au seul palier
`gate: true`, jamais journalisés ; 503 sans palier `gate: true` ; absents sans reprise."""
from __future__ import annotations

import logging

import httpx
import pytest

from nokker.config import TierConfig, load_config
from nokker.providers import gate
from nokker.providers.select import tier_provider
from tests.unit.test_local_api import _api

GOOD = {"gate_session": "sess-7f3a9c", "gate_org": "org_42", "gate_class": "interactive"}
SECRETS = ("sess-7f3a9c", "org_42", "interactive")


@pytest.fixture(autouse=True)
def _clean():
    gate.registry()._by_thread.clear()
    yield
    gate.registry()._by_thread.clear()


def _open(api, tmp_path, **extra):
    return api.handle("POST", "/v1/local/sessions", f"Bearer {api.token}",
                      {"cwd": str(tmp_path / "projets" / "app"), **extra})


def _upstream(seen):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append({k.lower(): v for k, v in req.headers.items()})
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "ok"},
                                                                    "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    return handler


def _call(provider, seen, key):
    provider._client = httpx.Client(transport=httpx.MockTransport(_upstream(seen)))
    provider.complete([{"role": "user", "content": "salut"}], [], reasoning=False, session_key=key, max_tokens=8)
    return seen[-1]


@pytest.mark.parametrize("bad", [
    {"gate_session": 12}, {"gate_session": ""}, {"gate_session": "a b"}, {"gate_session": "x" * 257},
    {"gate_session": "ok\r\nX-Evil: 1"}, {"inconnu": "v"}, ["gate_session"], "gate_session"])
def test_invalid_values_are_refused_400_without_echo(tmp_path, bad):
    api = _api(tmp_path)
    api.has_gate_tier = True
    status, body = _open(api, tmp_path, upstream_headers=bad)
    assert status == 400 and body["error"]["code"] == "bad_upstream_headers"
    assert "Evil" not in str(body) and api.code.sessions() == {}


def test_no_gate_tier_is_503_and_opens_nothing(tmp_path):
    api = _api(tmp_path)
    status, body = _open(api, tmp_path, upstream_headers=GOOD)
    assert status == 503 and body["error"]["code"] == "no_gate_tier"
    assert api.code.sessions() == {} and gate.registry()._by_thread == {}
    # vides : rien n'est demandé, la session s'ouvre comme avant
    assert _open(api, tmp_path, upstream_headers={})[0] == 201
    assert _open(api, tmp_path, upstream_headers=None)[0] == 201


def test_session_keeps_headers_in_memory_and_never_echoes_them(tmp_path):
    api = _api(tmp_path)
    api.has_gate_tier = True
    status, body = _open(api, tmp_path, upstream_headers=GOOD)
    assert status == 201 and not any(v in str(body) for v in SECRETS)
    tid = body["thread_id"]
    assert gate.registry().get(tid)["X-Gate-Org"] == "org_42"
    assert gate.registry().get(tid + "/sa-abc12")["X-Gate-Class"] == "interactive"  # sous-agent du fil
    assert gate.registry().get("autre_fil") == {}
    assert not any(v in repr(gate.registry()) for v in SECRETS)
    # une ouverture sans le champ remplace (efface) ; la fermeture aussi
    assert _open(api, tmp_path, thread_id=tid)[0] == 201 and gate.registry().get(tid) == {}
    _open(api, tmp_path, thread_id=tid, upstream_headers=GOOD)
    assert api.handle("DELETE", f"/v1/local/sessions/{tid}", f"Bearer {api.token}", {})[0] == 200
    assert gate.registry().get(tid) == {}


def test_headers_go_to_the_gate_tier_only(tmp_path):
    gate.registry().set("th_a", {"X-Gate-Session": "sess-7f3a9c", "X-Gate-Org": "org_42", "X-Gate-Class": "interactive"})
    on = tier_provider(TierConfig(base_url="https://gate.example/v1", model="m", gate=True), gate=True)
    off = tier_provider(TierConfig(base_url="https://other.example/v1", model="m"))
    seen: list[dict] = []
    sent = _call(on, seen, "th_a")
    assert (sent["x-gate-session"], sent["x-gate-org"], sent["x-gate-class"]) == ("sess-7f3a9c", "org_42", "interactive")
    assert _call(on, seen, "th_a/sa-1f2e")["x-gate-org"] == "org_42"
    for other in ("th_a", "th_b"):  # autre palier : jamais ; autre fil : jamais
        assert not [k for k in _call(off, seen, other) if k.startswith("x-gate")]
    assert not [k for k in _call(on, seen, "th_b") if k.startswith("x-gate")]
    assert not [k for k in _call(on, seen, "th_a#advise") if k.startswith("x-gate")]


def test_anthropic_format_tier_sends_them_too_when_gated():
    gate.registry().set("th_a", {"X-Gate-Org": "org_42"})
    cfg = TierConfig(base_url="https://gate.example", model="m", format="anthropic", gate=True)
    p = tier_provider(cfg, gate=True)
    seen: list[dict] = []

    def handler(req):
        seen.append({k.lower(): v for k, v in req.headers.items()})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                                         "model": "m", "usage": {"input_tokens": 1, "output_tokens": 1}})

    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    p.complete([{"role": "user", "content": "salut"}], [], reasoning=False, session_key="th_a", max_tokens=8)
    assert seen[0]["x-gate-org"] == "org_42"


def test_values_never_reach_the_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    api = _api(tmp_path)
    api.has_gate_tier = True
    tid = _open(api, tmp_path, upstream_headers=GOOD)[1]["thread_id"]
    _open(api, tmp_path, upstream_headers={"gate_session": "x y"})  # refusé : la valeur ne s'écrit pas non plus
    on = tier_provider(TierConfig(base_url="https://gate.example/v1", model="m", gate=True), gate=True)
    on.max_retries, on.retry_backoff_s = 1, 0.0

    def failing(req):  # l'échec d'amont est journalisé par le provider : sans les en-têtes
        return httpx.Response(502, text="bad gateway")

    on._client = httpx.Client(transport=httpx.MockTransport(failing))
    with pytest.raises(Exception):  # noqa: B017 — l'amont échoue, c'est le but
        on.complete([{"role": "user", "content": "x"}], [], reasoning=False, session_key=tid, max_tokens=8)
    assert caplog.records, "le test doit avoir exercé des journaux"
    text = "\n".join(r.getMessage() for r in caplog.records) + caplog.text
    assert not any(v in text for v in SECRETS) and "x y" not in text


def test_config_gate_flag(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("tiers:\n  ship: {base_url: 'https://a/v1', model: m, gate: true}\n  deep: {base_url: 'https://b/v1', model: m}\n")
    cfg = load_config(str(p))
    assert cfg.tiers["ship"].gate is True and cfg.tiers["deep"].gate is False
    p.write_text("tiers:\n  ship: {base_url: 'https://a/v1', model: m, gate: 'false'}\n")
    with pytest.raises(ValueError, match="gate"):
        load_config(str(p))
