"""Suivi en mémoire du dernier outil et de l'état des tours, nourri par les événements du bus (`EventBus.observers`).
Aucune table : l'état durable (fils, cartes, objectifs, sessions, agents) est lu à la demande par `PresenceService` ;
le suivi ne garde que ce qui n'est écrit nulle part (dernier outil d'un tour, pause, état d'un agent de la salle Maison).
Perdu au redémarrage : la vue retombe alors sur l'état durable. Publie `presence.changed` quand l'état d'un fil change."""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from nokker.presence.model import AWAITING, IDLE, PAUSED, WORKING
from nokker.tenancy.context import current_tenant_or_none

logger = logging.getLogger("nokker.presence")
MAX_THREADS = 2000
_WATCHED = {"turn.started", "turn.done", "turn.paused", "turn.resumed", "tool.call", "approval.requested",
            "approval.decided", "house.agent"}


class ActivityTracker:
    def __init__(self, *, audience_of: Callable[[str], set[str]] | None = None) -> None:
        self.audience_of = audience_of
        self.publish: Callable[[str, dict[str, Any]], Any] | None = None
        self._lock = threading.Lock()
        self._threads: OrderedDict[tuple[str | None, str], dict[str, Any]] = OrderedDict()
        self._house: dict[tuple[str | None, str, str], str] = {}
        self._cards: dict[tuple[str | None, str], str] = {}  # carte → fil, pour approval.decided

    # ── lecture ──
    def thread(self, thread_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._threads.get((current_tenant_or_none(), thread_id), {}))

    def house_state(self, household_id: str, slug: str) -> str:
        with self._lock:
            return self._house.get((current_tenant_or_none(), household_id, slug), IDLE)

    # ── écriture (appelé par le bus, hors de son verrou) ──
    def observe(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type not in _WATCHED:
            return
        tenant = data.get("_tenant", current_tenant_or_none())
        try:
            if event_type == "house.agent":
                with self._lock:
                    self._house[(tenant, str(data.get("household_id")), str(data.get("agent")))] = \
                        WORKING if data.get("state") == "working" else IDLE
                return
            if event_type.startswith("approval."):
                a = data.get("approval")
                tid = getattr(a, "thread_id", None)
                if event_type == "approval.requested" and tid:
                    with self._lock:
                        self._cards[(tenant, getattr(a, "id", ""))] = tid
                else:
                    with self._lock:
                        tid = self._cards.pop((tenant, str(data.get("approval_id"))), None)
                if not tid:
                    return
                self._set(tenant, tid, AWAITING if event_type == "approval.requested" else WORKING, data=None)
                return
            tid = data.get("thread_id")
            if not tid:
                return
            if event_type == "tool.call":
                self._set(tenant, tid, WORKING, tool=data.get("name"), audience=data.get("_audience"))
            elif event_type == "turn.started":
                self._set(tenant, tid, WORKING, tool=None, audience=data.get("_audience"))
            elif event_type == "turn.paused":
                self._set(tenant, tid, PAUSED, audience=data.get("_audience"))
            elif event_type == "turn.resumed":
                self._set(tenant, tid, WORKING, audience=data.get("_audience"))
            elif event_type == "turn.done":
                self._set(tenant, tid, IDLE, tool=None, audience=data.get("_audience"))
        except Exception:  # noqa: BLE001 — le suivi ne casse jamais le bus
            logger.warning("présence : événement %s non suivi", event_type, exc_info=True)

    def _set(self, tenant: str | None, thread_id: str, state: str, *, data: Any = None, audience: Any = None,
             tool: str | None | bool = False) -> None:
        key = (tenant, thread_id)
        with self._lock:
            cur = self._threads.pop(key, {})
            before = cur.get("state")
            cur["state"] = state
            if tool is not False:
                cur["tool"] = tool
            self._threads[key] = cur
            while len(self._threads) > MAX_THREADS:
                self._threads.popitem(last=False)
            tool_now = cur.get("tool")
        if before == state or self.publish is None:
            return
        who = audience
        if who is None and self.audience_of is not None:
            who = sorted(self.audience_of(thread_id))
        payload: dict[str, Any] = {"id": thread_id, "status": state, "activity_tool": tool_now, "_tenant": tenant}
        if who is not None:
            payload["_audience"] = list(who)
        self.publish("presence.changed", payload)
