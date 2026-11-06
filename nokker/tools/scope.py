"""Fil du tour en cours, pour le registre d'outils (drapeau `code_tools_session_only`, `tenancy/resources.py`) : les
outils d'une session de code ne sont OFFERTS que dans un fil qui a une session de code vivante. Posé par le noyau
(`Kernel.run_turn`, `Kernel.direct_call`) et par l'exécuteur d'approbations ; hors de ces chemins, aucun fil (périmètre
« ordinaire »)."""
from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

_thread: contextvars.ContextVar[str | None] = contextvars.ContextVar("nokker_tool_thread", default=None)


@contextmanager
def thread_scope(thread_id: str | None) -> Iterator[None]:
    token = _thread.set(thread_id or None)
    try:
        yield
    finally:
        _thread.reset(token)


def current_thread() -> str | None:
    return _thread.get()


__all__ = ["current_thread", "thread_scope"]
