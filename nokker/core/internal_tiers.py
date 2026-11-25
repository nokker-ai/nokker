"""Paliers INTERNES (juge de relecture, avis de carte, classeur, Guide, correcteur, juge de la maison…) : la classe de
panne FERMÉE d'un échec, et le guetteur qui la publie comme événement hôte `<module>.failed {reason}`.

Un palier interne qui échoue (quota de compte, délai, amont HS) se taisait : le repli du module (carte, règles, pas de
relecture) restait le bon comportement, mais personne ne le voyait. La `reason` est UNE classe de la liste fermée
ci-dessous (anglais + statut HTTP) : jamais d'adresse, de jeton ni de message d'exception."""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("nokker.internal_tiers")

_STATUS = re.compile(r"\bHTTP[ _]?(\d{3})\b|\bstatus[ _]?(?:code)?[ =:]*(\d{3})\b", re.I)


def failure_reason(exc: BaseException) -> str:
    """Classe de panne fermée : `quota exceeded (HTTP 403)`, `rate limited (HTTP 429)`, `unauthorized (HTTP 401)`,
    `forbidden (HTTP 403)`, `service unavailable (HTTP 5xx)`, `server error (HTTP 500)`, `timeout`, `no small tier`,
    `error`. Seuls le TYPE, le code d'une erreur du noyau et le statut HTTP sont lus ; le texte n'est jamais rendu."""
    if getattr(exc, "code", None) == "no_tier":
        return "no small tier"
    name = type(exc).__name__.casefold()
    if "timeout" in name or isinstance(exc, TimeoutError):
        return "timeout"
    text = str(exc)
    m = _STATUS.search(text)
    status = int(m.group(1) or m.group(2)) if m else int(getattr(exc, "status", 0) or getattr(exc, "status_code", 0) or 0)
    if "quota" in text.casefold() and status in (0, 402, 403, 429):
        return f"quota exceeded (HTTP {status})" if status else "quota exceeded"
    if status == 429:
        return "rate limited (HTTP 429)"
    if status == 401:
        return "unauthorized (HTTP 401)"
    if status == 403:
        return "forbidden (HTTP 403)"
    if status in (502, 503, 504):
        return f"service unavailable (HTTP {status})"
    if status >= 500:
        return f"server error (HTTP {status})"
    return "error"


def watch(transport: Any, module: str, emit: Callable[[str, dict[str, Any]], None] | None) -> None:
    """Enveloppe `transport.post` : un échec est journalisé (WARNING, classe seule) et publié `<module>.failed
    {reason}`, puis RELEVÉ tel quel (le repli du module ne change pas). Idempotent."""
    if emit is None or getattr(transport, "_failure_watch", None) == module:
        return
    inner = transport.post

    def post(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return inner(body)
        except Exception as e:
            reason = failure_reason(e)
            logger.warning("palier interne %s en échec : %s", module, reason)
            try:
                emit(f"{module}.failed", {"reason": reason})
            except Exception:  # noqa: BLE001 — un abonné en panne ne change jamais le repli
                logger.debug("événement %s.failed non publié", module, exc_info=True)
            raise

    transport.post = post
    transport._failure_watch = module


# Événements d'échec d'un palier interne : `<module>.failed` et `home.judge_failed` (publiés par `watch` et le juge de la maison).
FAILURE_EVENTS = frozenset({"home.judge_failed", "judge.failed", "advice.failed", "gate.failed", "auto_review.failed",
                            "outcomes.failed", "classifier.failed", "guide.failed"})


def is_failure_event(event_type: str) -> bool:
    return event_type in FAILURE_EVENTS or event_type.endswith(".failed")


class FailureLimiter:
    """Au plus UN événement d'échec par `interval` secondes et par clé `(module, raison)` ; les répétitions supprimées
    sont comptées et portées par l'événement SUIVANT (`repeats`). Seul l'ÉVÉNEMENT est limité : le WARNING journalisé et le
    repli du module restent à chaque échec. Horloge injectable ; mémoire bornée (`max_keys`, la plus ancienne clé est évincée)."""

    def __init__(self, interval: float = 60.0, monotonic: Callable[[], float] = time.monotonic, max_keys: int = 256) -> None:
        self.interval, self.monotonic, self.max_keys = interval, monotonic, max_keys
        self._seen: OrderedDict[tuple[str, str], list[float]] = OrderedDict()  # clé -> [dernier envoi, supprimés]
        self._lock = threading.Lock()

    def allow(self, module: str, reason: str) -> tuple[bool, int]:
        """(publier ?, répétitions supprimées depuis le dernier envoi). Une clé supprimée ne réinitialise pas la fenêtre."""
        now, key = self.monotonic(), (module, reason)
        with self._lock:
            entry = self._seen.get(key)
            if entry is not None and now - entry[0] < self.interval:
                entry[1] += 1
                return False, 0
            repeats = int(entry[1]) if entry is not None else 0
            self._seen[key] = [now, 0]
            self._seen.move_to_end(key)
            while len(self._seen) > self.max_keys:
                self._seen.popitem(last=False)
            return True, repeats
