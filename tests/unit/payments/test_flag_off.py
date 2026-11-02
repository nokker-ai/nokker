"""Drapeau `payments` éteint (défaut) : rien ne change — ni portes, ni outils, ni routes, ni défis d'approbation."""
from __future__ import annotations

import json
from types import SimpleNamespace

from nokker.api.server import configure_payments
from nokker.approvals.memory import InMemoryApprovals
from nokker.config import NokkerConfig
from nokker.core.turn import Decision, Gate, ToolCall, Verdict
from nokker.payments import http as payments_http
from nokker.tenancy.context import tenant_scope
from tests.unit.payments.conftest import ALICE, TENANT, pay_call, to_pay_verdict, turn_of


class Reg:
    def __init__(self):
        self.tester_sources = ["src"]
        self.refreshed = 0

    def refresh(self):
        self.refreshed += 1


def test_flag_is_off_by_default_and_configure_is_a_no_op():
    cfg = NokkerConfig()
    assert cfg.payments is False and cfg.payments_settings is None
    gates, tools = object(), Reg()
    kernel = SimpleNamespace(gates=gates, tools=tools)
    approvals = InMemoryApprovals()
    configure_payments(cfg, kernel, None, approvals, None)
    assert kernel.gates is gates and tools.tester_sources == ["src"] and tools.refreshed == 0
    assert approvals.payment_mandates is None and payments_http.service() is None
    assert not payments_http.is_payments_path("/v1/payments/mandates")


def test_flag_on_wires_gates_tool_and_routes():
    from nokker.payments.gates import PaymentGates

    cfg = NokkerConfig(payments=True, webauthn_origin="https://app.example.ch")
    kernel = SimpleNamespace(gates=object(), tools=Reg())
    approvals = InMemoryApprovals()
    try:
        configure_payments(cfg, kernel, None, approvals, None)
        assert isinstance(kernel.gates, PaymentGates) and approvals.payment_mandates is not None
        assert [type(s).__name__ for s in kernel.tools.tester_sources] == ["str", "PayTools"]
        assert payments_http.is_payments_path("/v1/payments/budgets")
    finally:
        payments_http.configure(None)


def _challenge(backend, call, verdict, account=ALICE):
    with tenant_scope(TENANT[account]):
        card = backend.request(turn_of(account), call, verdict, None)
        return json.loads(backend.challenge(card.id).canonical())


def test_challenges_are_unchanged_without_the_hook_and_for_other_cards(world):
    other = ToolCall(id="c", name="infra_lvextend", arguments={"size": "50G"})
    destructive = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:destructive",
                          strength="strong")
    off = InMemoryApprovals(webauthn_origin="https://app.example.ch")
    assert set(_challenge(off, other, destructive)) == {"approval_id", "args_sha256", "nonce", "exp", "aud"}
    assert set(_challenge(off, pay_call(), to_pay_verdict())) == {"approval_id", "args_sha256", "nonce", "exp", "aud"}
    # drapeau allumé : seule la carte « à payer » gagne `mandate_sha256`
    assert set(_challenge(world.backend, other, destructive)) == {"approval_id", "args_sha256", "nonce", "exp", "aud"}
    assert "mandate_sha256" in _challenge(world.backend, pay_call(), to_pay_verdict())
