"""Modèle qui SERT le tour, pour le préfixe par modèle (`local.prefix_by_model`, `ContextEngineV0.prefix_by_model`).

Sur Lokkal, le modèle par défaut (réglages, `PUT /v1/local/setup/models`) remplace À CHAUD `kernel.providers[SHIP]` et `[DEEP]` par un même fournisseur
(`api/server.py::default_model_applier`) ; ce n'est pas le BYOK cloud (`kernel.byok`). Le noyau LIT ce modèle UNE FOIS en début de tour (`Kernel.run_turn`) et le
pose dans une ContextVar : un remplacement des fournisseurs pendant le tour ne change pas le préfixe du tour en cours ; le tour suivant prend le nouveau.

Règle : un modèle n'est « servant » que si SHIP et DEEP portent le MÊME id de modèle (un seul modèle pour tout le tour). Sinon (paliers différents, tour BYOK cloud,
fournisseur sans modèle) : None = repli sur `prefix_path`, car un préfixe qui dépendrait du palier changerait en plein tour. Aucun texte vu par le modèle ici."""
from __future__ import annotations

import contextvars
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

_model: contextvars.ContextVar[str | None] = contextvars.ContextVar("nokker_serving_model", default=None)


def active_model() -> str | None:
    return _model.get()


@contextmanager
def turn_model_scope(model: str | None) -> Iterator[str | None]:
    token = _model.set(model)
    try:
        yield model
    finally:
        _model.reset(token)


def serving_model(providers: Mapping[Any, Any], ship: Any, deep: Any) -> str | None:
    """Id du modèle servant le tour si SHIP et DEEP portent le même, sinon None."""
    a, b = getattr(providers.get(ship), "model", None), getattr(providers.get(deep), "model", None)
    return a if isinstance(a, str) and a and a == b else None


__all__ = ["active_model", "serving_model", "turn_model_scope"]
