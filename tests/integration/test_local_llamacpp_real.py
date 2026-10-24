"""Repli réel sur un llama.cpp (modèle Qwen3.6-35B-A3B) dont l'URL OpenAI est dans `NOKKER_TEST_LLAMACPP_URL`
(ex. `http://hote21:8006/v1`) ; absente ou injoignable : SKIP. Serveur partagé : concurrence 1, quelques appels
seulement (consigne F2). Vérifié en plus du Router Sokkan, pas en remplacement."""
from __future__ import annotations

import os

import httpx
import pytest

from nokker.providers.sokkan import SokkanProvider

HOTE21_URL = os.environ.get("NOKKER_TEST_LLAMACPP_URL", "")
pytestmark = pytest.mark.integration


def _hote21_reachable() -> bool:
    if not HOTE21_URL:
        return False
    try:
        httpx.get(f"{HOTE21_URL}/models", timeout=5)
        return True
    except httpx.HTTPError:
        return False


requires_hote21 = pytest.mark.skipif(not _hote21_reachable(), reason="NOKKER_TEST_LLAMACPP_URL absent ou injoignable")


@requires_hote21
def test_real_call_via_hote21_llamacpp_with_thinking_disabled():
    # Ici, chat_template_kwargs.enable_thinking (PAS reasoning_effort) est la bonne surcharge —
    # confirmé par les bancs F1/F4 (gabarit llama.cpp/vLLM de Qwen3.6-35B-A3B).
    p = SokkanProvider(base_url=HOTE21_URL, api_key="", model="qwen3.6-35b-a3b",
                       reasoning_off_body={"chat_template_kwargs": {"enable_thinking": False}},
                       timeout_s=60)
    try:
        comp = p.complete([{"role": "user", "content": "Réponds par un seul mot : pong."}], [],
                          reasoning=False, session_key="nokker-f2-hote21-test", max_tokens=32)
    finally:
        p.close()
    assert comp.usage.prompt > 0
    print(f"\n[réel] hote21 llama.cpp: model={comp.model} latence={comp.latency_s:.2f}s "
          f"usage={comp.usage} content={comp.message.content!r}")
