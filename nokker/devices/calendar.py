"""L'agenda du téléphone comme agenda de 068 (P8) : une boîte `calendar` de mode `device`, en lecture seule, propre au
membre (jamais partagée dans le foyer). `calendar_events` la lit par une demande à l'app (`calendar.list`, 20 s).

Textes vus par le modèle : relus et corrigés avant fusion (06.11.2026), mot pour mot ; empreinte figée par un test
(`texts_sha256`). `<label>` = nom de l'appareil, neutralisé (`qa/untrusted.inline`) et coupé à 40 partout où il entre.
Les événements de l'appareil sont normalisés par liste blanche (nokker/devices/shapes.py) puis rendus sous la forme des
autres agendas, avec `"source": "device"` posé par le NOYAU après la normalisation ; ils restent dans l'unique clôture
`agenda` de `calendar_events`. Les événements CalDAV, Google, Graph et iCal ne changent pas de forme.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from nokker.connectors.boxes import Box
from nokker.connectors.calendar import fence_events
from nokker.core.ports import ToolContext, ToolResult
from nokker.devices.broker import DeviceRequestError

MODE = "device"
MAX_DAYS = 31
RIGHT = "à toi, lecture seule, agenda du téléphone"
TEXTS = {
    "device_offline": ("device_offline : l'agenda « {label} » est sur le téléphone, qui n'a pas répondu en 20 s (app "
                       "fermée ou hors ligne) ; rien n'a été lu ; demande à la personne d'ouvrir l'app Nokker sur le "
                       "téléphone, puis réessaie."),
    "device_denied": ("device_denied : le téléphone refuse l'accès à l'agenda « {label} » (autorisation retirée dans "
                      "ses réglages) ; rien n'a été lu ; la personne peut la rétablir dans les réglages du téléphone."),
    "device_too_large": ("device_too_large : l'agenda « {label} » a renvoyé trop d'éléments ; relance calendar_events "
                         "sur une période plus courte."),
    "device_rate": ("device_rate : limite de 30 lectures par heure sur les téléphones atteinte ; l'agenda « {label} » "
                    "est sauté jusqu'à {hhmm} (heure locale)."),
    "calendar_read_only": ("calendar_read_only : l'agenda « {label} » est celui du téléphone, en lecture seule ; rien "
                           "n'a été modifié ; pour ajouter ou changer un rendez-vous, utilise un agenda modifiable de "
                           "la liste (calendar=…)."),
}
STATUS_KEYS = ("device_offline", "device_denied", "device_too_large", "device_rate")


def texts_sha256() -> str:
    """Empreinte des textes vus par le modèle (JSON trié de RIGHT et TEXTS) : à changer avec une relecture."""
    blob = json.dumps({"RIGHT": RIGHT, "TEXTS": TEXTS}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def box_id(account: str, device_id: str) -> str:
    return "ca_" + hashlib.sha256(f"{account}|{device_id}".encode()).hexdigest()[:10]


def error_text(code: str, label: str, retry_at: float | None = None, tz: str = "Europe/Zurich") -> str:
    hhmm = ""
    if retry_at is not None:
        try:
            from zoneinfo import ZoneInfo

            hhmm = datetime.fromtimestamp(retry_at, tz=ZoneInfo(tz)).strftime("%H:%M")
        except Exception:  # noqa: BLE001
            hhmm = datetime.fromtimestamp(retry_at, tz=UTC).strftime("%H:%M")
    return TEXTS[code].format(label=label, hhmm=hhmm)


def to_calendar_event(e: dict[str, Any]) -> dict[str, Any]:
    """Événement normalisé de l'appareil (liste blanche) → forme des agendas de 068, `source` posé par le noyau."""
    ident = hashlib.sha256(json.dumps([e["title"], e["start"], e["calendar"]], ensure_ascii=False).encode()).hexdigest()
    return {"id": "dev-" + ident[:12], "summary": e["title"], "start": e["start"], "end": e["end"],
            "location": e.get("location", ""), "description": e["notes"], "attendees": [],
            "all_day": e["all_day"], "source": "device"}


class DeviceCalendarSource:
    """Ce que BoxSource attend d'un connecteur d'agenda (`call`), pour UN téléphone du membre."""

    def __init__(self, broker: Any, member: str, device_id: str, label: str,
                 clock: Callable[[], float] = time.time) -> None:
        self.broker, self.member, self.device_id, self.label, self.clock = broker, member, device_id, label, clock

    def _err(self, name: str, text: str) -> ToolResult:
        return ToolResult(call_id="", name=name, content=text, is_error=True)

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if name != "calendar_events":
            return self._err(name, error_text("calendar_read_only", self.label))
        try:
            days = int(arguments.get("days") or 7)
        except (TypeError, ValueError):
            days = 7
        days = max(1, days)
        clamped = days > MAX_DAYS
        start = datetime.fromtimestamp(self.clock(), tz=UTC)
        args = {"from": start.isoformat(), "to": (start + timedelta(days=min(days, MAX_DAYS))).isoformat()}
        try:
            items = self.broker.request(self.member, "calendar.list", args, device_id=self.device_id)
        except DeviceRequestError as e:
            code = e.code if e.code in TEXTS else "device_offline"
            return self._err(name, error_text(code, self.label, e.retry_at))
        events = sorted((to_calendar_event(x) for x in items), key=lambda x: (x["start"], x["summary"]))
        out: dict[str, Any] = {"count": len(events), "events": events}
        if clamped:
            out["truncated"] = len(events)
        return ToolResult(call_id="", name=name, content=fence_events(out))


class DeviceCalendars:
    """Les agendas des téléphones d'un membre, pour le fournisseur des connecteurs (068)."""

    def __init__(self, broker: Any) -> None:
        self.broker = broker

    def boxes(self, account: str) -> list[Box]:
        try:
            devices = self.broker.capable(account, "calendar.list")
        except Exception:  # noqa: BLE001 — sans la liste des appareils : les autres agendas seulement
            return []
        return [Box(id=box_id(account, d.id), kind="calendar", label=d.name, access="owner", owner_account=account,
                    mode=MODE) for d in devices]

    def source(self, account: str, box: Box) -> DeviceCalendarSource | None:
        for d in self.broker.capable(account, "calendar.list"):
            if box_id(account, d.id) == box.id:
                return DeviceCalendarSource(self.broker, account, d.id, box.label)
        return None


__all__ = ["MODE", "RIGHT", "STATUS_KEYS", "TEXTS", "DeviceCalendarSource", "DeviceCalendars", "box_id", "error_text",
           "texts_sha256", "to_calendar_event"]
