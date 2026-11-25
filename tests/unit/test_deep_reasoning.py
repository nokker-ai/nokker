"""Montée vers deep sans réflexion forcée : `route.deep_reasoning: false` (config) et `deep_reasoning: false` (session) ; le défaut reste la
réflexion demandée par la politique (comportement validé d'avant). Le palier REDO_DEEP de la boucle suit la même règle."""
from __future__ import annotations

import pytest

from nokker.core import session_opts
from nokker.core.ports import Completion, RouteDecision
from nokker.core.turn import Message, Tier, TurnInput, Usage
from nokker.local.api import LocalApi
from tests.unit.test_note_claims import guard_world


class Spy:
    def __init__(self):
        self.reasoning: list[bool] = []

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.reasoning.append(reasoning)
        return Completion(message=Message(role="assistant", content="ok"), usage=Usage(prompt=5), model="t", latency_s=0.0)


class DeepPolicy:
    """Choisit toujours deep en demandant la réflexion, comme `PolicyV0` sur un signal d'échec."""

    def __init__(self, deep_reasoning=True, ship_reasoning=False):
        self.deep_reasoning, self.ship_reasoning = deep_reasoning, ship_reasoning

    def choose(self, state):
        return RouteDecision(tier=Tier.DEEP, reasoning=self.deep_reasoning, reason="signal")


@pytest.fixture(autouse=True)
def _clean():
    for t in ("th_d", "th_d/sa-1", "th_e"):
        session_opts.deep_reasoning_registry().clear(t)
    yield
    for t in ("th_d", "th_d/sa-1", "th_e"):
        session_opts.deep_reasoning_registry().clear(t)


def run(policy, thread="th_d", wrap=None):
    k = guard_world("ok")
    spy = Spy()
    k.providers = {Tier.SHIP: Spy(), Tier.DEEP: spy}
    k.policy = policy if wrap is None else wrap(policy)
    k.run_turn(TurnInput(thread_id=thread, user_text="x"))
    return spy.reasoning


def test_default_is_unchanged_deep_asks_for_reasoning():
    assert run(DeepPolicy()) == [True]


def test_session_option_does_not_force_reasoning_on_deep_it_follows_ship():
    session_opts.deep_reasoning_registry().set("th_d", False)
    assert run(DeepPolicy()) == [False]
    assert run(DeepPolicy(ship_reasoning=True)) == [True]  # suit Ship, ne coupe pas non plus
    assert run(DeepPolicy(), thread="th_e") == [True]  # un autre fil : inchangé
    session_opts.deep_reasoning_registry().set("th_d", False)
    assert run(DeepPolicy(), thread="th_d/sa-1") == [False]  # un sous-agent hérite


def test_session_option_reaches_through_the_policy_wrappers():
    from nokker.route.forced import force_tier
    from nokker.tenancy.limits import LimitedPolicy

    session_opts.deep_reasoning_registry().set("th_d", False)
    assert run(DeepPolicy(), wrap=lambda p: LimitedPolicy(inner=p, limits=type("L", (), {"allow_deep": lambda s, x: True, "deep_per_day": 0})())) == [False]
    assert run(DeepPolicy(), wrap=lambda p: force_tier(p, Tier.DEEP)) == [False]


def test_redo_deep_follows_the_same_rule():
    k = guard_world("ok")
    k.policy = DeepPolicy(deep_reasoning=True)
    assert k._deep_decision("th_d", "doute") == RouteDecision(tier=Tier.DEEP, reasoning=True, reason="doute")
    session_opts.deep_reasoning_registry().set("th_d", False)
    assert k._deep_decision("th_d", "doute").reasoning is False
    k.policy = DeepPolicy(deep_reasoning=False)  # config `route.deep_reasoning: false`
    session_opts.deep_reasoning_registry().clear("th_d")
    assert k._deep_decision("th_d", "doute").reasoning is False  # le « doute » n'impose plus True en dur


def test_config_route_deep_reasoning_builds_the_policy():
    from nokker.api.server import build_policy
    from nokker.config import NokkerConfig

    assert build_policy(NokkerConfig(route={}), {}).deep_reasoning is True
    assert build_policy(NokkerConfig(route={"deep_reasoning": False}), {}).deep_reasoning is False
    assert build_policy(NokkerConfig(route={"policy": "v1", "deep_reasoning": False}), {}).deep_reasoning is False
    with pytest.raises(ValueError, match="deep_reasoning"):
        build_policy(NokkerConfig(route={"deep_reasoning": "false"}), {})


# ───────────────────────── option de session ─────────────────────────

def _api(tmp_path):
    root = tmp_path / "projets"
    (root / "app").mkdir(parents=True, exist_ok=True)
    return LocalApi.from_config({"token_path": str(tmp_path / "tok"), "code": {"roots": [str(root)], "backend": "local"}}, port=8742)


def _open(api, tmp_path, **extra):
    return api.handle("POST", "/v1/local/sessions", f"Bearer {api.token}", {"cwd": str(tmp_path / "projets" / "app"), **extra})


@pytest.mark.parametrize("bad", [0, 1, "false", "", [], {}, 0.0])
def test_non_boolean_is_400_and_opens_nothing(tmp_path, bad):
    api = _api(tmp_path)
    status, body = _open(api, tmp_path, deep_reasoning=bad)
    assert status == 400 and body["error"]["code"] == "bad_deep_reasoning" and api.code.sessions() == {}


def test_session_option_is_remembered_echoed_cleared(tmp_path):
    api = _api(tmp_path)
    status, body = _open(api, tmp_path, thread_id="th_d", deep_reasoning=False)
    assert status == 201 and body["deep_reasoning"] is False and not session_opts.deep_reasoning_forced("th_d")
    assert not session_opts.deep_reasoning_forced("th_d/sa-1") and session_opts.deep_reasoning_forced("th_e")
    status, body = _open(api, tmp_path, thread_id="th_d", deep_reasoning=True)  # true = défaut
    assert status == 201 and "deep_reasoning" not in body and session_opts.deep_reasoning_forced("th_d")
    _open(api, tmp_path, thread_id="th_d", deep_reasoning=False)
    assert _open(api, tmp_path, thread_id="th_d")[0] == 201 and session_opts.deep_reasoning_forced("th_d")  # réouverture sans le champ
    _open(api, tmp_path, thread_id="th_d", deep_reasoning=False)
    assert api.handle("DELETE", "/v1/local/sessions/th_d", f"Bearer {api.token}", {})[0] == 200
    assert session_opts.deep_reasoning_forced("th_d")


def test_relayed_call_ignores_it(tmp_path):
    api = _api(tmp_path)
    api.relayed_owner = lambda auth: "owner"  # type: ignore[method-assign]
    api.delegation_claims = lambda auth: None  # type: ignore[method-assign]
    status, body = api.handle("POST", "/v1/local/sessions", "Bearer x", {"cwd": str(tmp_path / "projets" / "app"),
                                                                        "thread_id": "th_e", "deep_reasoning": False}, relayed=True)
    assert status == 201 and "deep_reasoning" not in body and session_opts.deep_reasoning_forced("th_e")
