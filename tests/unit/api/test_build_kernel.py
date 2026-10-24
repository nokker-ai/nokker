"""`build_kernel`/`build_invariants`/`build_ledger` — câblage réel (F3 `DeterministicInvariants`, F6
`InMemoryLedger`/`PostgresLedger`), sans réseau (DSN vide -> backends mémoire, aucune connexion)."""
from __future__ import annotations

from nokker.api.server import build_invariants, build_kernel, build_ledger
from nokker.config import NokkerConfig
from nokker.core.ports import ToolCall
from nokker.core.turn import Decision, Effect, InputCheck, Tier, ToolResult, Turn
from nokker.ledger.costs import InMemoryLedger
from nokker.qa.invariants import DeterministicInvariants
from nokker.store.pg import MemoryStore
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope


def test_build_invariants_returns_real_deterministic_invariants():
    inv = build_invariants(NokkerConfig())
    assert isinstance(inv, DeterministicInvariants)


def test_build_invariants_go_senders_include_config_telegram_ids():
    inv = build_invariants(NokkerConfig(telegram_ids={"1000000001": "alice", "1000000002": "bruno"}))
    assert inv.go_senders.authorized("1000000001")
    assert inv.go_senders.authorized("1000000002")
    assert not inv.go_senders.authorized("1234567890")


def test_build_invariants_flags_fiscal_domain():
    inv = build_invariants(NokkerConfig())
    checked = inv.check_input("quel taux de TVA pour ma facture ?")
    assert "fiscal" in checked.domain_flags


def test_build_invariants_redacts_known_secret_from_config_env_prefix(monkeypatch):
    # known_secrets_env_prefix (défaut NOKKER_SECRET_) : toute valeur d'env sous ce préfixe est une
    # valeur CONNUE du coffre, caviardée par redact_result quel que soit l'effet déclaré.
    monkeypatch.setenv("NOKKER_SECRET_GRAFANA", "gf_live_s3cr3t_token_abcdef")
    inv = build_invariants(NokkerConfig())
    from nokker.core.ports import ToolSpec

    spec = ToolSpec(name="grafana_get_credentials", description="d", parameters={}, effect=Effect.SECRET)
    result = ToolResult(call_id="c1", name="grafana_get_credentials",
                        content="token: gf_live_s3cr3t_token_abcdef")
    out = inv.redact_result(result, spec)
    assert "gf_live_s3cr3t_token_abcdef" not in out.content
    assert out.secret_handles


def test_build_ledger_without_dsn_is_in_memory():
    ledger = build_ledger(NokkerConfig(pg_dsn_env="TEST_NOKKER_ABSENT_DSN_VAR"))
    assert isinstance(ledger, InMemoryLedger)


def test_build_kernel_without_dsn_uses_memory_store_and_ledger():
    cfg = NokkerConfig(pg_dsn_env="TEST_NOKKER_ABSENT_DSN_VAR", mcp_tool_servers=[])
    kernel = build_kernel(cfg)
    assert isinstance(kernel.store, MemoryStore)


class _FakeMemory:
    """Même contrat que `CorthexisMemory` (`MemoryPort` + `ToolSource`) — un appelant (ex. le banc
    P1-S1) peut remplacer la mémoire réelle par un stub, sans toucher à `build_kernel` au-delà du
    paramètre `memory` (pattern déjà établi par `invariants`/`ledger`)."""

    def recall(self, query, k=5):
        return []

    def propose(self, thread_id, facts):
        pass

    def specs(self):
        return []

    def call(self, name, arguments, ctx):
        raise AssertionError("ne devrait jamais être appelé dans ce test")


def test_build_kernel_memory_override_is_used_instead_of_corthexis():
    cfg = NokkerConfig(pg_dsn_env="TEST_NOKKER_ABSENT_DSN_VAR", mcp_tool_servers=[])
    fake = _FakeMemory()
    kernel = build_kernel(cfg, memory=fake)
    assert kernel.memory.internal is fake  # TenantMemory : la mémoire passée sert le locataire interne
    assert isinstance(kernel.ledger, InMemoryLedger)
    assert Tier.SHIP in kernel.providers and Tier.DEEP in kernel.providers


def test_build_kernel_core_tools_present_in_registry():
    cfg = NokkerConfig(pg_dsn_env="TEST_NOKKER_ABSENT_DSN_VAR", mcp_tool_servers=[])
    kernel = build_kernel(cfg)
    with tenant_scope(INTERNAL_TENANT):
        names = {s.name for s in kernel.tools.core()}
    # recall_context retiré de la surface (décision Alice, P1-S1, 17.10.2026) tant que A-1 n'existe pas
    assert {"clarify", "tool_find", "escalate_to_human"} <= names
    assert "recall_context" not in names


def test_build_kernel_destructive_action_without_go_is_hitl():
    cfg = NokkerConfig(pg_dsn_env="TEST_NOKKER_ABSENT_DSN_VAR", mcp_tool_servers=[])
    kernel = build_kernel(cfg)
    from nokker.core.ports import ToolSpec

    spec = ToolSpec(name="infra_lvextend", description="d", parameters={}, effect=Effect.DESTRUCTIVE)
    turn = Turn(thread_id="t", index=0, input=InputCheck(text="le disque est plein"), sender=None)
    verdict = kernel.invariants.check_action(ToolCall(id="c", name="infra_lvextend", arguments={}),
                                             spec, turn)
    assert verdict.decision is Decision.HITL


def test_judge_disabled_by_default_uses_invariants_only_gates():
    from nokker.api.server import build_gates
    from nokker.config import NokkerConfig
    from nokker.qa.gates import GatesV0, GatesV1

    class Inv:
        pass

    assert isinstance(build_gates(NokkerConfig(), Inv()), GatesV0)
    assert isinstance(build_gates(NokkerConfig(judge_enabled=True), Inv()), GatesV1)


def test_tier_reasoning_off_body_is_configurable(tmp_path):
    from nokker.config import load_config

    p = tmp_path / "c.yaml"
    p.write_text("tiers:\n  deep:\n    base_url: http://127.0.0.1:18001/v1\n    model: deep\n"
                 "    reasoning_off_body: {chat_template_kwargs: {enable_thinking: false}}\n")
    cfg = load_config(str(p))
    assert cfg.tiers["deep"].reasoning_off_body == {"chat_template_kwargs": {"enable_thinking": False}}


def test_build_kernel_with_hub_registers_bubble_tools_and_observer(monkeypatch):
    from nokker.api.events import EventBus
    from nokker.api.server import build_hub

    monkeypatch.delenv("NOKKER_PG_DSN", raising=False)
    cfg = NokkerConfig(web_search_enabled=False, approvals_enabled=False,
                       telegram_ids={"1000000001": "alice", "1000000002": "bruno"})
    bus = EventBus()
    hub = build_hub(cfg, bus)
    kernel = build_kernel(cfg, hub=hub)
    assert kernel.observer is hub and hub.kernel is kernel and hub.scrub is not None
    for name in ("bubble_update", "session_send"):
        with tenant_scope(INTERNAL_TENANT):
            spec = kernel.tools.spec(name)
        assert spec is not None and spec.effect is Effect.READ
    assert {"alice", "bruno"} <= hub.known_users
