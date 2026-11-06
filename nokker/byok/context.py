"""Tour servi par le point d'accès d'un client (BYOK cloud, docs/CLOUD-BYOK.md §5) : posé par `Kernel.run_turn` pour
la durée du tour, lu par le noyau (fournisseur de CHAQUE appel, pas de repli de palier), par `LimitedPolicy` (pas de
plafond Deep) et par les plafonds (`tenancy/limits.py`, compteur `byok_turns`). Une ContextVar : un sous-agent lancé
par `submit_in_context` dans le même tour l'emporte avec lui."""
from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ByokRoute:
    """`name` : l'entrée choisie (`byok:<name>`) ; `provider` : fournisseur compatible OpenAI du locataire, déjà construit
    (base_url, modèle et clé de CE locataire), ou None si le tour est refusé (`blocked`)."""

    name: str
    provider: Any
    # Formule sans inférence Nokker et aucun point d'accès utilisable : le tour est refusé avant tout appel de modèle
    # (`byok:not_configured`), `provider` vaut None. `blocked` = le motif (journal, jamais montré tel quel).
    blocked: str | None = None


_active: contextvars.ContextVar[ByokRoute | None] = contextvars.ContextVar("nokker_byok_route", default=None)


def active() -> ByokRoute | None:
    return _active.get()


@contextmanager
def turn_scope(route: ByokRoute | None) -> Iterator[ByokRoute | None]:
    token = _active.set(route)
    try:
        yield route
    finally:
        _active.reset(token)


__all__ = ["ByokRoute", "active", "turn_scope"]
