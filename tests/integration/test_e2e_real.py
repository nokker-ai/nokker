"""Tour réel de bout en bout : `build_kernel()` avec le VRAI Router (Ship), le VRAI MCP `sokkan-memory`,
en Postgres jetable si `NOKKER_TEST_PG_DSN` est posé (sinon `MemoryStore`, toujours un vrai Kernel). Test
de sortie F2 (PLAN) : « un tour réel de bout en bout ». Marqué `integration`, ignoré par CI."""
from __future__ import annotations

import os

import pytest

from nokker.api.server import build_kernel
from nokker.config import ApiKeyIdentity, NokkerConfig, TierConfig
from nokker.core.turn import TurnInput

ROUTER_KEY = os.environ.get("ROUTER_KEY", "")
MEMORY_TOKEN = os.environ.get("SOKKAN_MEMORY_MCP_TOKEN", "")
PG_DSN = os.environ.get("NOKKER_TEST_PG_DSN", "")

pytestmark = pytest.mark.integration
requires_router = pytest.mark.skipif(not ROUTER_KEY, reason="ROUTER_KEY non posé")


@requires_router
def test_real_turn_end_to_end_via_router_and_memory(monkeypatch):
    monkeypatch.setenv("TEST_NOKKER_ROUTER_KEY", ROUTER_KEY)
    if MEMORY_TOKEN:
        monkeypatch.setenv("SOKKAN_MEMORY_MCP_TOKEN", MEMORY_TOKEN)
    if PG_DSN:
        monkeypatch.setenv("TEST_NOKKER_PG_DSN", PG_DSN)

    cfg = NokkerConfig(
        pg_dsn_env="TEST_NOKKER_PG_DSN" if PG_DSN else "TEST_NOKKER_ABSENT_PG_DSN",
        api_keys=[ApiKeyIdentity(token_env="TEST_NOKKER_API_KEY_UNUSED", sender="alice")],
        tiers={
            "ship": TierConfig(base_url="https://router.sokkan.ch/v1", model="qwen/qwen3.8-27b",
                               api_key_env="TEST_NOKKER_ROUTER_KEY"),
            "deep": TierConfig(base_url="https://router.sokkan.ch/v1", model="qwen/qwen3.5-397b-a17b",
                               api_key_env="TEST_NOKKER_ROUTER_KEY"),
        },
        mcp_tool_servers=[],
    )
    kernel = build_kernel(cfg)
    import time
    t0 = time.monotonic()
    turn = kernel.run_turn(TurnInput(thread_id="nokker-f2-e2e-real", user_text="Dis juste : bonjour.",
                                     channel="api", sender="alice"))
    dt = time.monotonic() - t0

    assert turn.error is None, f"tour en échec : {turn.error}"
    assert turn.reply
    assert turn.calls and turn.calls[0].tier.value == "ship"
    usage = turn.calls[0].usage
    print(f"\n[réel E2E] store={'Postgres' if PG_DSN else 'MemoryStore'} "
          f"mémoire={'sokkan-memory réel' if MEMORY_TOKEN else 'MCP non configuré'} "
          f"latence_tour={dt:.2f}s latence_appel={turn.calls[0].latency_s:.2f}s "
          f"modèle={turn.calls[0].model} usage={usage} reply={turn.reply!r}")

    # Le tour doit être relisible depuis le store (Postgres réel si PG_DSN, sinon mémoire).
    history = kernel.store.history("nokker-f2-e2e-real")
    assert history and history[-1].reply == turn.reply
    if hasattr(kernel.store, "close"):
        kernel.store.close()


@requires_router
def test_real_turn_escalates_fiscal_domain_even_through_full_stack(monkeypatch):
    monkeypatch.setenv("TEST_NOKKER_ROUTER_KEY", ROUTER_KEY)
    cfg = NokkerConfig(
        pg_dsn_env="TEST_NOKKER_ABSENT_PG_DSN",
        tiers={
            "ship": TierConfig(base_url="https://router.sokkan.ch/v1", model="qwen/qwen3.8-27b",
                               api_key_env="TEST_NOKKER_ROUTER_KEY"),
            "deep": TierConfig(base_url="https://router.sokkan.ch/v1", model="qwen/qwen3.5-397b-a17b",
                               api_key_env="TEST_NOKKER_ROUTER_KEY"),
        },
        mcp_tool_servers=[],
    )
    kernel = build_kernel(cfg)
    turn = kernel.run_turn(TurnInput(thread_id="nokker-f2-e2e-fiscal", user_text="Quel taux de TVA "
                                     "dois-je appliquer sur ma facture à l'étranger ?",
                                     channel="api", sender="alice"))
    assert turn.escalated is True
    print(f"\n[réel E2E fiscal] escalated={turn.escalated} reply={turn.reply!r}")
