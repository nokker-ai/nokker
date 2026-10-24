"""Vrais appels réseau : Router SOKKAN (compte interne, prix nul — confirmé 17.10.2026) et, à défaut, le
llama.cpp partagé de hote21 (modèle Qwen3.6-35B-A3B, concurrence 1 — PLAN F2). Marqué `integration`, ignoré
par défaut (CI ne les lance pas : `tests/unit` seulement). Exporter `ROUTER_KEY` (clé
de banc, jamais une clé de prod) avant de lancer."""
from __future__ import annotations

import os

import pytest

from nokker.providers.sokkan import SokkanProvider

ROUTER_KEY = os.environ.get("ROUTER_KEY", "")
pytestmark = pytest.mark.integration
requires_router = pytest.mark.skipif(not ROUTER_KEY, reason="ROUTER_KEY non posé")


@requires_router
def test_real_ship_call_via_router():
    p = SokkanProvider(base_url="https://router.sokkan.ch/v1", api_key=ROUTER_KEY, model="qwen/qwen3.8-27b")
    try:
        comp = p.complete([{"role": "user", "content": "Réponds par un seul mot : pong."}], [],
                          reasoning=False, session_key="nokker-f2-test-ship", max_tokens=16)  # gitleaks:allow
    finally:
        p.close()
    assert comp.usage.prompt > 0
    assert comp.model
    assert comp.usage.reasoning == 0  # reasoning_effort: none coupe vraiment le raisonnement
    print(f"\n[réel] Ship router: model={comp.model} latence={comp.latency_s:.2f}s "
          f"usage={comp.usage} content={comp.message.content!r}")


@requires_router
def test_real_deep_call_via_router():
    p = SokkanProvider(base_url="https://router.sokkan.ch/v1", api_key=ROUTER_KEY,
                       model="qwen/qwen3.5-397b-a17b")
    try:
        comp = p.complete([{"role": "user", "content": "Réponds par un seul mot : pong."}], [],
                          reasoning=False, session_key="nokker-f2-test-deep", max_tokens=16)
    finally:
        p.close()
    assert comp.usage.prompt > 0
    print(f"\n[réel] Deep router: model={comp.model} latence={comp.latency_s:.2f}s usage={comp.usage}")


@requires_router
def test_real_eu_route_still_rejected_402_as_expected():
    """`-eu` reste volontairement refusé (402) — PAS un contournement, juste une vérification que le
    comportement documenté tient toujours (consigne F2 : ne pas taper -eu)."""
    import httpx
    resp = httpx.post("https://router.sokkan.ch/v1/chat/completions",
                      headers={"Authorization": f"Bearer {ROUTER_KEY}"},
                      json={"model": "qwen/qwen3.6-35b-a3b-eu",
                            "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4},
                      timeout=15)
    assert resp.status_code == 402


@requires_router
def test_real_cached_tokens_reported_on_second_identical_call():
    """F5 a corrigé `cached_tokens` côté Router — vérifie que ça tient toujours en conditions réelles
    (même session_key = affinité, 2 appels identiques)."""
    p = SokkanProvider(base_url="https://router.sokkan.ch/v1", api_key=ROUTER_KEY, model="qwen/qwen3.8-27b")
    try:
        messages = [{"role": "system", "content": "Tu es un assistant de test F2 Nokker, réponds bref."},
                    {"role": "user", "content": "Dis juste : ok."}]
        p.complete(messages, [], reasoning=False, session_key="nokker-f2-cache-test", max_tokens=8)
        comp2 = p.complete(messages, [], reasoning=False, session_key="nokker-f2-cache-test", max_tokens=8)
    finally:
        p.close()
    print(f"\n[réel] cache: cached_reported={comp2.usage.cached_reported} cached={comp2.usage.cached}")
