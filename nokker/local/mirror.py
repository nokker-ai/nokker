"""Mémoire en miroir web ⇄ Lokkal, côté machine (docs/LOKKAL.md §6.3, L5 ; première version, abonnés, drapeau
`local.mirror`). Le pendant de `nokker/cloud/mirror.py`.

- `MirroringStore` enveloppe le `Store` du noyau : chaque tour ajouté sur un fil de la machine part dans la file
  d'envoi ; les copies venues du web (`mirror-web-*`) n'y vont jamais (pas d'aller-retour).
- `MirrorWorker` (un fil de fond) : pousse la file vers `POST /v1/mirror/turns` (jeton de machine), et tire
  `GET /v1/mirror/turns?since=` pour ranger les tours des sessions web sous `mirror-web-<fil>` (lecture seule) ; le
  curseur est gardé dans l'état local (`setup.json`). Rien du dossier de travail ne part : seulement les tours.
Limite de cette version : la file d'envoi est en mémoire ; un tour ajouté juste avant un arrêt brutal n'est poussé
qu'au prochain tour du même fil (le web voit l'écart, rien n'est perdu côté machine).
"""
from __future__ import annotations

import dataclasses
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from nokker.store.pg import turn_from_dict, turn_to_dict

logger = logging.getLogger("nokker.local.mirror")
WEB_PREFIX = "mirror-web-"


@dataclass
class MirroringStore:
    inner: Any
    outbox: deque = field(default_factory=deque)

    def append(self, turn: Any) -> None:
        self.inner.append(turn)
        if not turn.thread_id.startswith(WEB_PREFIX):
            self.outbox.append({"thread_id": turn.thread_id, "turn": turn_to_dict(turn)})

    def __getattr__(self, name: str) -> Any:  # history, next_index, turns_after… : ceux du magasin enveloppé
        return getattr(self.inner, name)


@dataclass
class MirrorWorker:
    store: MirroringStore
    base_url: str  # https://app.nokker.ai
    token: Any  # () -> jeton de machine
    state: dict[str, Any]  # setup.state (curseur « mirror_cursor »)
    save: Any  # () -> None (persiste l'état)
    push_s: float = 5.0
    pull_s: float = 30.0
    client: Any = None
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def start(self) -> None:
        threading.Thread(target=self._run, name="nokker-mirror", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _http(self) -> Any:
        return self.client or httpx.Client(timeout=30.0)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def push_once(self) -> int:
        batch = []
        while self.store.outbox and len(batch) < 200:
            batch.append(self.store.outbox.popleft())
        if not batch:
            return 0
        try:
            r = self._http().post(self.base_url.rstrip("/") + "/v1/mirror/turns", headers=self._headers(),
                                  json={"turns": batch})
            r.raise_for_status()
        except httpx.HTTPError as e:
            logger.info("miroir : envoi reporté (%s)", type(e).__name__)
            self.store.outbox.extendleft(reversed(batch))  # remis en tête, dans l'ordre
            return 0
        return len(batch)

    def pull_once(self) -> int:
        params = {"since": self.state["mirror_cursor"]} if self.state.get("mirror_cursor") else {}
        try:
            r = self._http().get(self.base_url.rstrip("/") + "/v1/mirror/turns", headers=self._headers(),
                                 params=params)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.info("miroir : tirage reporté (%s)", type(e).__name__)
            return 0
        n = 0
        for it in data.get("turns") or []:
            target = WEB_PREFIX + str(it["thread_id"])
            d = dict(it["turn"])
            if self.store.next_index(target) > int(d["index"]):
                continue
            d["thread_id"] = target
            self.store.inner.append(dataclasses.replace(turn_from_dict(d), thread_id=target))
            n += 1
        if data.get("cursor"):
            self.state["mirror_cursor"] = data["cursor"]
            self.save()
        return n

    def _run(self) -> None:
        ticks = 0
        while not self._stop.wait(self.push_s):
            self.push_once()
            ticks += 1
            if ticks * self.push_s >= self.pull_s:
                ticks = 0
                self.pull_once()


__all__ = ["MirrorWorker", "MirroringStore", "WEB_PREFIX"]
