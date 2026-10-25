"""X8 : câblage du multi-modèle et du juge allumé par `build_kernel` (drapeaux, défauts sûrs, repli un seul modèle)."""
from __future__ import annotations

import textwrap

from nokker.api.server import build_kernel
from nokker.config import NokkerConfig, TierConfig, load_config
from nokker.core.turn import Tier
from nokker.qa.judge import HttpJudgeTransport
from nokker.route.policy import PolicyV0, PolicyV1

NO_DSN = "TEST_NOKKER_ABSENT_DSN_VAR"


def test_defaults_are_off():
    k = build_kernel(NokkerConfig(pg_dsn_env=NO_DSN, mcp_tool_servers=[]))
    assert type(k.policy.inner) is PolicyV0
    assert k.reviewer is None and k.advisor is None


def test_v1_three_tiers_and_judge_on(monkeypatch):
    monkeypatch.setenv("ROUTER_KEY", "k")
    tiers = {"small": TierConfig("https://r/v1", "small-m"), "ship": TierConfig("https://r/v1", "ship-m"),
             "deep": TierConfig("https://r/v1", "deep-m")}
    cfg = NokkerConfig(pg_dsn_env=NO_DSN, mcp_tool_servers=[], tiers=tiers, route={"policy": "v1"},
                       judge_review=True, judge_advice=True)
    k = build_kernel(cfg)
    assert isinstance(k.policy.inner, PolicyV1)
    assert set(k.providers) == {Tier.SMALL, Tier.SHIP, Tier.DEEP}
    # Sans `judge:` en config, le juge est le palier Ship (un seul modèle de plus à choisir : aucun).
    assert k.reviewer.transport.model == "ship-m" and k.advisor.transport.model == "ship-m"
    assert k.reviewer.tier is Tier.SHIP
    k.providers.pop(Tier.SMALL)  # retiré à chaud (modèle choisi à l'onboarding) : la politique le voit
    from nokker.core.ports import RouteState
    from nokker.core.turn import InputCheck, Turn

    s = RouteState(turn=Turn(thread_id="t", index=0, input=InputCheck(text="merci !")), call_index=0)
    assert k.policy.choose(s).tier is Tier.SHIP


def test_small_tier_without_key_is_skipped(monkeypatch):
    monkeypatch.delenv("NOKKER_SMALL_KEY", raising=False)
    tiers = {"small": TierConfig("https://r/v1", "small-m", api_key_env="NOKKER_SMALL_KEY"),
             "ship": TierConfig("https://r/v1", "ship-m")}
    k = build_kernel(NokkerConfig(pg_dsn_env=NO_DSN, mcp_tool_servers=[], tiers=tiers, route={"policy": "v1"}))
    assert Tier.SMALL not in k.providers


def test_load_config_judge_block(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent("""
        tiers:
          ship: {base_url: "https://r/v1", model: ship-m}
        judge: {base_url: "https://r/v1", model: judge-m, reasoning_off_body: {reasoning_effort: low}}
        judge_review: true
        route: {policy: v1, classifier: model}
    """), encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.judge_explicit and cfg.judge_config().model == "judge-m"
    assert cfg.judge_config().reasoning_off_body == {"reasoning_effort": "low"}
    p.write_text("tiers:\n  ship: {base_url: 'https://r/v1', model: ship-m}\n", encoding="utf-8")
    assert load_config(str(p)).judge_config().model == "ship-m"


class _Resp:
    def __init__(self, code, data):
        self.status_code, self._data, self.text = code, data, str(data)

    def json(self):
        return self._data


class _Client:
    def __init__(self):
        self.bodies = []

    def post(self, url, json, headers):
        self.bodies.append(json)
        if "reasoning_effort" in json or "top_logprobs" in json:
            return _Resp(400, {"error": "Harmony does not support reasoning_effort='none'"})
        return _Resp(200, {"choices": [{"message": {"content": "Oui"}}], "usage": {"prompt_tokens": 3}})


def test_judge_transport_retries_once_without_extras_and_counts_usage():
    seen = []
    t = HttpJudgeTransport(base_url="https://r/v1", model="gpt-oss", extra_body={"reasoning_effort": "none"},
                           on_usage=lambda m, u, s: seen.append((m, u["prompt_tokens"])))
    t._client = _Client()
    out = t.post({"messages": [], "max_tokens": 1, "logprobs": True})
    assert out["choices"][0]["message"]["content"] == "Oui"
    assert len(t._client.bodies) == 2 and t._plain
    t.post({"messages": [], "max_tokens": 1, "logprobs": True})
    assert len(t._client.bodies) == 3  # gardé : plus de premier essai refusé
    assert seen == [("gpt-oss", 3), ("gpt-oss", 3)]
