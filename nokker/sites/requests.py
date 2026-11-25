"""Sites connectés — demandes à l'utilisateur et armement de la capture du chat (docs/CONNECTED-SITES.md §4.2, §4.5,
§4.6, §15).

Une **demande** (`password`, `code`, `link`, `app_approval`) part sur Telegram (mini-app) ou le web : elle porte une
clé publique éphémère de la machine ; la réponse arrive chiffrée (`crypto.MachineKeyring`) ou, pour `app_approval`,
comme un simple « Fait ». Une seule demande ouverte par connecteur ; échéance courte (code et validation : 3 min ;
mot de passe et lien : 10 min) ; une réponse ne sert qu'une fois.

L'**armement** est posé par le RÉSULTAT de `sites_connect` (`needs_password`, `needs_code`) pour UN message de CE fil et
de CET expéditeur, jusqu'à l'échéance de la demande : si la personne écrit quand même la valeur dans le chat, la
capture (`capture.py`) la prend avant le modèle et la remet à la demande, comme une réponse de la mini-app.
"""
from __future__ import annotations

import secrets as _secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.sites.crypto import MachineKeyring, SealedError

KINDS = ("password", "code", "link", "app_approval")
TTL_S = {"password": 600.0, "link": 600.0, "code": 180.0, "app_approval": 180.0}


class RequestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Request:
    id: str
    connector: str
    account: str
    label: str
    kind: str
    expires_at: float
    machine_key: dict[str, str] | None
    answered: bool = False
    cancelled: bool = False
    _value: str | None = field(default=None, repr=False)  # jamais dans un repr, jamais sérialisé
    _event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label, "expires_at": self.expires_at,
                "machine_key": self.machine_key}


@dataclass
class Arm:
    request_id: str
    kind: str  # password | code
    label: str
    expires_at: float


class Requests:
    def __init__(self, keyring: MachineKeyring | None = None, clock: Callable[[], float] = time.time,
                 notify: Callable[[Request], None] | None = None) -> None:
        self.keyring = keyring or MachineKeyring(clock=clock)
        self.clock = clock
        self.notify = notify  # Telegram (mini-app) / web : prévient la personne qu'une saisie l'attend
        self._by_id: dict[str, Request] = {}
        self._arms: dict[tuple[str, str], Arm] = {}  # (fil, expéditeur) -> armement
        self._lock = threading.Lock()

    # ── demandes ──
    def open(self, connector: str, account: str, label: str, kind: str) -> Request:
        if kind not in KINDS:
            raise RequestError("bad_kind", "demande inconnue")
        with self._lock:
            for r in self._by_id.values():  # une seule ouverte par connecteur
                if r.connector == connector and not r.answered and not r.cancelled:
                    r.cancelled = True
                    r._event.set()
                    self.keyring.forget(r.id)
            rid = "rq_" + _secrets.token_hex(8)
            ttl = TTL_S[kind]
            key = self.keyring.public_key(rid, ttl) if kind != "app_approval" else None
            req = Request(id=rid, connector=connector, account=account, label=label, kind=kind,
                          expires_at=self.clock() + ttl, machine_key=key)
            self._by_id[rid] = req
        if self.notify is not None:
            try:
                self.notify(req)
            except Exception:  # noqa: BLE001 — la page web montre aussi la demande
                pass
        return req

    def get(self, rid: str) -> Request | None:
        return self._by_id.get(rid)

    def open_for(self, connector: str) -> Request | None:
        for r in self._by_id.values():
            if r.connector == connector and not r.answered and not r.cancelled and self.clock() <= r.expires_at:
                return r
        return None

    def _check(self, rid: str, account: str | None) -> Request:
        req = self._by_id.get(rid)
        if req is None or (account is not None and req.account != account):
            raise RequestError("not_found", "demande inconnue")
        if req.cancelled or self.clock() > req.expires_at:
            raise RequestError("expired", "demande expirée")
        if req.answered:
            raise RequestError("answered", "demande déjà satisfaite")
        return req

    def answer_sealed(self, rid: str, sealed: dict, account: str | None = None) -> None:
        req = self._check(rid, account)
        if req.kind == "app_approval":
            raise RequestError("bad_answer", "cette demande attend « Fait »")
        try:
            value = self.keyring.open(rid, sealed)
        except SealedError as e:
            raise RequestError("bad_answer", str(e)) from None
        self._deliver(req, value)

    def answer_done(self, rid: str, account: str | None = None) -> None:
        req = self._check(rid, account)
        if req.kind != "app_approval":
            raise RequestError("bad_answer", "cette demande attend une saisie")
        self._deliver(req, "")

    def answer_plain(self, rid: str, value: str) -> None:
        """Valeur captée dans le chat (cas armé, `capture.py`) : même chemin qu'une réponse de la mini-app, sans
        chiffrement puisqu'elle est déjà sur la machine."""
        req = self._check(rid, None)
        self.keyring.forget(rid)
        self._deliver(req, value)

    def _deliver(self, req: Request, value: str) -> None:
        req._value = value
        req.answered = True
        self._disarm(req.id)
        req._event.set()

    def wait(self, rid: str, timeout_s: float | None = None) -> str | None:
        """Attend la réponse ; rend la valeur UNE fois (puis l'oublie), ou None (expirée, annulée)."""
        req = self._by_id.get(rid)
        if req is None:
            return None
        left = max(0.0, req.expires_at - self.clock())
        req._event.wait(min(left, timeout_s) if timeout_s is not None else left)
        if not req.answered:
            return None
        value, req._value = req._value, None
        return value

    def cancel(self, rid: str) -> None:
        req = self._by_id.get(rid)
        if req is not None and not req.answered:
            req.cancelled = True
            self.keyring.forget(rid)
            self._disarm(rid)
            req._event.set()

    # ── armement du chat (§4.6) ──
    def arm(self, thread_id: str, sender: str | None, req: Request) -> None:
        if req.kind not in ("password", "code"):
            return
        with self._lock:
            self._arms[(thread_id, sender or "")] = Arm(req.id, req.kind, req.label, req.expires_at)

    def take_arm(self, thread_id: str, sender: str | None) -> Arm | None:
        """L'armement de ce fil et de cet expéditeur, consommé (UN message), s'il vit encore."""
        with self._lock:
            arm = self._arms.pop((thread_id, sender or ""), None)
        if arm is None or self.clock() > arm.expires_at:
            return None
        req = self._by_id.get(arm.request_id)
        if req is None or req.answered or req.cancelled:
            return None
        return arm

    def _disarm(self, rid: str) -> None:
        with self._lock:
            for k in [k for k, a in self._arms.items() if a.request_id == rid]:
                self._arms.pop(k, None)


__all__ = ["Arm", "KINDS", "Request", "RequestError", "Requests", "TTL_S"]
