"""Mémoire en miroir web ⇄ Lokkal, côté machine (docs/LOKKAL.md §6.3, L5 ; abonnés, drapeau `local.mirror`). Le
pendant de `nokker/cloud/mirror.py`.

`MirrorWorker` (un fil de fond) :
- **poussée** : relit le magasin du noyau après un curseur DURABLE (`mirror_push_cursor` dans l'état local,
  `setup.json`) et envoie les tours des fils de la machine à `POST /v1/mirror/turns` (jeton de machine). Le curseur
  n'avance qu'après un envoi accepté : un arrêt brutal ne perd rien, l'envoi reprend au même point (le web saute ce
  qu'il a déjà). Les copies venues du web (`mirror-web-*`) ne partent jamais (pas d'aller-retour) ;
- **tirage** : `GET /v1/mirror/turns?since=` range les tours des sessions web sous `mirror-web-<fil>` (lecture seule),
  curseur `mirror_cursor`.
Le fil de fond n'a pas de requête autour de lui : il pose lui-même le locataire du noyau local (`tenant`), sinon un
magasin Postgres refuse tout (NoTenant). Rien du dossier de travail ne part : seulement les tours.

Effacement propagé (§6.3) :
- le propriétaire efface les copies de sa machine chez nokker.ai (`erase_remote`, route locale
  `POST /v1/local/mirror/erase`) : l'envoi passe en pause (`mirror_paused`) jusqu'à `resume`, qui renvoie tout ;
- compte effacé ou machine déliée chez nokker.ai : le tirage reçoit 401. Après `UNLINKED_AFTER` refus d'affilée, les
  copies des fils web gardées ici sont effacées (fonction `nokker_mirror_erase`, copies seulement) et le curseur de
  tirage repart de zéro.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

from nokker.store.pg import turn_from_dict
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

logger = logging.getLogger("nokker.local.mirror")
WEB_PREFIX = "mirror-web-"
BATCH = 200
UNLINKED_AFTER = 3  # 401 d'affilée au tirage (≈ 90 s) : un refus isolé n'efface rien


def _cursor(raw: str | None) -> tuple[str, str, int]:
    if not raw:
        return ("-infinity", "", -1)  # avant tout horodatage (Postgres) et tout rang (mémoire)
    created, tid, idx = raw.split("|", 2)
    return (created, tid, int(idx))


@dataclass
class MirrorWorker:
    store: Any  # Store du noyau local
    base_url: str  # https://app.nokker.ai
    token: Any  # () -> jeton de machine
    state: dict[str, Any]  # setup.state (curseurs « mirror_push_cursor », « mirror_cursor »)
    save: Any  # () -> None (persiste l'état)
    tenant: str = INTERNAL_TENANT  # locataire du noyau local (le propriétaire)
    push_s: float = 5.0
    pull_s: float = 30.0
    client: Any = None
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _refused: int = field(default=0, init=False, repr=False)

    def start(self) -> None:
        threading.Thread(target=self._run, name="nokker-mirror", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _http(self) -> Any:
        return self.client or httpx.Client(timeout=30.0)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def push_once(self) -> int:
        """Envoie au plus `BATCH` tours après le curseur durable ; rend le nombre de tours envoyés."""
        if self.state.get("mirror_paused"):
            return 0
        with tenant_scope(self.tenant):
            rows = self.store.turns_after(_cursor(self.state.get("mirror_push_cursor")), BATCH * 2)
        batch, last = [], None
        for created, tid, idx, d in rows:
            last = f"{created}|{tid}|{idx}"  # avance aussi sur les copies web sautées
            if tid.startswith(WEB_PREFIX):
                continue
            batch.append({"thread_id": tid, "turn": d})
            if len(batch) >= BATCH:
                break
        if last is None:
            return 0
        if batch:
            try:
                r = self._http().post(self.base_url.rstrip("/") + "/v1/mirror/turns", headers=self._headers(),
                                      json={"turns": batch})
                r.raise_for_status()
            except httpx.HTTPError as e:
                logger.info("miroir : envoi reporté (%s)", type(e).__name__)
                return 0  # curseur inchangé : le même lot repart au prochain passage
        self.state["mirror_push_cursor"] = last
        self.save()
        return len(batch)

    def pull_once(self) -> int:
        params = {"since": self.state["mirror_cursor"]} if self.state.get("mirror_cursor") else {}
        try:
            r = self._http().get(self.base_url.rstrip("/") + "/v1/mirror/turns", headers=self._headers(),
                                 params=params)
            if r.status_code == 401:
                self._refused += 1
                if self._refused >= UNLINKED_AFTER:
                    self.purge_web_copies()
                return 0
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.info("miroir : tirage reporté (%s)", type(e).__name__)
            return 0
        self._refused = 0
        n = 0
        with tenant_scope(self.tenant):
            for it in data.get("turns") or []:
                target = WEB_PREFIX + str(it["thread_id"])
                d = dict(it["turn"])
                if self.store.next_index(target) > int(d["index"]):
                    continue
                d["thread_id"] = target
                self.store.append(dataclasses.replace(turn_from_dict(d), thread_id=target))
                n += 1
        if data.get("cursor"):
            self.state["mirror_cursor"] = data["cursor"]
            self.save()
        return n

    def purge_web_copies(self) -> int:
        """Efface les copies des fils web gardées sur la machine (compte effacé ou machine déliée chez nokker.ai)."""
        with tenant_scope(self.tenant):
            n = self.store.erase_mirror_copies(WEB_PREFIX)
        self.state.pop("mirror_cursor", None)
        self.save()
        self._refused = 0
        logger.info("miroir : %d tour(s) des copies web effacés (machine refusée par nokker.ai)", n)
        return n

    def erase_remote(self) -> int:
        """Efface chez nokker.ai les copies de cette machine et met l'envoi en pause. Lève httpx.HTTPError si nokker.ai
        ne répond pas : rien n'est mis en pause tant que l'effacement n'est pas confirmé."""
        r = self._http().post(self.base_url.rstrip("/") + "/v1/mirror/erase", headers=self._headers(), json={})
        r.raise_for_status()
        self.state["mirror_paused"] = True
        self.state.pop("mirror_push_cursor", None)  # une reprise renvoie tout
        self.save()
        return int(r.json().get("erased_turns", 0))

    def resume(self) -> None:
        self.state.pop("mirror_paused", None)
        self.save()

    def status(self) -> dict[str, Any]:
        return {"enabled": True, "paused": bool(self.state.get("mirror_paused")),
                "push_cursor": self.state.get("mirror_push_cursor"), "pull_cursor": self.state.get("mirror_cursor")}

    def _run(self) -> None:
        ticks = 0
        while not self._stop.wait(self.push_s):
            try:
                while self.push_once() >= BATCH:  # rattrapage (premier miroir d'un long historique)
                    pass
                ticks += 1
                if ticks * self.push_s >= self.pull_s:
                    ticks = 0
                    self.pull_once()
            except Exception:  # noqa: BLE001 — un fil de fond qui meurt arrête le miroir en silence
                logger.warning("miroir : passage en échec, repris au suivant", exc_info=True)


__all__ = ["MirrorWorker", "WEB_PREFIX"]
