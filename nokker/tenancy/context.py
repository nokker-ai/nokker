"""Locataire courant (bêta multi-locataire, docs/TENANCY.md).

Le locataire est fixé UNE fois, à la frontière (API, Telegram, CLI), puis lu partout par `current_tenant()` : stores,
registre d'outils, préfixe, connexions Postgres (`app.tenant_id` → RLS). Il voyage dans une ContextVar ; un travail
soumis à un pool de threads doit passer par `submit_in_context` (un ThreadPoolExecutor ne copie pas le contexte).

Pas de valeur par défaut silencieuse : hors d'un `tenant_scope`, `current_tenant()` lève `NoTenant`. Un chemin qui a
oublié de fixer le locataire échoue au lieu de tomber dans celui de Nick (`INTERNAL_TENANT`).
"""
from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterator
from concurrent.futures import Executor, Future
from contextlib import contextmanager
from typing import Any

# Les opérateurs internes, le canari et tout ce qui existait avant le multi-locataire. Les lignes d'avant la migration 006 y sont
# rattachées.
INTERNAL_TENANT = "tn_ninabot"

_tenant: contextvars.ContextVar[str | None] = contextvars.ContextVar("nokker_tenant", default=None)


class NoTenant(RuntimeError):
    """Code exécuté sans locataire fixé : refus plutôt qu'un repli sur le locataire interne."""


def current_tenant() -> str:
    tid = _tenant.get()
    if not tid:
        raise NoTenant("aucun locataire fixé (tenant_scope manquant à la frontière)")
    return tid


def current_tenant_or_none() -> str | None:
    return _tenant.get()


def is_internal() -> bool:
    return _tenant.get() == INTERNAL_TENANT


@contextmanager
def tenant_scope(tenant_id: str) -> Iterator[str]:
    if not tenant_id or not tenant_id.startswith("tn_"):
        raise ValueError(f"locataire invalide : {tenant_id!r}")
    token = _tenant.set(tenant_id)
    try:
        yield tenant_id
    finally:
        _tenant.reset(token)


def submit_in_context(pool: Executor, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
    """`pool.submit` qui emporte le contexte courant (locataire compris) dans le thread du pool."""
    ctx = contextvars.copy_context()
    return pool.submit(ctx.run, fn, *args, **kwargs)


def scoped_thread_id(tenant_id: str, client_thread_id: str) -> str:
    """Identifiant de fil interne. Locataire interne : inchangé (données du canari). Testeur : préfixé par son
    locataire, si bien que deux locataires ne peuvent jamais adresser le même fil, même avec le même X-Session-Id."""
    if tenant_id == INTERNAL_TENANT:
        return client_thread_id
    prefix = f"{tenant_id}:"
    return client_thread_id if client_thread_id.startswith(prefix) else prefix + client_thread_id


def tenant_of_thread(thread_id: str) -> str:
    """Locataire propriétaire d'un fil, d'après son identifiant interne."""
    head, sep, _ = thread_id.partition(":")
    return head if sep and head.startswith("tn_") else INTERNAL_TENANT


__all__ = ["INTERNAL_TENANT", "NoTenant", "current_tenant", "current_tenant_or_none", "is_internal",
           "scoped_thread_id", "submit_in_context", "tenant_of_thread", "tenant_scope"]
