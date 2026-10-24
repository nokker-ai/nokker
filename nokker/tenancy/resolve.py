"""Expéditeur authentifié → locataire (docs/TENANCY.md §2). Fonctions pures, appelées par les frontières (API,
Telegram, CLI) AVANT tout travail ; jamais de repli sur le locataire interne.

- `us_…` (compte d'un testeur : session web ou appareil, id Telegram appairé) → le locataire de son compte actif ;
- expéditeur interne (clé d'API de config, `telegram_ids`, appareils des opérateurs internes) → `INTERNAL_TENANT` ;
- tout le reste → `UnknownSender` (403 à la frontière).
"""
from __future__ import annotations

from collections.abc import Callable, Container

from nokker.tenancy.context import INTERNAL_TENANT
from nokker.tenancy.store import Tenancy

ACCOUNT_PREFIX = "us_"


class UnknownSender(PermissionError):
    """Expéditeur sans locataire : compte inconnu ou suspendu, identité hors config, ou absente."""


def is_account(sender: str | None) -> bool:
    return bool(sender) and str(sender).startswith(ACCOUNT_PREFIX)


def resolve_tenant(sender: str | None, tenancy: Tenancy, internal_senders: Container[str]) -> str:
    """Locataire de `sender`, ou `UnknownSender`. Un `us_…` passe TOUJOURS par `Tenancy`, même s'il figurait par
    erreur dans `internal_senders` : un compte de testeur ne devient jamais interne."""
    if not sender:
        raise UnknownSender("expéditeur absent")
    if is_account(sender):
        acc = tenancy.account(sender)
        if acc is None or acc.status != "active" or not acc.tenant_id.startswith("tn_"):
            raise UnknownSender(f"compte inconnu ou inactif : {sender}")
        return acc.tenant_id
    if sender in internal_senders:
        return INTERNAL_TENANT
    raise UnknownSender(f"expéditeur sans locataire : {sender}")


def account_tenant_fn(tenancy: Tenancy) -> Callable[[str | None], str | None]:
    """`account_tenant(sender) -> tn_… | None` : locataire d'un compte `us_…` actif, `None` pour tout le reste.
    Injecté dans les invariants (GO) et les approbations (décideur) : ils ne savent rien de la base."""
    def account_tenant(sender: str | None) -> str | None:
        if not is_account(sender):
            return None
        acc = tenancy.account(str(sender))
        return acc.tenant_id if acc is not None and acc.status == "active" else None

    return account_tenant


__all__ = ["ACCOUNT_PREFIX", "UnknownSender", "account_tenant_fn", "is_account", "resolve_tenant"]
