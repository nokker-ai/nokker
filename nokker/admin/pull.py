"""Tirage des candidatures depuis `nokker-beta-api` (front1) — chemin a′ de espace-admin §5.

le front ne joint pas le noyau ; le noyau joint le front par le réseau privé. Le noyau tire donc chaque minute
`GET /internal/applications?after=<plus grand id déjà importé>` (Bearer), insère dans `beta_applications` (doublon de
source ignoré), accuse réception (`POST /internal/applications/ack`) puis notifie les opérateurs sur Telegram. Le
premier tirage de l'histoire de la base (import initial) envoie UN message récapitulatif, pas un par candidature.
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from nokker.admin.store import AdminStore, normalize_application

logger = logging.getLogger("nokker.admin.pull")

ACTOR = "system:beta-pull"
SOURCE = "front1"


def http_json(method: str, url: str, token: str, body: dict[str, Any] | None = None,
              timeout_s: float = 10.0) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib_request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib_request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 — URL fixée par la config
        return json.loads(resp.read() or b"{}")


@dataclass
class TelegramText:
    """`sendMessage` texte simple aux opérateurs. N'appelle JAMAIS getUpdates ni setWebhook : le bot (prod de l'instance interne) est
    lu par un autre processus. Une notification ratée est journalisée, jamais propagée : la candidature est en base."""

    bot_token: str
    chat_ids: tuple[str, ...]
    timeout_s: float = 10.0

    def send(self, text: str) -> None:
        for chat_id in self.chat_ids:
            body = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode()
            req = urllib_request.Request(f"https://api.telegram.org/bot{self.bot_token}/sendMessage", data=body,
                                         method="POST", headers={"Content-Type": "application/json"})
            try:
                with urllib_request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                    resp.read()
            except Exception:  # noqa: BLE001
                logger.warning("notification Telegram en échec (chat %s)", chat_id, exc_info=True)


def application_message(app: dict[str, Any], console_url: str) -> str:
    """Prénom, profil, pays, première tâche, lien vers la fiche (espace-admin §3.A). Jamais l'e-mail."""
    first = " ".join((app.get("taches") or "").split())
    if len(first) > 220:
        first = first[:219] + "…"
    who = app.get("prenom") or "?"
    meta = ", ".join(x for x in (app.get("profil"), app.get("pays"), (app.get("lang") or "").upper()) if x)
    dup = " — même adresse qu'une candidature précédente" if app.get("duplicate") else ""
    return (f"Nouvelle candidature bêta : {who} ({meta}){dup}\n"
            f"Tâche : {first}\n{console_url.rstrip('/')}/admin/#/a/{app['id']}")


@dataclass
class BetaPuller:
    store: AdminStore
    base_url: str  # ex. http://<front>:8122
    token: str
    send: Callable[[str], None] | None = None
    console_url: str = "https://admin.nokker.ai"
    interval_s: float = 60.0
    batch: int = 200
    fetch: Callable[..., dict[str, Any]] = http_json
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)

    def pull_once(self) -> int:
        """Un tirage complet (plusieurs pages si besoin). Nombre de candidatures insérées."""
        # Import initial = jamais rien importé (journal), pas « table vide » : une table vidée par un effacement (test,
        # demande nLPD) ne doit pas transformer la candidature suivante en simple récapitulatif.
        bootstrap = not self.store.has_imported()
        inserted: list[dict[str, Any]] = []
        while True:
            after = self.store.max_source_id(SOURCE)
            url = f"{self.base_url.rstrip('/')}/internal/applications?" + urllib_parse.urlencode(
                {"after": after, "limit": self.batch})
            raw = self.fetch("GET", url, self.token).get("applications") or []
            if not raw:
                break
            new = self.store.import_applications(SOURCE, [normalize_application(r) for r in raw], ACTOR)
            inserted.extend(new)
            try:
                self.fetch("POST", f"{self.base_url.rstrip('/')}/internal/applications/ack", self.token,
                           {"ids": [int(r["id"]) for r in raw]})
            except Exception:  # noqa: BLE001 — l'accusé est informatif ; le tirage repart de la base, pas de lui
                logger.warning("accusé de réception à nokker-beta-api en échec", exc_info=True)
            if len(raw) < self.batch:
                break
        if inserted and self.send is not None:
            if bootstrap:
                self.send(f"Console opérateur : import initial de {len(inserted)} candidature(s) bêta.\n"
                          f"{self.console_url.rstrip('/')}/admin/")
            else:
                for app in inserted:
                    self.send(application_message(app, self.console_url))
        if inserted:
            logger.info("candidatures importées : %d", len(inserted))
        return len(inserted)

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.pull_once()
            except Exception:  # noqa: BLE001 — front1 injoignable : on réessaie au prochain tour
                logger.warning("tirage des candidatures en échec", exc_info=True)
            self._stop.wait(self.interval_s)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="nokker-beta-pull", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()


__all__ = ["BetaPuller", "TelegramText", "application_message", "http_json"]
