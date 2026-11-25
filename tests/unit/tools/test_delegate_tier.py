"""`delegate(tier=ship|deep)` (drapeau `delegate_tier`) : drapeau éteint = schéma et description d'avant ; allumé, l'enfant
tourne sur le palier demandé sous le plafond du compte du parent ; `delegate_tenants` ouvre l'outil à un locataire."""
from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from nokker.api.server import build_kernel
from nokker.config import NokkerConfig, TierConfig
from nokker.core.ports import RouteState, ToolContext
from nokker.core.turn import InputCheck, Tier, ToolResult, Turn
from nokker.route.policy import PolicyV0
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.tenancy.limits import LimitedPolicy, TenantLimits
from nokker.tools import delegate as dg
from nokker.tools.delegate import DelegateToolSource
from nokker.tools.registry import Registry

DESC = ("Confie des sous-tâches à des sous-agents qui travaillent en parallèle, chacun dans son worktree git "
        "(branche nokker/sa-<id>) ; rend un résumé par enfant. Bloque jusqu'à leur retour.")
TIER_DESC = ("palier du modèle des sous-agents : ship (code, DevOps, build, opérations ; rapide) ou deep "
             "(architecture, analyse longue, problème difficile ; plus coûteux, réservé aux comptes qui y ont droit) ; "
             "absent : le palier habituel")


@dataclasses.dataclass
class FakeKernel:
    policy: Any
    providers: dict
    limits: Any = None
    decisions: list = dataclasses.field(default_factory=list)

    def cancel(self, thread_id):
        return None

    def run_turn(self, inp, stream=None):
        t = Turn(thread_id=inp.thread_id, index=0, input=InputCheck(text=""), reply="fait")
        for _ in range(2):
            d = self.policy.choose(RouteState(turn=Turn(thread_id=inp.thread_id, index=0, input=InputCheck(text="x")),
                                              call_index=0))
            self.decisions.append(d.tier)
        return t


class Base:
    core = False

    def specs(self):
        return []

    def call(self, name, args, ctx):
        return ToolResult(call_id="", name=name, content="ok")


def setup(flag=True, policy=None, limits=None, providers=None, **kw):
    parent = FakeKernel(policy=policy or PolicyV0(), limits=limits,
                        providers=providers if providers is not None else {Tier.SHIP: 1, Tier.DEEP: 1})
    kids: list[FakeKernel] = []

    def factory(registry, thread_id, observer):
        k = dataclasses.replace(parent, decisions=[])
        kids.append(k)
        return k

    src = DelegateToolSource(factory, None, tier=flag, **kw)
    src.bind(Registry(sources=[Base(), src]))
    ctx = ToolContext(thread_id="t1", turn_index=0, channel="api", sender="alice", kernel=parent)
    return src, parent, kids, ctx


def call(src, ctx, **extra):
    return src.call("delegate", {"tasks": [{"goal": "g"}], "isolate": False, **extra}, ctx)


class AlwaysDeep(PolicyV0):
    def choose(self, state):
        d = super().choose(state)
        return dataclasses.replace(d, tier=Tier.DEEP, reasoning=True)


# ───────────────────────── drapeau éteint : rien ne change ─────────────────────────


def test_flag_off_schema_and_description_unchanged():
    src = DelegateToolSource(lambda *a: None, None)
    assert src.specs()[0].parameters is dg._SPECS_PARAMS and "tier" not in dg._SPECS_PARAMS["properties"]
    assert src.specs()[0].description == DESC
    bg = DelegateToolSource(lambda *a: None, None, background=True)
    assert "tier" not in bg.specs()[0].parameters["properties"]
    assert bg.specs()[0].description.endswith("Bloque jusqu'à leur retour, sauf background=true (ids tout de suite).")


def test_flag_off_ignores_a_tier_argument():
    src, _, kids, ctx = setup(flag=False)
    r = call(src, ctx, tier="deep")
    assert not r.is_error and "tier" not in json.loads(r.content)[0]
    assert set(kids[0].decisions) == {Tier.SHIP}


def test_flag_on_adds_only_the_tier_parameter_and_keeps_the_description():
    src, *_ = setup()
    spec = src.specs()[0]
    # Sans session de code (conversation, `delegate_tenants`) : texte sans git et sans `isolate` (relecture, Run6).
    assert spec.description == dg.CONV_DESCRIPTION
    assert spec.parameters["properties"]["tier"] == {"type": "string", "enum": ["ship", "deep"],
                                                     "description": TIER_DESC}
    assert {k: v for k, v in spec.parameters["properties"].items() if k not in ("tier", "readonly")} \
        == {k: v for k, v in dg._SPECS_PARAMS["properties"].items() if k != "isolate"}
    assert "tier" not in spec.parameters["required"]


# ───────────────────────── palier absent / présent ─────────────────────────


def test_tier_absent_keeps_the_current_policy_and_result():
    src, parent, kids, ctx = setup()
    r = call(src, ctx)
    assert kids[0].policy is parent.policy and "tier" not in json.loads(r.content)[0]


def test_tier_deep_forces_deep_and_ship_forces_ship():
    src, _, kids, ctx = setup()
    r = call(src, ctx, tier="deep")
    assert set(kids[0].decisions) == {Tier.DEEP} and json.loads(r.content)[0]["tier"] == "deep"
    src, _, kids, ctx = setup(policy=AlwaysDeep())
    call(src, ctx, tier="ship")
    assert set(kids[0].decisions) == {Tier.SHIP}


# ───────────────────────── jamais au-delà des droits du parent ─────────────────────────


def test_tenant_without_deep_cannot_force_deep():
    lim = TenantLimits.from_config({"deep_per_day": 0})
    src, _, kids, ctx = setup(policy=LimitedPolicy(inner=PolicyV0(), limits=lim), limits=lim)
    with tenant_scope("tn_a"):
        r = call(src, ctx, tier="deep")
        assert not kids  # aucun enfant lancé
        ok = call(src, ctx, tier="ship")
    assert r.is_error and r.content == dg.TIER_NO_DEEP and len(kids) == 1
    assert not ok.is_error  # ship reste permis


def test_deep_cap_exhausted_today_is_refused_and_internal_tenant_is_not_capped():
    lim = TenantLimits.from_config({"deep_per_day": 1})
    src, _, kids, ctx = setup(policy=LimitedPolicy(inner=PolicyV0(), limits=lim), limits=lim)
    with tenant_scope("tn_a"):
        assert lim.allow_deep("alice")  # consomme l'unique appel du jour
        assert call(src, ctx, tier="deep").content == dg.TIER_NO_DEEP
    with tenant_scope(INTERNAL_TENANT):
        assert not call(src, ctx, tier="deep").is_error


def test_cap_stays_above_the_forced_tier():
    lim = TenantLimits.from_config({"deep_per_day": 1})
    src, _, kids, ctx = setup(policy=LimitedPolicy(inner=PolicyV0(), limits=lim), limits=lim)
    with tenant_scope("tn_a"):
        call(src, ctx, tier="deep")
    assert kids[0].decisions == [Tier.DEEP, Tier.SHIP]  # le 2e appel dépasse le plafond : retombe sur Ship


@pytest.mark.parametrize("value, msg", [("fast", dg.TIER_INVALID), (3, dg.TIER_INVALID)])
def test_invalid_tier_is_refused(value, msg):
    src, _, kids, ctx = setup()
    r = call(src, ctx, tier=value)
    assert r.is_error and r.content == msg and not kids


def test_unserved_tier_is_refused():
    src, _, kids, ctx = setup(providers={Tier.SHIP: 1})
    r = call(src, ctx, tier="deep")
    assert r.is_error and r.content == dg.TIER_NOT_SERVED.format(tier="deep") and not kids
    assert dg.TIER_NOT_SERVED.format(tier="deep") == ("tier=deep refusé : ce palier n'est pas servi ici ; relance sans "
                                                      "tier ou avec l'autre palier")


# ───────────────────────── delegate_tenants ─────────────────────────


def cfg(**kw):
    t = TierConfig("https://r/v1", "m")
    return NokkerConfig(pg_dsn_env="TEST_NOKKER_ABSENT_DSN_VAR", mcp_tool_servers=[], tiers={"ship": t, "deep": t}, **kw)


def has_delegate(k, tenant):
    with tenant_scope(tenant):
        return k.tools.current().spec("delegate") is not None


def test_delegate_tenants_empty_by_default_opens_nothing():
    assert NokkerConfig().delegate_tenants == [] and NokkerConfig().delegate_tier is False
    k = build_kernel(cfg())
    assert not has_delegate(k, "tn_nina") and not has_delegate(k, "tn_other")


def test_delegate_tenants_opens_delegate_to_the_named_tenant_only():
    k = build_kernel(cfg(delegate_tenants=["tn_nina"], delegate_tier=True))
    assert has_delegate(k, "tn_nina") and not has_delegate(k, "tn_other")
    with tenant_scope("tn_nina"):
        assert "tier" in k.tools.current().spec("delegate").parameters["properties"]
    with tenant_scope(INTERNAL_TENANT):
        assert k.tools.current().spec("delegate") is None  # le locataire interne n'est pas nommé
