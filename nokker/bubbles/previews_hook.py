"""Branche le service d'aperçus (`nokker/previews/`, docs/BUBBLES.md §5) sur le producteur `search` :
après la création d'une bulle `search`, les aperçus de TOUS les résultats (15 au plus) sont demandés en tâche de
fond, puis la bulle est mise à jour (`bubble.updated`) avec `results[i].preview`. Un aperçu en échec
(`{"url", "error"}`) est simplement omis ; la bulle porte `previews_done: true` une fois le lot fini
(réussi ou non) pour que l'interface arrête les squelettes de chargement. Un lot en panne ne touche pas le tour."""
from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nokker.bubbles.types import Bubble

if TYPE_CHECKING:
    from nokker.bubbles.hub import BubbleHub

logger = logging.getLogger("nokker.bubbles.previews")

PreviewMany = Callable[[list[str], int], list[dict[str, Any]]]


def attach_previews(bubble: Bubble, previews: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Nouveau `state` avec les aperçus posés sur les résultats de même URL ; `None` si rien à poser."""
    by_url = {p.get("url"): p for p in previews if p and not p.get("error")}
    if not by_url:
        return None
    state = dict(bubble.state)
    state["results"] = [dict(r, preview=by_url[r.get("url")]) if r.get("url") in by_url else r
                        for r in state.get("results") or []]
    return state


def make_preview_requester(hub: BubbleHub, preview_many: PreviewMany, *, limit: int = 15,
                           background: bool = True) -> Callable[[Bubble], None]:
    def run(bubble: Bubble) -> None:
        try:
            urls = [r.get("url") for r in (bubble.state.get("results") or [])[:limit] if r.get("url")]
            if not urls:
                return
            state = attach_previews(bubble, preview_many(urls, limit)) or dict(bubble.state)
        except Exception:
            logger.exception("aperçus de la bulle %s en échec", bubble.id)
            state = dict(bubble.state)
        state["previews_done"] = True
        try:
            hub.update_from_kernel(bubble.id, state=state)
        except Exception:
            logger.exception("mise à jour de la bulle %s en échec", bubble.id)

    def request(bubble: Bubble) -> None:
        if background:
            # Le locataire du tour suit la mise à jour de la bulle (docs/TENANCY.md §2).
            ctx = contextvars.copy_context()
            threading.Thread(target=ctx.run, args=(run, bubble), name=f"nokker-previews-{bubble.id}",
                             daemon=True).start()
        else:
            run(bubble)

    return request


__all__ = ["attach_previews", "make_preview_requester"]
