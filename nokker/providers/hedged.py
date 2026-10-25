"""Provider « avec repli sur lenteur » : un amont qui n'a rien rendu au bout de `hedge_after_s` est abandonné au profit
d'un second modèle (même interface que `SokkanProvider`). Né de la mesure du 19.10.2026 : le 27B du routeur répond en
0,4 s ou en 60 s sur la même requête triviale (file d'attente amont), les autres modèles en < 0,5 s.

Règles : le primaire tourne dans un thread ; s'il livre un résultat, ou un premier morceau en streaming, AVANT l'échéance,
on s'engage sur lui. Sinon on l'abandonne (ses morceaux tardifs sont jetés, son résultat ignoré) et le repli répond
sur le fil appelant. Abandonné, le primaire ne RÉESSAIE plus (client HTTP propre à l'appel, fermé) : ses réessais
tenaient une place de la clé jusqu'à 180 s, et quelques tours lents saturaient le plafond de 4 requêtes simultanées
du Router (429 puis escalade, vu en réel le 20.10.2026). La requête déjà en vol, elle, va à son terme côté amont (un
`recv` bloqué ne se réveille pas d'un autre thread) et ne rend sa place qu'à la réponse du Router. Pas de seconde chance après un premier morceau : un flux déjà vu par le client n'est jamais rejoué
ici (c'est le rôle de `StreamInterrupted`, loop.py)."""
from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Callable
from typing import Any

from nokker.core.ports import Completion

logger = logging.getLogger(__name__)


class HedgedProvider:
    def __init__(self, primary: Any, fallback: Any, hedge_after_s: float = 8.0) -> None:
        self.primary = primary
        self.fallback = fallback
        self.hedge_after_s = hedge_after_s
        self.hedged_calls = 0  # compteur observable (santé du primaire)

    @property
    def model(self) -> str:
        return self.primary.model

    @property
    def vision(self) -> bool:
        """Vision des DEUX modèles (X7) : le repli peut répondre à la place du primaire, une image ne part que si
        l'un comme l'autre sait la lire."""
        return bool(getattr(self.primary, "vision", False)) and bool(getattr(self.fallback, "vision", False))

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, reasoning: bool,
                 session_key: str, max_tokens: int = 2048,
                 on_delta: Callable[[str], None] | None = None) -> Completion:
        lock = threading.Lock()
        started = threading.Event()  # primaire engagé (résultat ou premier morceau reçu)
        done = threading.Event()
        state: dict[str, Any] = {"abandoned": False}
        detach = getattr(self.primary, "detached", None)
        primary = detach() if callable(detach) else self.primary
        own = primary is not self.primary  # client propre à cet appel : à fermer quoi qu'il arrive

        def relay(piece: str) -> None:
            with lock:
                if state["abandoned"]:
                    return
                state["emitted"] = True
                started.set()
            assert on_delta is not None
            on_delta(piece)

        def run() -> None:
            try:
                kw: dict[str, Any] = {"on_delta": relay} if on_delta is not None else {}
                res = primary.complete(messages, tools, reasoning=reasoning, session_key=session_key,
                                       max_tokens=max_tokens, **kw)
                with lock:
                    if state["abandoned"]:
                        return
                    state["result"] = res
                    started.set()
            except Exception as e:  # noqa: BLE001
                with lock:
                    if state["abandoned"]:
                        return
                    state["error"] = e
                    started.set()
            finally:
                if own:
                    _close_quietly(primary)
                done.set()

        threading.Thread(target=contextvars.copy_context().run, args=(run,), daemon=True, name="hedged-primary").start()
        started.wait(self.hedge_after_s)
        with lock:
            engaged = started.is_set()
            if not engaged:
                state["abandoned"] = True
        if not engaged:
            if own:
                _close_quietly(primary)  # plus aucun réessai ; la requête en vol finit avec l'amont
            self.hedged_calls += 1
            logger.warning("amont %s sans réponse après %.1fs : repli sur %s", self.primary.model,
                           self.hedge_after_s, self.fallback.model)
            kw2: dict[str, Any] = {"on_delta": on_delta} if on_delta is not None else {}
            return self.fallback.complete(messages, tools, reasoning=reasoning, session_key=session_key,
                                          max_tokens=max_tokens, **kw2)
        done.wait()
        if "error" in state:
            err = state["error"]
            if state.get("emitted"):  # le client a déjà vu du texte : jamais de bascule (loop.py gère le rejeu)
                raise err
            logger.warning("amont %s en échec (%s) : repli sur %s", self.primary.model, err, self.fallback.model)
            kw3: dict[str, Any] = {"on_delta": on_delta} if on_delta is not None else {}
            return self.fallback.complete(messages, tools, reasoning=reasoning, session_key=session_key,
                                          max_tokens=max_tokens, **kw3)
        return state["result"]

    def close(self) -> None:
        for p in (self.primary, self.fallback):
            getattr(p, "close", lambda: None)()


def _close_quietly(provider: Any) -> None:
    try:
        provider.close()
    except Exception:  # noqa: BLE001 — fermer un client déjà fermé ou en vol ne doit rien casser
        logger.debug("fermeture du primaire abandonné", exc_info=True)


__all__ = ["HedgedProvider"]
