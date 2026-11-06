"""Le juge ne retombe jamais sur Ship en silence : WARNING une fois, ou refus avec `judge_require_explicit`."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from nokker import config as config_mod
from nokker.api.server import build_kernel
from nokker.config import NokkerConfig, TierConfig, load_config

NO_DSN = "TEST_NOKKER_ABSENT_DSN_VAR"
TIERS = {"ship": TierConfig("https://r/v1", "ship-m"), "deep": TierConfig("https://r/v1", "deep-m")}
JUDGE = TierConfig("https://r/v1", "qwen/qwen3.5-397b-a17b")
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _fresh_warned(monkeypatch):
    monkeypatch.setattr(config_mod, "_JUDGE_FALLBACK_WARNED", set())


def cfg(**kw):
    return NokkerConfig(pg_dsn_env=NO_DSN, mcp_tool_servers=[], tiers=dict(TIERS), **kw)


def test_fallback_on_ship_warns_once_and_names_the_model(caplog, monkeypatch):
    monkeypatch.setenv("ROUTER_KEY", "k")
    with caplog.at_level(logging.WARNING, logger="nokker.config"):
        build_kernel(cfg(judge_review=True, auto_review={"enabled": True}))
        build_kernel(cfg(judge_review=True))
    msgs = [r.getMessage() for r in caplog.records if r.name == "nokker.config"]
    assert len(msgs) == 1
    assert "retombe sur le palier Ship" in msgs[0] and "ship-m" in msgs[0]
    assert "judge_review" in msgs[0] and "auto_review" in msgs[0]


def test_no_warning_when_nothing_uses_the_judge_or_when_explicit(caplog, monkeypatch):
    monkeypatch.setenv("ROUTER_KEY", "k")
    with caplog.at_level(logging.WARNING, logger="nokker.config"):
        build_kernel(cfg())
        build_kernel(cfg(auto_review={"enabled": True, "tier": "deep"}))
        build_kernel(cfg(judge_review=True, judge=JUDGE, judge_explicit=True))
    assert not [r for r in caplog.records if r.name == "nokker.config"]


@pytest.mark.parametrize("kw, who", [
    ({"judge_review": True}, "judge_review"),
    ({"judge_advice": True}, "judge_advice"),
    ({"judge_enabled": True}, "judge_enabled"),
    ({"auto_review": {"enabled": True}}, "auto_review"),
    ({"outcomes_enabled": True}, "outcomes_grader"),
    ({"goals_enabled": True, "outcomes_grader": "judge"}, "outcomes_grader"),
])
def test_require_explicit_without_judge_refuses_to_start(kw, who):
    with pytest.raises(ValueError, match=f"judge_require_explicit.*{who}.*judge:"):
        build_kernel(cfg(judge_require_explicit=True, **kw))


def test_require_explicit_with_judge_starts(monkeypatch):
    monkeypatch.setenv("ROUTER_KEY", "k")
    k = build_kernel(cfg(judge_require_explicit=True, judge_review=True, auto_review={"enabled": True},
                         judge=JUDGE, judge_explicit=True))
    assert k.reviewer.transport.model == "qwen/qwen3.5-397b-a17b"


def test_require_explicit_ignores_graders_on_a_named_tier_and_off_features():
    build_kernel(cfg(judge_require_explicit=True))
    build_kernel(cfg(judge_require_explicit=True, auto_review={"enabled": True, "tier": "ship"}))


@pytest.mark.parametrize("path", [
    pytest.param("deploy/beta/core-config.yaml", marks=pytest.mark.skipif(
        not (ROOT / "deploy/beta/core-config.yaml").exists(), reason="deploy/beta absent (arbre public)")),
    "deploy/lokkal/core-config.yaml"])
def test_delivered_configs_require_an_explicit_judge(path):
    c = load_config(ROOT / path)
    assert c.judge_require_explicit is True and c.judge_explicit is True
    assert c.judge_config().model in ("qwen/qwen3.5-397b-a17b", "deepseek/deepseek-v4.1-flash")
    assert c.judge_config().base_url == c.tiers["ship"].base_url
    c.check_judge_explicit()
