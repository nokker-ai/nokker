"""Retours des testeurs de la bêta : `/retour <texte>` sur le bot Telegram, `POST /v1/feedback` pour l'app web.

Le retour est gardé dans le locataire du testeur (table `feedback`, sous RLS) puis signalé aux opérateurs (Telegram,
bot de prod de l'instance interne, comme les candidatures). Le texte est caviardé (`scrub`) avant d'être gardé ou transmis : un testeur
qui colle un mot de passe dans un retour ne le fait pas circuler.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from nokker.tenancy.context import current_tenant

logger = logging.getLogger("nokker.feedback")

MAX_CHARS = 4000


class FeedbackStore(Protocol):
    def add(self, account_id: str, channel: str, text: str) -> int: ...


@dataclass
class MemoryFeedback:
    rows: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, account_id: str, channel: str, text: str) -> int:
        with self._lock:
            self.rows.append({"id": len(self.rows) + 1, "tenant_id": current_tenant(), "account_id": account_id,
                              "channel": channel, "text": text})
            return len(self.rows)


class PgFeedback:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def add(self, account_id: str, channel: str, text: str) -> int:
        from nokker.store.conn import connect

        with connect(self.dsn) as conn:
            return int(conn.execute("INSERT INTO feedback (account_id, channel, text) VALUES (%s,%s,%s) RETURNING id",
                                    (account_id, channel, text)).fetchone()[0])


@dataclass
class Feedback:
    store: FeedbackStore
    scrub: Callable[[str], str] = lambda t: t
    notify: Callable[[str], None] | None = None  # message aux opérateurs

    def submit(self, account: Any, channel: str, text: str) -> int | None:
        """None si le texte est vide. Toujours dans le locataire du compte (appelant en `tenant_scope`)."""
        clean = self.scrub((text or "").strip())[:MAX_CHARS]
        if not clean:
            return None
        fid = self.store.add(account.account_id, channel, clean)
        if self.notify is not None:
            try:
                self.notify(f"Retour bêta ({account.email}, {channel}) :\n{clean[:1500]}")
            except Exception:  # noqa: BLE001 — le retour est gardé, la notification n'est qu'un signal
                logger.warning("notification d'un retour en échec", exc_info=True)
        return fid


_service: Feedback | None = None


def configure(service: Feedback | None) -> None:
    """Une fois au démarrage (`api/server.py::serve`) : partagé par le bot (/retour) et la route POST /v1/feedback."""
    global _service
    _service = service


def service() -> Feedback | None:
    return _service


__all__ = ["Feedback", "FeedbackStore", "MemoryFeedback", "PgFeedback", "configure", "service"]
