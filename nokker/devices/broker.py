"""Demandes du noyau à l'app du membre (P8) : `device.request` sur le flux `/v1/events` des sessions du membre, réponse
par `POST /v1/me/device-requests/{id}/result`.

- La demande ne part que vers les sessions DU MEMBRE (audience `[membre]`, dans son locataire), et seulement si l'un de
  ses appareils déclare la capacité (`devices` : ceux qui peuvent répondre).
- 20 s pour répondre ; sinon `device_offline`. 30 demandes par heure et par membre ; au-delà, `device_rate` (avec
  l'heure où une place se libère).
- Une réponse vient d'un appareil du membre qui a la capacité : 204 ; déjà répondue : 409 ; expirée : 410 ; demande
  d'un autre membre (ou inconnue) : 403.
- Rien n'est gardé : la demande et sa réponse vivent en mémoire le temps du tour (un seul réplica par noyau, comme la
  présence et les limites de débit du lien).
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.devices.shapes import CAP_OF, ShapeError, clean_args, clean_items

TIMEOUT_S = 20.0
RATE_MAX = 30
RATE_WINDOW_S = 3600.0
ERRORS = ("denied", "unavailable", "too_large")


class DeviceRequestError(Exception):
    """Échec d'une demande : `code` = device_offline | device_denied | device_too_large | device_rate | no_device ;
    `retry_at` (epoch) pour device_rate."""

    def __init__(self, code: str, retry_at: float | None = None) -> None:
        super().__init__(code)
        self.code, self.retry_at = code, retry_at


@dataclass
class _Pending:
    member: str
    op: str
    devices: frozenset[str]
    expires_at: float
    done: threading.Event = field(default_factory=threading.Event)
    answered: bool = False
    items: list[dict[str, Any]] | None = None
    error: str | None = None


@dataclass
class DeviceBroker:
    devices: Any  # MemoryDeviceStore | PgDeviceStore
    publish: Callable[[str, dict[str, Any]], Any] | None = None  # EventBus.publish
    clock: Callable[[], float] = time.time
    timeout_s: float = TIMEOUT_S
    # P7a : un téléphone EN LIGNE du membre (nœud, nokker/cloud/phone.py) sert la demande par la file ; None = pas de
    # tel nœud, repli sur device.request. `(membre, op, args) → éléments | None`, lève DeviceRequestError.
    via_node: Callable[[str, str, dict[str, Any]], list[dict[str, Any]] | None] | None = None
    _pending: dict[str, _Pending] = field(default_factory=dict, init=False)
    _sent: dict[str, deque] = field(default_factory=dict, init=False)
    # demandes closes : id → (membre, answered | expired) ; ni la demande ni sa réponse
    _gone: dict[str, tuple[str, str]] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def capable(self, member: str, op: str) -> list[Any]:
        cap = CAP_OF[op]
        return [d for d in self.devices.list(member) if cap in d.caps]

    def _rate(self, member: str) -> None:
        now = self.clock()
        with self._lock:
            q = self._sent.setdefault(member, deque())
            while q and now - q[0] > RATE_WINDOW_S:
                q.popleft()
            if len(q) >= RATE_MAX:
                raise DeviceRequestError("device_rate", retry_at=q[0] + RATE_WINDOW_S)
            q.append(now)

    def request(self, member: str, op: str, args: dict[str, Any], *, device_id: str | None = None
                ) -> list[dict[str, Any]]:
        """Envoie la demande et attend la réponse (20 s). `device_id` : un appareil précis (agenda d'un téléphone)."""
        if self.via_node is not None:
            got = self.via_node(member, op, args)
            if got is not None:
                try:
                    return clean_items(op, got)  # même liste blanche, mêmes bornes que par l'app
                except ShapeError:
                    raise DeviceRequestError("device_too_large") from None
        targets = [d for d in self.capable(member, op) if device_id is None or d.id == device_id]
        if not targets or self.publish is None:
            raise DeviceRequestError("no_device")
        self._rate(member)
        rid = "dr_" + secrets.token_hex(10)
        p = _Pending(member=member, op=op, devices=frozenset(d.id for d in targets),
                     expires_at=self.clock() + self.timeout_s)
        with self._lock:
            self._pending[rid] = p
        try:
            self.publish("device.request", {"request_id": rid, "op": op, "args": clean_args(op, args),
                                            "devices": sorted(p.devices), "expires_at": p.expires_at,
                                            "_audience": [member]})
            if not p.done.wait(self.timeout_s):
                raise DeviceRequestError("device_offline")
            if p.error == "denied":
                raise DeviceRequestError("device_denied")
            if p.error == "too_large":
                raise DeviceRequestError("device_too_large")
            if p.error is not None:
                raise DeviceRequestError("device_offline")  # unavailable : l'agenda n'est pas lisible là
            return list(p.items or [])
        finally:
            with self._lock:  # rien n'est gardé après le tour : seulement l'issue, pour 409 / 410 / 403
                gone = self._pending.pop(rid, None)
                if gone is not None:
                    self._closed(rid, gone.member, "answered" if gone.answered else "expired")

    def _closed(self, rid: str, member: str, outcome: str) -> None:
        self._gone[rid] = (member, outcome)
        if len(self._gone) > 1000:
            for k in list(self._gone)[:500]:
                del self._gone[k]

    def answer(self, member: str, request_id: str, body: Any) -> tuple[int, dict[str, Any] | None]:
        """`POST /v1/me/device-requests/{id}/result` → (statut, corps)."""
        if not isinstance(body, dict) or set(body) - {"device_id", "items", "error"} \
                or ("items" in body) == ("error" in body):
            return 400, {"error": "bad_request", "message": "corps attendu : {device_id, items | error}"}
        with self._lock:
            p = self._pending.get(request_id)
            if p is None:
                who, outcome = self._gone.get(request_id, ("", ""))
                if not who or who != member:
                    return 403, {"error": "not_yours"}
                return (409, {"error": "already_answered"}) if outcome == "answered" else (410, {"error": "expired"})
            if p.member != member:
                return 403, {"error": "not_yours"}
            if p.answered:
                return 409, {"error": "already_answered"}
            if self.clock() > p.expires_at:
                return 410, {"error": "expired"}
            if body.get("device_id") not in p.devices:
                return 403, {"error": "not_yours", "message": "appareil inconnu ou sans cette capacité"}
            if "error" in body:
                if body["error"] not in ERRORS:
                    return 400, {"error": "bad_request", "message": f"error : {' | '.join(ERRORS)}"}
                p.error = str(body["error"])
            else:
                try:
                    p.items = clean_items(p.op, body.get("items"))
                except ShapeError:
                    p.error = "too_large"
            p.answered = True
            p.done.set()
        return 204, None


__all__ = ["ERRORS", "RATE_MAX", "TIMEOUT_S", "DeviceBroker", "DeviceRequestError"]
