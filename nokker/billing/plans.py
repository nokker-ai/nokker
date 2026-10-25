"""Paliers et plafonds. Les PRIX vivent dans Stripe (un `price_id` par palier, posé en configuration) ; ici seulement ce
que le noyau applique : les plafonds, dans les clés de `nokker.tenancy.limits.DEFAULT_LIMITS`.

Valeurs PROVISOIRES tant que la grille n'est pas tranchée :
elles se règlent en configuration (`billing_plans`), pas en code.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    id: str
    caps: dict[str, int]


DEFAULT_PLANS: dict[str, Plan] = {
    "essentiel": Plan("essentiel", {"turns_per_day": 50, "deep_per_day": 5}),
    "solo": Plan("solo", {"turns_per_day": 150, "deep_per_day": 20}),
    # Paliers de foyer (X23, LOKKAL §7.29 ; décision produit du 25.10 : Famille 49, Large 99 CHF/€ par mois, 6 membres au
    # plus, payeur compris, le tour moins cher que Solo). Quota COMMUN du foyer, lu par `household.Settings` ;
    # `household_members` = sièges, payeur compris (ignoré par les plafonds du locataire).
    "famille": Plan("famille", {"turns_per_day": 110, "turns_per_week": 550, "household_members": 6}),
    "large": Plan("large", {"turns_per_day": 230, "turns_per_week": 1150, "household_members": 6}),
}

# Paliers dont l'abonnement porte un foyer (quota partagé entre ses membres).
HOUSEHOLD_PLANS = frozenset({"famille", "large"})

# `past_due` garde les plafonds du palier : Stripe relance le paiement quelques jours avant de résilier.
ACTIVE_STATUSES = frozenset({"active", "trialing", "past_due"})


def plans_from_config(raw: dict[str, dict[str, int]] | None) -> dict[str, Plan]:
    """`{"solo": {"turns_per_day": 200, …}}` remplace les plafonds du palier du même nom ; un nom inconnu en ajoute un."""
    out = dict(DEFAULT_PLANS)
    for pid, caps in (raw or {}).items():
        base = out[pid].caps if pid in out else {}
        out[pid] = Plan(pid, {**base, **{k: int(v) for k, v in caps.items()}})
    return out
