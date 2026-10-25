"""Plafonds du palier payé (Billing.caps_for) branchés dans TenantLimits : un abonné a ceux de son plan, un locataire
sans abonnement ceux de la config, et un service de paiement en panne ne bloque jamais (plafonds de la bêta)."""
from __future__ import annotations

from nokker.tenancy.context import tenant_scope
from nokker.tenancy.limits import TenantLimits


def _limits(caps):
    lim = TenantLimits.from_config({"mode": "hard", "turns_per_day": 2, "turns_per_week": 100,
                                    "hard_turns_per_day": 150, "deep_per_day": 1})
    lim.caps_for = caps
    return lim


def test_paid_plan_caps_replace_the_beta_caps():
    lim = _limits(lambda tid: {"turns_per_day": 4} if tid == "tn_paye" else None)
    with tenant_scope("tn_paye"):
        assert [lim.check_turn("us_p") for _ in range(4)] == [None] * 4
        assert lim.check_turn("us_p") is not None and "4 messages" in lim.reply(lim.check_turn("us_p"))
    with tenant_scope("tn_gratuit"):
        assert [lim.check_turn("us_g") is None for _ in range(3)] == [True, True, False]  # 2 par jour (config)


def test_billing_down_never_blocks():
    def boom(tid):
        raise RuntimeError("Stripe injoignable")

    lim = _limits(boom)
    with tenant_scope("tn_x"):
        assert lim.check_turn("us_x") is None and lim.allow_deep("us_x") is True
