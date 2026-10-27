"""`InMemoryApprovals` — backend `Approvals` pour les tests (docs/APPROVALS.md §4). Même comportement que
`PgApprovals` (toute la logique de preuve est dans `base.BaseApprovals`, partagée) ; ici, de simples
dicts protégés par un verrou — aucune persistance, aucun réseau."""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field

from nokker.approvals.base import BaseApprovals
from nokker.approvals.types import Device, Session
from nokker.core.ports import Approval


@dataclass
class InMemoryApprovals(BaseApprovals):
    _approvals: dict[str, Approval] = field(default_factory=dict)
    _events: list[tuple[str, str, dict]] = field(default_factory=list)
    _nonces: dict[str, dict] = field(default_factory=dict)  # nonce -> {scope_id, exp, used}
    _devices: dict[str, Device] = field(default_factory=dict)
    _devices_by_code: dict[str, str] = field(default_factory=dict)  # code -> device_id
    _totp: dict[str, tuple[str, int | None]] = field(default_factory=dict)  # user -> (secret, last_step)
    _sessions: dict[str, Session] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _load_approval(self, approval_id: str) -> Approval | None:
        with self._lock:
            a = self._approvals.get(approval_id)
            return copy.deepcopy(a) if a is not None else None

    def _store_approval(self, approval: Approval) -> None:
        with self._lock:
            self._approvals[approval.id] = copy.deepcopy(approval)

    def _list_pending_approvals(self, user: str | None) -> list[Approval]:
        with self._lock:
            items = [copy.deepcopy(a) for a in self._approvals.values() if a.status == "pending"]
        return items

    def _append_event(self, approval_id: str, event_type: str, data: dict) -> None:
        with self._lock:
            self._events.append((approval_id, event_type, dict(data)))

    def _reserve_nonce(self, nonce: str, scope_id: str, exp: float) -> None:
        with self._lock:
            self._nonces[nonce] = {"scope_id": scope_id, "exp": exp, "used": False}

    def _consume_nonce(self, nonce: str, scope_id: str) -> bool:
        with self._lock:
            row = self._nonces.get(nonce)
            if row is None or row["used"] or row["scope_id"] != scope_id or self.clock() > row["exp"]:
                return False
            row["used"] = True
            return True

    def _load_device(self, device_id: str) -> Device | None:
        with self._lock:
            d = self._devices.get(device_id)
            return copy.deepcopy(d) if d is not None else None

    def _load_device_by_code(self, code: str) -> Device | None:
        with self._lock:
            device_id = self._devices_by_code.get(code)
            if device_id is None:
                return None
            d = self._devices.get(device_id)
            return copy.deepcopy(d) if d is not None else None

    def _store_device(self, device: Device) -> None:
        with self._lock:
            self._devices[device.id] = copy.deepcopy(device)
            self._devices_by_code[device.code] = device.id

    def _list_devices(self, user: str | None) -> list[Device]:
        with self._lock:
            items = [copy.deepcopy(d) for d in self._devices.values() if user is None or d.user == user]
        return items

    def _delete_device(self, device_id: str) -> None:
        with self._lock:
            d = self._devices.pop(device_id, None)
            if d is not None:
                self._devices_by_code.pop(d.code, None)
        with self._sign_lock:
            self._sign_counts.pop(device_id, None)

    def _load_totp(self, user: str) -> tuple[str, int | None] | None:
        with self._lock:
            return self._totp.get(user)

    def _store_totp_secret(self, user: str, secret_b32: str) -> None:
        with self._lock:
            self._totp[user] = (secret_b32, None)

    def _set_totp_last_step(self, user: str, step: int) -> None:
        with self._lock:
            secret_b32, _ = self._totp.get(user, ("", None))
            self._totp[user] = (secret_b32, step)

    def _store_session(self, session: Session) -> None:
        with self._lock:
            self._sessions[session.token] = copy.deepcopy(session)

    def _load_session(self, token: str) -> Session | None:
        with self._lock:
            s = self._sessions.get(token)
            return copy.deepcopy(s) if s is not None else None


__all__ = ["InMemoryApprovals"]
