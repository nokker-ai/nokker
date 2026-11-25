"""`escalate: false` par session (docs/HOST-EMBED.md §15) : pas d'escalade automatique vers le palier deep ; un palier imposé à
un sous-agent par `delegate` l'emporte ; mémorisé par fil comme les autres options de session."""
from __future__ import annotations

import logging

import pytest

from nokker.core import session_opts
from nokker.core.loop import Kernel
from nokker.core.ports import RouteState, ToolSpec
from nokker.core.turn import Effect, Message, Tier, ToolCall, ToolResult, Turn, TurnInput
from nokker.local.api import LocalApi
from nokker.route.forced import force_tier
from nokker.route.policy import PolicyV0
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.tenancy.limits import LimitedPolicy, MemoryUsage, TenantLimits
from tests.unit.test_loop_contract import (
    Ctx, Esc, GatesFake, Inv, LedgerFake, Mem, ScriptedProvider, StoreFake,
)


@pytest.fixture(autouse=True)
def _clean():
    reg = session_opts.escalate_registry()
    for t in ("t1", "th_e", "th_f"):
        reg.clear(t)
    with tenant_scope(INTERNAL_TENANT):  # `LimitedPolicy` lit le locataire courant
        yield
    for t in ("t1", "th_e", "th_f"):
        reg.clear(t)


def _limited(inner=None):
    """La politique du serveur : le plafond du compte (`LimitedPolicy`) porte aussi `escalate: false`."""
    return LimitedPolicy(inner=inner or PolicyV0(), limits=TenantLimits(counter=MemoryUsage()))


def _state(thread="t1", failures=3):
    turn = Turn(thread_id=thread, index=0, sender="s", input=TurnInput(thread, "x"))
    return RouteState(turn=turn, call_index=1, tool_failures=failures)


def test_policy_escalates_by_default_and_stays_on_ship_with_escalate_false():
    pol = _limited()
    assert pol.choose(_state()).tier is Tier.DEEP
    session_opts.escalate_registry().set("t1", False)
    got = pol.choose(_state())
    assert got.tier is Tier.SHIP and got.reasoning is False and "escalate:false" in got.reason
    assert pol.choose(_state("t1/sa-abc")).tier is Tier.SHIP  # un sous-agent hérite
    assert pol.choose(_state("t2")).tier is Tier.DEEP  # un autre fil non
    assert pol.choose(_state(failures=0)).tier is Tier.SHIP  # rien à changer sans signal
    assert pol.inner.deep_reasoning is True  # la politique de dessous est inchangée (pas d'enveloppe de plus)


def test_an_explicit_delegate_tier_still_reaches_deep():
    session_opts.escalate_registry().set("t1", False)
    forced = force_tier(_limited(), Tier.DEEP)
    assert forced.choose(_state("t1/sa-abc", failures=0)).tier is Tier.DEEP


class FailTools:
    specs = {"flaky": ToolSpec("flaky", "échoue", {}, Effect.READ)}

    def core(self):
        return []

    def spec(self, n):
        return self.specs.get(n)

    def find(self, q, limit=5):
        return []

    def index(self):
        return []

    def call(self, n, a, ctx):
        return ToolResult(call_id="", name=n, content="erreur", is_error=True)


def _kernel(ship_script, deep_script=(), redo_once=False):
    inv = Inv()
    ship, deep = ScriptedProvider(ship_script), ScriptedProvider(list(deep_script), model="deep")
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=GatesFake(inv, redo_once),
               escalator=Esc(), policy=_limited(), providers={Tier.SHIP: ship, Tier.DEEP: deep},
               tools=FailTools(), ledger=LedgerFake(), max_calls=12)
    return k, ship, deep


def _failing(n):
    return [Message("assistant", tool_calls=[ToolCall(id=f"c{i}", name="flaky", arguments={"i": i})]) for i in range(n)]


def test_three_tool_failures_go_to_deep_by_default():
    k, ship, deep = _kernel([*_failing(3)], [Message("assistant", "repris en deep")])
    turn = k.run_turn(TurnInput("t1", "fais-le"))
    assert turn.reply == "repris en deep" and len(deep.seen) == 1


def test_three_tool_failures_stay_on_ship_with_escalate_false():
    session_opts.escalate_registry().set("t1", False)
    k, ship, deep = _kernel([*_failing(3), Message("assistant", "je conclus en ship")])
    turn = k.run_turn(TurnInput("t1", "fais-le"))
    assert turn.reply == "je conclus en ship" and deep.seen == [] and len(ship.seen) == 4


def test_gate_doubt_redoes_in_deep_by_default_and_delivers_the_ship_answer_with_escalate_false():
    k, ship, deep = _kernel([Message("assistant", "réponse douteuse")], [Message("assistant", "réponse deep")], redo_once=True)
    assert k.run_turn(TurnInput("t1", "q")).reply == "réponse deep"
    session_opts.escalate_registry().set("t1", False)
    k, ship, deep = _kernel([Message("assistant", "réponse douteuse")], [Message("assistant", "réponse deep")], redo_once=True)
    turn = k.run_turn(TurnInput("t1", "q"))
    assert turn.reply == "réponse douteuse" and deep.seen == [] and len(ship.seen) == 1  # pas de relance en boucle


def test_escalate_to_human_is_not_the_tier_escalation():
    """`escalate: false` ne touche pas `escalate_to_human` (outil qui passe la main à une personne)."""
    session_opts.escalate_registry().set("t1", False)
    k, ship, deep = _kernel([Message("assistant", tool_calls=[ToolCall(id="h1", name="escalate_to_human",
                                                                          arguments={"reason": "besoin d'un humain"})])])
    turn = k.run_turn(TurnInput("t1", "q"))
    assert turn.escalated and "Alice" in turn.reply


# ── API : POST /v1/local/sessions ──────────────────────────────────────────────────────────────────────────────────
def _api(tmp_path) -> LocalApi:
    root = tmp_path / "projets"
    (root / "app").mkdir(parents=True, exist_ok=True)
    return LocalApi.from_config({"token_path": str(tmp_path / "tok"), "code": {"roots": [str(root)], "backend": "local"}},
                                port=8742)


def _open(api, tmp_path, **extra):
    return api.handle("POST", "/v1/local/sessions", f"Bearer {api.token}", {"cwd": str(tmp_path / "projets" / "app"), **extra})


@pytest.mark.parametrize("bad", [0, 1, "false", "true", "", [], {}, 0.0])
def test_non_boolean_is_400_bad_escalate_and_opens_nothing(tmp_path, bad):
    api = _api(tmp_path)
    status, body = _open(api, tmp_path, escalate=bad)
    assert status == 400 and body["error"]["code"] == "bad_escalate" and api.code.sessions() == {}


def test_escalate_false_is_remembered_inherited_cleared_and_absent_changes_nothing(tmp_path):
    api = _api(tmp_path)
    reg = session_opts.escalate_registry()
    status, body = _open(api, tmp_path, thread_id="th_e")
    assert status == 201 and "escalate" not in body and session_opts.escalation_allowed("th_e")
    status, body = _open(api, tmp_path, thread_id="th_e", escalate=False)
    assert status == 201 and body["escalate"] is False
    assert not session_opts.escalation_allowed("th_e") and not session_opts.escalation_allowed("th_e/sa-9f")
    assert session_opts.escalation_allowed("th_autre") and reg.get("th_autre") is None
    assert _open(api, tmp_path, thread_id="th_e")[0] == 201 and session_opts.escalation_allowed("th_e")  # reprise sans le champ
    _open(api, tmp_path, thread_id="th_e", escalate=False)
    assert _open(api, tmp_path, thread_id="th_e", escalate=True)[0] == 201 and session_opts.escalation_allowed("th_e")
    _open(api, tmp_path, thread_id="th_e", escalate=None)  # null = absent
    assert session_opts.escalation_allowed("th_e")
    _open(api, tmp_path, thread_id="th_e", escalate=False)
    assert api.handle("DELETE", "/v1/local/sessions/th_e", f"Bearer {api.token}", {})[0] == 200
    assert session_opts.escalation_allowed("th_e")


def test_relayed_call_ignores_escalate_even_when_invalid(tmp_path, caplog):
    api = _api(tmp_path)
    api.relayed_owner = lambda auth: "owner"  # type: ignore[method-assign]
    api.delegation_claims = lambda auth: None  # type: ignore[method-assign]
    caplog.set_level(logging.INFO)
    for value in (False, "n'importe"):
        status, body = api.handle("POST", "/v1/local/sessions", "Bearer x",
                                  {"cwd": str(tmp_path / "projets" / "app"), "thread_id": "th_f", "escalate": value}, relayed=True)
        assert status == 201 and "escalate" not in body and session_opts.escalation_allowed("th_f")
    assert "escalate ignoré" in caplog.text
