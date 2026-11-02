"""Assemblage de la consolidation nocturne (serveur et CLI) : magasin, palier de modèle, signaux, message du matin."""
from __future__ import annotations

import logging
from typing import Any

from nokker.memory.consolidation.engine import Consolidator, Settings

log = logging.getLogger("nokker.memory.consolidation")


def provider_for(config: Any, tier: str) -> tuple[Any, str | None]:
    """Le client du palier `tier` de la config (`tiers.<tier>`), ou (None, None) : passage déterministe seul."""
    t = (getattr(config, "tiers", None) or {}).get(tier)
    if t is None:
        log.warning("consolidation : palier %r absent de la config — doublons exacts seulement", tier)
        return None, None
    if t.api_key_env and not t.api_key():
        log.warning("consolidation : palier %r sans clé (%s) — doublons exacts seulement", tier, t.api_key_env)
        return None, None
    from nokker.providers.sokkan import SokkanProvider

    extra = {"reasoning_off_body": t.reasoning_off_body} if t.reasoning_off_body is not None else {}
    return SokkanProvider(base_url=t.base_url, api_key=t.api_key(), model=t.model, **extra), t.model


def build(config: Any, long_service: Any, *, dsn: str | None, episodes: Any = None, history_from: Any = None,
          scrub: Any = None, notify: Any = None, ledger: Any = None, migrate_now: bool = True) -> Consolidator:
    """`episodes` (EpisodeStore du never clear) et `history_from` (journal des tours) : sans eux, ni résumé d'épisodes
    ni leçons (passage sur la seule mémoire longue)."""
    from nokker.memory.consolidation.stores import MemoryConsolidationStore, PgConsolidationStore

    settings = Settings.from_config(getattr(config, "memory_consolidation_settings", None))
    store: Any = PgConsolidationStore(dsn, migrate_now=migrate_now) if dsn else MemoryConsolidationStore(
        long_service.store)
    complete, model = provider_for(config, settings.tier)
    signals = None
    if dsn and episodes is not None and history_from is not None:
        from nokker.memory.consolidation.signals import KernelSignals, pg_signal_fns

        fns = pg_signal_fns(dsn)
        signals = KernelSignals(threads_of=fns["threads_of"], episodes=episodes, history_from=history_from,
                                recent_turns=fns["recent_turns"], refusals=fns["refusals"])
    else:
        log.info("consolidation : sans Postgres ou sans never clear — ni résumé d'épisodes ni leçons")
    return Consolidator(store=store, settings=settings, complete=complete, model_name=model, signals=signals,
                        embed=getattr(long_service, "_embed_doc", None), scrub=scrub, notify=notify, ledger=ledger)


def notifier(hub: Any, delivery: Any) -> Any:
    """Le mécanisme existant : notification du noyau dans l'app (`BubbleHub.notify_user`) et, si la personne l'a
    réglée pour son brief du matin, la même livraison Telegram / e-mail (`plan/delivery.py::BriefDelivery`)."""
    if hub is None and delivery is None:
        return None

    def notify(account_id: str, title: str, text: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if hub is not None:
            try:
                hub.notify_user(account_id, title=title, text=text)
                out["app"] = "sent"
            except Exception:
                log.warning("consolidation : notification dans l'app en échec", exc_info=True)
                out["app"] = "failed"
        if delivery is not None:
            try:
                out.update(delivery.deliver(account_id, title, text) or {})
            except Exception:
                log.warning("consolidation : livraison hors de l'app en échec", exc_info=True)
                out["delivery"] = "failed"
        return out

    return notify


__all__ = ["build", "notifier", "provider_for"]
