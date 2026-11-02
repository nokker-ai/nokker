"""Outil conseiller `advise` (nokker/route/advise.py) : drapeau, appel au palier conseiller, coût au tour, traces,
plafond par tour, garde contre la récursion, palier de repli. Vrai noyau du banc de conversations, faux modèles."""
from __future__ import annotations

import pytest

from bench.advisor_fork.rig import AdvisorModel
from bench.conv.runner import HERE as CONV, build_rig
from nokker.core.ports import ToolContext
from nokker.core.turn import InputCheck, LLMCall, Tier, Turn, TurnInput, Usage
from nokker.route import advise as av
from nokker.route.advise import ADVISOR_SYSTEM, AdviseConfig, AdviseToolSource, build_advise_source
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.traces.recorder import MemoryTraceSink, TraceRecorder

ARGS = {"question": "Le test échoue sur l'arrondi, je corrige où ?", "context": "def total(): return round(x, 2)"}


def rig(advise=None, **config):
    sc = {"turns": [], "config": dict(config)}
    if advise is not None:
        sc["config"]["advise"] = advise
    return build_rig(sc, CONV / "canary-config.yaml", None, "prefix")


def tool_names(seen):
    return [t["function"]["name"] for t in seen.tools] if hasattr(seen, "tools") else []


def test_flag_off_no_tool_no_source():
    assert build_advise_source({}, {}, lambda t: None) is None
    assert build_advise_source({"enabled": False}, {}, lambda t: None) is None
    with tenant_scope(INTERNAL_TENANT):
        r = rig()
        assert r.kernel.tools.spec("advise") is None
        r.model.load([], "ok")
        r.kernel.run_turn(TurnInput(thread_id="t-off", user_text="bonjour", channel="api", sender="bench"))
        assert "advise" not in r.model.seen[-1].tool_names


def test_advise_calls_deep_without_tools_and_counts_cost():
    with tenant_scope(INTERNAL_TENANT):
        r = rig({"enabled": True})
        adv = AdvisorModel()
        r.kernel.providers[Tier.DEEP] = adv
        r.model.load([{"tool": "advise", "args": ARGS}], "Corrigé.")
        turn = r.kernel.run_turn(TurnInput(thread_id="t-adv", user_text="corrige le total", channel="api",
                                           sender="bench"))
    assert "advise" in r.model.seen[0].tool_names  # outil cœur : visible dès le premier appel
    assert len(adv.seen) == 1
    s = adv.seen[0]
    assert s.tools == [] and s.max_tokens == 600
    assert s.messages[0] == {"role": "system", "content": ADVISOR_SYSTEM}
    assert "[Question de l'agent]" in s.messages[1]["content"] and "corrige le total" in s.messages[1]["content"]
    assert "[Extrait du contexte]\ndef total()" in s.messages[1]["content"]
    result = next(m for m in turn.messages if m.role == "tool" and m.name == "advise")
    assert result.content.startswith("[Conseil]\n") and adv.advice in result.content
    side = [c for c in turn.calls if c.route_reason.startswith("conseiller")]
    assert len(side) == 1 and side[0].tier is Tier.DEEP and side[0].model == adv.model
    assert side[0].route_reason == "conseiller : consulté par l'exécutant (ship → deep)"
    led = [c for c in r.kernel.ledger._calls if c.route_reason.startswith("conseiller")]
    assert len(led) == 1 and led[0].chf is not None  # coût au catalogue (397B) compté au ledger


def test_cap_per_turn():
    with tenant_scope(INTERNAL_TENANT):
        r = rig({"enabled": True, "max_per_turn": 2})
        adv = AdvisorModel()
        r.kernel.providers[Tier.DEEP] = adv
        q = [{"tool": "advise", "args": {**ARGS, "question": f"q{i}"}} for i in range(3)]
        r.model.load(q, "fini")
        turn = r.kernel.run_turn(TurnInput(thread_id="t-cap", user_text="aide", channel="api", sender="bench"))
    assert len(adv.seen) == 2
    results = [m.content for m in turn.messages if m.role == "tool" and m.name == "advise"]
    assert results[2] == av.REFUSED_CAP.format(n=2)
    # tour suivant : le compteur repart
    with tenant_scope(INTERNAL_TENANT):
        r.model.load([{"tool": "advise", "args": ARGS}], "fini")
        r.kernel.run_turn(TurnInput(thread_id="t-cap", user_text="encore", channel="api", sender="bench"))
    assert len(adv.seen) == 3


class FakeKernel:
    def __init__(self, providers):
        self.providers, self.recorded = providers, []
        self.invariants = None

    def record_side_call(self, turn, tier, comp, reason, messages, side="advisor", meta=None):
        self.recorded.append((tier, reason))


def _turn(tier=Tier.SHIP):
    t = Turn(thread_id="t", index=1, input=InputCheck(text="demande"))
    t.calls = [LLMCall(tier=tier, model="m", reasoning=False, prefix_sha256="", layer_tokens={}, usage=Usage(),
                       latency_s=0.0, route_reason="défaut : Ship")]
    return t


def _ctx(kernel, turn):
    return ToolContext(thread_id="t", turn_index=1, channel="api", kernel=kernel, turn=turn)


def test_recursion_guard_and_no_tools():
    src = AdviseToolSource(AdviseConfig(enabled=True))
    adv = AdvisorModel()
    k = FakeKernel({Tier.DEEP: adv})
    token = av._ADVISING.set(True)
    try:
        res = src.call("advise", ARGS, _ctx(k, _turn()))
    finally:
        av._ADVISING.reset(token)
    assert res.is_error and res.content == av.REFUSED_NESTED and adv.seen == []

    class Nested(AdvisorModel):
        def complete(self, messages, tools, **kw):  # le conseiller tente un advise pendant sa consultation
            self.inner = src.call("advise", ARGS, _ctx(k, _turn()))
            return super().complete(messages, tools, **kw)

    nested = Nested()
    k.providers = {Tier.DEEP: nested}
    res = src.call("advise", ARGS, _ctx(k, _turn()))
    assert not res.is_error and nested.inner.content == av.REFUSED_NESTED and len(nested.seen) == 1


def test_same_tier_refused_and_fallback_tier():
    src = AdviseToolSource(AdviseConfig(enabled=True, fallback_tier="nina"), extra={"nina": AdvisorModel()})
    adv = AdvisorModel()
    res = src.call("advise", ARGS, _ctx(FakeKernel({Tier.DEEP: adv}), _turn(Tier.DEEP)))
    assert res.content == av.REFUSED_SAME_TIER and adv.seen == []
    k = FakeKernel({Tier.SHIP: AdvisorModel()})  # pas de Deep servi : repli sur le palier nina
    res = src.call("advise", ARGS, _ctx(k, _turn()))
    assert not res.is_error and len(src.extra["nina"].seen) == 1
    assert k.recorded[0][1] == "conseiller : consulté par l'exécutant (ship → nina)"
    none = AdviseToolSource(AdviseConfig(enabled=True))
    assert none.call("advise", ARGS, _ctx(FakeKernel({Tier.SHIP: adv}), _turn())).content == av.REFUSED_ABSENT


def test_bounded_inputs():
    src = AdviseToolSource(AdviseConfig(enabled=True, question_chars=10, context_chars=20))
    adv = AdvisorModel()
    src.call("advise", {"question": "q" * 50, "context": "c" * 50}, _ctx(FakeKernel({Tier.DEEP: adv}), _turn()))
    user = adv.seen[0].messages[1]["content"]
    assert "q" * 10 + av.TRUNCATED in user and "q" * 11 not in user
    assert "c" * 20 + av.TRUNCATED in user and "c" * 21 not in user


def test_counter_is_per_tenant():
    src = AdviseToolSource(AdviseConfig(enabled=True, max_per_turn=1))
    adv = AdvisorModel()
    k = FakeKernel({Tier.DEEP: adv})
    with tenant_scope("tn_alice"):
        assert not src.call("advise", ARGS, _ctx(k, _turn())).is_error
        assert src.call("advise", ARGS, _ctx(k, _turn())).content == av.REFUSED_CAP.format(n=1)
    with tenant_scope("tn_bob"):  # même fil, même tour, autre locataire : son propre plafond
        assert not src.call("advise", ARGS, _ctx(k, _turn())).is_error


def test_traces_side_row_keeps_loop_rows():
    with tenant_scope(INTERNAL_TENANT):
        r = rig({"enabled": True})
        sink = MemoryTraceSink()
        r.kernel.traces = TraceRecorder(sink=sink, target="canary")
        r.kernel.providers[Tier.DEEP] = AdvisorModel()
        r.model.load([{"tool": "advise", "args": ARGS}], "Corrigé.")
        r.kernel.run_turn(TurnInput(thread_id="t-tr", user_text="corrige", channel="api", sender="bench"))
    loop = [x for x in sink.rows if "side" not in x["meta"]]
    side = [x for x in sink.rows if x["meta"].get("side") == "advisor"]
    assert len(loop) == 2 and len(side) == 1
    assert loop[1]["prompt"] == loop[0]["prompt"] + loop[0]["completion"]  # découpage de la boucle intact
    assert side[0]["call_index"] == 2 and side[0]["tools"] == []
    assert side[0]["prompt"][0]["content"] == ADVISOR_SYSTEM
    assert side[0]["meta"]["route_reason"].startswith("conseiller")
    # Palier configuré ET modèle réel sur la ligne du conseiller (relecture du format, 02.11).
    m = side[0]["meta"]
    assert m["advisor_tier"] == "deep" and m["advisor_model"] == m["model"] and m["advisor_model"]


@pytest.mark.parametrize("raw,ok", [({"enabled": True, "max_per_turn": 0}, 1), ({"enabled": True}, 2)])
def test_config_floor(raw, ok):
    assert AdviseConfig.from_dict(raw).max_per_turn == ok
