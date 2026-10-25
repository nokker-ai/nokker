"""Garde-fou du train du 2.11 (docs/LOKKAL-TRAIN-0211.md) : la config de la BÊTA (deploy/beta/core-config.yaml),
chargée et câblée comme le serveur de la bêta (`build_kernel`, `configure_*`), sans réseau ni Postgres.

But : un lot de `p2/lokkal` qui s'allumerait en bêta par erreur (un défaut qui passe à vrai, une clé ajoutée à la
config, un outil qui arrive chez les testeurs) casse ce test AVANT le déploiement. Si un changement est VOULU, il est
d'abord relu et listé dans docs/LOKKAL-TRAIN-0211.md, puis ce test est mis à jour dans le même commit.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nokker.api import server as srv
from nokker.api.events import EventBus
from nokker.approvals.memory import InMemoryApprovals
from nokker.config import load_config
from nokker.tenancy.context import tenant_scope

ROOT = Path(__file__).resolve().parents[3]
BETA = ROOT / "deploy" / "beta" / "core-config.yaml"
TESTER = "tn_aaaaaaaaaaaa"

# Variables d'environnement qui allumeraient quelque chose indépendamment de la config : vidées pour le test (le pod
# de la bêta ne doit pas les porter, sauf le pool de navigateurs et la clé de la passerelle, testés à part).
_ENV = ("NOKKER_PG_DSN", "NOKKER_PG_OWNER_DSN", "API_SERVER_KEY", "NOKKER_BROWSER_POOL_URL", "NOKKER_BROWSER_POOL_TOKEN",
        "NOKKER_MEMORY_MODE", "NOKKER_CREW_ENGINE", "NOKKER_GATEWAY_SIGNING_KEY", "NOKKER_STRIPE_SECRET_KEY",
        "NOKKER_STRIPE_WEBHOOK_SECRET", "NOKKER_SECRETS_PROVIDER")

# Ce que voit un testeur sans connecteur ni pool de navigateurs : identique à `main` (7d6fd78), mesuré le 25.10.
TESTER_CORE = {"clarify", "escalate_to_human", "recall_context", "tool_find"}
TESTER_TOOLS = {"agent_draft", "bubble_show", "bubble_update", "session_send", "web_fetch", "web_search"}
# Outils de Lokkal et des lots X qui ne doivent JAMAIS apparaître chez un testeur de la bêta.
NEVER_FOR_TESTER = {
    "write_file", "patch", "read_file", "search_files", "terminal", "delegate",  # session de code (L0, L3)
    "process_start", "process_list", "process_logs", "process_stop",  # X6
    "ssh_run", "ssh_copy", "ssh_trust",  # X11
    "pack_list", "pack_install", "mcp_server_add", "mcp_server_list", "mcp_server_remove",  # X16
    "skill_list", "skill_view", "skill_install", "skill_remove",  # L3
    "checkpoint_create", "checkpoint_list", "checkpoint_diff", "checkpoint_restore",  # L3
    "memory_note", "memory_search", "memory_get",  # X1, mémoire longue
    "page_create",  # pages de session
    "plan_card_create", "plan_card_update", "plan_cards", "plan_propose",  # X18 (noms possibles : aucun plan_*)
}


@pytest.fixture
def env(monkeypatch):
    from nokker.connectors.crypto import SecretBox

    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NOKKER_CONNECTOR_KEY", SecretBox.generate_key())
    yield monkeypatch
    # Les modules HTTP gardent leur service dans une variable de module : rien ne doit fuir vers les autres tests.
    from nokker.billing import http as billing_http
    from nokker.cloud import http as cloud_http
    from nokker.incidents import http as incidents_http
    from nokker.plan import http as plan_http
    from nokker.team import http as team_http

    for mod in (cloud_http, team_http, plan_http, incidents_http, billing_http):
        mod.configure(None)


def _config():
    cfg = load_config(str(BETA))
    cfg.prefix_path = str(ROOT / "nokker" / "prompts" / "prefix-nokker.md")  # /etc/nokker/… n'existe pas ici
    cfg.previews_enabled = False  # aucune requête sortante pendant le test
    return cfg


def _kernel(cfg):
    bus = EventBus()
    approvals = InMemoryApprovals(on_event=bus.publish)
    hub = srv.build_hub(cfg, bus)
    kernel = srv.build_kernel(cfg, approvals=approvals, hub=hub)
    return kernel, approvals, hub, bus


def _tester_tools(kernel) -> tuple[set[str], set[str]]:
    with tenant_scope(TESTER):
        return {n for n, _ in kernel.tools.index()}, {s.name for s in kernel.tools.core()}


def test_beta_config_flags_are_as_reviewed_for_the_train(env):
    cfg = _config()
    # Multi-locataire strict, aucune ressource de l'instance interne.
    assert cfg.internal_tenant is False and cfg.api_key_map() == {} and cfg.dev_open_api is False
    assert cfg.require_rls is True
    assert cfg.extra_go_senders == [] and cfg.telegram_ids == {} and cfg.allowlist_domains == []
    assert cfg.memory_mcp_url == "" and cfg.memory_mode == "" and cfg.mcp_tool_servers == []
    assert cfg.admin_enabled is False and cfg.admin_notify_chat_ids == []
    assert cfg.admin_notify_chat_ids_env == "NOKKER_ADMIN_NOTIFY_CHAT_IDS"
    # Pas de Lokkal : ni API locale, ni outils de code, ni images (X7 : magasin vide → 400 sur la route de chat).
    assert cfg.local is None and cfg.images_root() == ""
    assert not any(t.vision for t in cfg.tiers.values())
    assert set(cfg.tiers) == {"ship", "deep"}  # pas de petit palier (X8)
    # Juge (X8) : éteint sous ses trois formes ; routage v0 (pas de `route.policy: v1`).
    assert cfg.judge_enabled is False and cfg.judge_review is False and cfg.judge_advice is False
    assert str((cfg.route or {}).get("policy", "v0")) == "v0"
    # Lots derrière un drapeau, éteints en bêta.
    assert cfg.team is False and cfg.plan is False and cfg.operate is False  # X17, X18
    assert cfg.billing_enabled is False and cfg.billing_live is False  # facturation (29.10)
    assert srv.crew_engine_enabled(cfg) is False  # X14 : aucun ordonnanceur chez nokker.ai
    # Contexte « never clear » : oui ; index sémantique CortHeXis : non.
    assert cfg.context_enabled is True and not (cfg.context_corthexis or {}).get("enabled")
    # Plafonds Solo en mode soft (22.10).
    assert cfg.tenant_limits == {"mode": "soft", "turns_per_day": 40, "turns_per_week": 200,
                                 "hard_turns_per_day": 150, "deep_per_day": 20}
    # Lien Lokkal (train du 2.11) : exactement ces clés. Une clé de plus (mirror, crew, web_agents…) se relit d'abord.
    assert cfg.cloud_link == {"signing_key_env": "NOKKER_GATEWAY_SIGNING_KEY", "base_url": "https://app.nokker.ai",
                              "gateway_domain": "nokker.app", "relayed_connectors": True}


def test_beta_kernel_is_built_like_main_for_testers(env):
    cfg = _config()
    kernel, *_ = _kernel(cfg)
    # Portes : invariants seuls, sans session de code (le drapeau de domaine n'y est jamais « consultatif de session »).
    assert type(kernel.gates).__name__ == "GatesV0" and kernel.gates.code_session is None
    assert type(kernel.policy.inner).__name__ == "PolicyV0"
    assert kernel.reviewer is None and kernel.advisor is None  # X8
    assert kernel.images is None  # X7
    assert kernel.budget_notice_at is None and kernel.stall_limit is None and kernel.stop_check is None
    assert kernel.max_calls == 8 and kernel.max_tokens is None  # défauts de la bêta (pas ceux de Lokkal)
    assert type(kernel.escalator).__name__ == "TenantEscalator"
    assert type(kernel.memory.internal).__name__ == "NullMemory"  # pas de mémoire longue, même pour l'interne
    names, core = _tester_tools(kernel)
    assert core == TESTER_CORE
    assert names == TESTER_TOOLS
    assert not (names | core) & NEVER_FOR_TESTER
    assert not any(n.startswith(("lokkal_", "plan_", "browser_")) for n in names | core)


def test_beta_browser_pool_only_with_its_url(env):
    """Le pool de navigateurs (décision produit, 23.10) : outils `browser_*` pour les testeurs SEULEMENT si l'adresse du
    gestionnaire est posée (Secret du pod) ; aucune connexion à la construction."""
    env.setenv("NOKKER_BROWSER_POOL_URL", "http://127.0.0.1:9")
    kernel, *_ = _kernel(_config())
    names, _ = _tester_tools(kernel)
    browser = {n for n in names if n.startswith("browser_")}
    assert browser and names - browser == TESTER_TOOLS


def test_beta_services_off_without_secrets(env):
    """Sans les secrets du train : aucune route du lien Lokkal, ni équipe, ni plan, ni incidents, ni facturation."""
    from nokker.billing import http as billing_http
    from nokker.cloud import http as cloud_http
    from nokker.incidents import http as incidents_http
    from nokker.plan import http as plan_http
    from nokker.team import http as team_http
    from nokker.tenancy.store import MemoryTenancy

    cfg = _config()
    kernel, approvals, hub, bus = _kernel(cfg)
    tenancy = MemoryTenancy()
    srv.configure_billing(cfg)
    srv.configure_cloud_link(cfg, tenancy, approvals, None, store=kernel.store,
                             connector_store=srv.build_connector_store(cfg), invariants=kernel.invariants)
    srv.configure_team(cfg, tenancy)
    srv.configure_plan_operate(cfg, kernel, agents=None, hub=hub, approvals=approvals, tenancy=tenancy, events=bus)
    assert billing_http.service() is None
    assert cloud_http._cloud is None  # cloud_link posé mais NOKKER_GATEWAY_SIGNING_KEY absente : inerte
    assert team_http.enabled() is False and plan_http.service() is None and incidents_http.service() is None


def test_beta_cloud_link_with_signing_key_turns_on_exactly_this(env):
    """Clé de la passerelle posée (équipe release, au train) : ce que `cloud_link` allume par défaut en bêta — et ce qui reste
    éteint. Relu le 25.10 (docs/LOKKAL-TRAIN-0211.md §2) : handoffs (X21) et connecteurs relayés (X20) allumés,
    boîte aux lettres des approbations fortes (X10) toujours là, miroir (L5) et Crew (X15) éteints."""
    from nokker.cloud import http as cloud_http
    from nokker.cloud.link import Signer
    from nokker.tenancy.store import MemoryTenancy

    env.setenv("NOKKER_GATEWAY_SIGNING_KEY", Signer.generate().private_pem.decode())
    cfg = _config()
    kernel, approvals, hub, bus = _kernel(cfg)
    srv.configure_cloud_link(cfg, MemoryTenancy(), approvals, None, store=kernel.store,
                             connector_store=srv.build_connector_store(cfg), invariants=kernel.invariants)
    cloud = cloud_http._cloud
    assert cloud is not None
    assert cloud.handoffs is not None  # X21 : `cloud_link.handoffs` non posé = allumé
    assert cloud.relay is not None  # X20 : `relayed_connectors: true`
    assert cloud.approvals_relay is not None  # X10 : toujours avec le lien
    assert cloud.mirror_allowed is None  # L5 : pas de `cloud_link.mirror`
    assert cloud.crew is None  # X15 : `cloud_link.crew` absent
    assert cloud.links.base_url == "https://app.nokker.ai"
