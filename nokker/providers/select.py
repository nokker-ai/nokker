"""Choix du provider d'un palier d'après sa config (`tiers.<nom>.format`).

`openai` (défaut, champ absent) : `SokkanProvider`, construit avec exactement les arguments d'avant. `anthropic` :
`AnthropicProvider` (`/v1/messages`, docs/NOKKAN-ANTHROPIC.md), importé seulement dans ce cas.
"""
from __future__ import annotations

from typing import Any

from nokker.config import auth_of, cache_ttl_of, effort_of, format_of
from nokker.providers.sokkan import SokkanProvider


def anthropic_provider(*, base_url: str, api_key: str, model: str, cfg: Any = None, **extra: Any) -> SokkanProvider:
    from nokker.providers.anthropic import DEFAULT_VERSION, AnthropicProvider

    extra.pop("vision_probe", None)  # la sonde de vision parle /chat/completions : `vision: auto` n'est pas servi ici
    return AnthropicProvider(base_url=base_url, api_key=api_key, model=model,
                             cache=str(getattr(cfg, "cache", "none") or "none"),
                             anthropic_version=str(getattr(cfg, "anthropic_version", "") or DEFAULT_VERSION),
                             auth=auth_of(getattr(cfg, "auth", "")),
                             cache_ttl=cache_ttl_of(getattr(cfg, "cache_ttl", "")),
                             effort=effort_of(getattr(cfg, "effort", "")),
                             binding_controls=bool(getattr(cfg, "binding_controls", False)), **extra)


def tier_provider(cfg: Any, *, model: str | None = None, **extra: Any) -> SokkanProvider:
    """Provider d'un palier de la config (`TierConfig`) ; `model` remplace celui du palier (modèle de repli)."""
    kwargs = dict(base_url=cfg.base_url, api_key=cfg.api_key(), model=model or cfg.model,
                  send_call_id=bool(getattr(cfg, "send_call_id", False)))
    # Corps de réflexion « allumée » du palier (`reasoning_on_body`), si la config le pose ; défaut du fournisseur sinon.
    if getattr(cfg, "reasoning_on_body", None) is not None and "reasoning_on_body" not in extra:
        extra["reasoning_on_body"] = dict(cfg.reasoning_on_body)
    # Délais du palier (`timeout_s`, `total_budget_s`) jusqu'au fournisseur ; un appelant qui les passe à la main prime.
    for name in ("timeout_s", "total_budget_s"):
        if name not in extra and getattr(cfg, name, None) is not None:
            extra[name] = float(getattr(cfg, name))
    # Température du palier (`tiers.<nom>.temperature`) jusqu'au corps ; un `sampling_body` passé par l'appelant (déjà fusionné) prime.
    if "sampling_body" not in extra and getattr(cfg, "temperature", None) is not None:
        extra["sampling_body"] = {"temperature": float(cfg.temperature)}
    # Plafond de sortie du palier (`tiers.<nom>.max_tokens`) : sans le champ, 2048 comme avant.
    if "max_tokens_default" not in extra and getattr(cfg, "max_tokens", None):
        extra["max_tokens_default"] = int(cfg.max_tokens)
        extra["max_tokens_pinned"] = True  # la valeur du palier prime sur le défaut de la boucle (`Kernel.max_tokens`)
    if getattr(cfg, "router", None) is not None and "router" not in extra:
        extra["router"] = bool(cfg.router)
    if format_of(getattr(cfg, "format", "")) == "anthropic":
        return anthropic_provider(cfg=cfg, **kwargs, **extra)
    return SokkanProvider(**kwargs, **extra)


__all__ = ["anthropic_provider", "tier_provider"]
