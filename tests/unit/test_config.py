"""`nokker/config.py` — résolution des clés API/identité, go_senders, ledger_dsn. Aucun réseau."""
from __future__ import annotations

import os

import pytest

from nokker.config import ApiKeyIdentity, NokkerConfig, load_config


def test_api_key_map_resolves_from_env(monkeypatch):
    monkeypatch.setenv("TEST_NOKKER_KEY_ALICE", "s3cr3t-alice")
    cfg = NokkerConfig(api_keys=[ApiKeyIdentity(token_env="TEST_NOKKER_KEY_ALICE", sender="alice")])
    assert cfg.api_key_map() == {"s3cr3t-alice": "alice"}


def test_api_key_map_skips_unset_env(monkeypatch):
    monkeypatch.delenv("TEST_NOKKER_KEY_UNSET", raising=False)
    cfg = NokkerConfig(api_keys=[ApiKeyIdentity(token_env="TEST_NOKKER_KEY_UNSET", sender="alice")])
    assert cfg.api_key_map() == {}


def test_api_key_map_multiple_keys():
    os.environ["TEST_NOKKER_KEY_A"] = "tok-a"
    os.environ["TEST_NOKKER_KEY_B"] = "tok-b"
    try:
        cfg = NokkerConfig(api_keys=[
            ApiKeyIdentity(token_env="TEST_NOKKER_KEY_A", sender="alice"),
            ApiKeyIdentity(token_env="TEST_NOKKER_KEY_B", sender="bruno"),
        ])
        assert cfg.api_key_map() == {"tok-a": "alice", "tok-b": "bruno"}
    finally:
        del os.environ["TEST_NOKKER_KEY_A"]
        del os.environ["TEST_NOKKER_KEY_B"]


def test_go_senders_includes_deterministic_invariants_defaults():
    cfg = NokkerConfig()
    senders = cfg.go_senders()
    assert "alice" in senders and "bruno" in senders and "alice@example.ch" in senders


def test_go_senders_includes_configured_telegram_ids():
    cfg = NokkerConfig(telegram_ids={"1000000001": "alice", "1000000002": "bruno"})
    senders = cfg.go_senders()
    assert "1000000001" in senders and "1000000002" in senders


@pytest.mark.no_operators
def test_core_defaults_carry_no_operator_identity_or_internal_host():
    """Le noyau publié n'a ni opérateur, ni identifiant, ni hôte interne par défaut : chaque instance pose les siens."""
    cfg = NokkerConfig()
    assert cfg.go_senders() == frozenset() and cfg.telegram_ids == {} and cfg.api_keys == []
    assert cfg.allowlist_domains == [] and cfg.memory_mcp_url == "" and cfg.admin_access_team == ""
    assert cfg.telegram_mini_app_url == "" and cfg.prefix_path.endswith("prompts/prefix-nokker.md")


def test_go_senders_includes_extra_configured():
    cfg = NokkerConfig(extra_go_senders=["special-ops-channel"])
    assert "special-ops-channel" in cfg.go_senders()


def test_ledger_dsn_falls_back_to_pg_dsn(monkeypatch):
    monkeypatch.setenv("TEST_NOKKER_PG_DSN", "postgresql://x/y")
    cfg = NokkerConfig(pg_dsn_env="TEST_NOKKER_PG_DSN", ledger_dsn_env=None)
    assert cfg.ledger_dsn() == "postgresql://x/y"


def test_ledger_dsn_separate_env_when_configured(monkeypatch):
    monkeypatch.setenv("TEST_NOKKER_PG_DSN", "postgresql://x/y")
    monkeypatch.setenv("TEST_NOKKER_LEDGER_DSN", "postgresql://x/ledger")
    cfg = NokkerConfig(pg_dsn_env="TEST_NOKKER_PG_DSN", ledger_dsn_env="TEST_NOKKER_LEDGER_DSN")
    assert cfg.ledger_dsn() == "postgresql://x/ledger"


def test_load_config_missing_file_returns_defaults(tmp_path):
    cfg = load_config(str(tmp_path / "absent.yaml"))
    assert isinstance(cfg, NokkerConfig)
    assert cfg.tiers["ship"].model == "qwen/qwen3.8-27b"


def test_load_config_parses_api_keys(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("""
api_keys:
  - token_env: TEST_NOKKER_KEY_FROM_FILE
    sender: bruno
""")
    cfg = load_config(str(p))
    assert cfg.api_keys == [ApiKeyIdentity(token_env="TEST_NOKKER_KEY_FROM_FILE", sender="bruno")]


def test_load_config_parses_tiers(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("""
tiers:
  ship:
    base_url: http://hote21:8006/v1
    model: qwen3.6-35b-a3b
""")
    cfg = load_config(str(p))
    assert cfg.tiers["ship"].base_url == "http://hote21:8006/v1"
    assert cfg.tiers["ship"].model == "qwen3.6-35b-a3b"


def test_api_keys_are_read_from_config_and_empty_otherwise(tmp_path):
    """`api_keys: []` comme une clé absente : aucune clé (le défaut n'en a plus)."""
    from nokker.config import load_config

    p = tmp_path / "c.yaml"
    p.write_text("api_keys: []\n", encoding="utf-8")
    assert load_config(str(p)).api_keys == []
    p.write_text("port: 8742\n", encoding="utf-8")
    assert load_config(str(p)).api_keys == []
    p.write_text("api_keys: [{token_env: K, sender: owner}]\n", encoding="utf-8")
    assert [k.sender for k in load_config(str(p)).api_keys] == ["owner"]


def test_admin_notify_ids_merge_config_and_env(monkeypatch):
    monkeypatch.setenv("TEST_NOTIFY_IDS", " 1000000002, 1000000003 ,")
    cfg = NokkerConfig(admin_notify_chat_ids=["1000000001", "1000000002"], admin_notify_chat_ids_env="TEST_NOTIFY_IDS")
    assert cfg.admin_notify_ids() == ("1000000001", "1000000002", "1000000003")
    monkeypatch.delenv("TEST_NOTIFY_IDS")
    assert cfg.admin_notify_ids() == ("1000000001", "1000000002")
    assert NokkerConfig().admin_notify_ids() == ()
