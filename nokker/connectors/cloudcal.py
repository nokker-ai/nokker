"""Agendas Google (Calendar API v3) et Microsoft (Graph v1.0), reliés par OAuth (docs/CONNECTORS-OAUTH.md §5).

Mêmes outils, mêmes arguments et MÊME forme de résultat que l'agenda CalDAV (`calendar.py`) : la source regroupée
(`boxes.py`) en fait un agenda de plus dans `calendar`. `calendar_events` rend un résultat CLÔTURÉ (`fence_events`).
Aucune invitation n'est envoyée : Google `sendUpdates=none` ; Graph envoie des invitations dès qu'un événement a des
participants, donc les invités ne sont PAS posés comme participants Graph — ils sont écrits dans la description.
Hôtes fixes (`www.googleapis.com`, `graph.microsoft.com`) ; le « secret » est un jeton d'accès frais, jamais montré.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from nokker.connectors.calendar import DEFAULT_TZ, MAX_EVENTS, SOURCE, CalendarConnector, fence_events, parse_input_time
from nokker.connectors.errors import ConnectorError, scrub
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import ToolResult

GOOGLE = "https://www.googleapis.com/calendar/v3"
GRAPH = "https://graph.microsoft.com/v1.0"
MODES = ("google", "graph")


@dataclass(frozen=True)
class ApiCal:
    id: str
    name: str
    writable: bool = True


def _specs(locality: str | None) -> list[ToolSpec]:
    return CalendarConnector({"mode": "caldav", "url": "https://caldav.invalid/", "username": "x"}, "x",
                             locality=locality).specs()


class ApiCalendar:
    """`ToolSource` agenda Google ou Graph d'un compte. `client` : un `httpx.Client` (tests : MockTransport)."""

    def __init__(self, settings: dict[str, Any], token: str, *, locality: str | None = None, client: Any = None,
                 now: Any = None) -> None:
        self.mode = str(settings.get("mode") or "")
        if self.mode not in MODES:
            raise ConnectorError("bad_settings", "mode agenda inconnu")
        if not token:
            raise ConnectorError("bad_settings", "jeton manquant")
        try:
            self.tz = ZoneInfo(str(settings.get("tz") or DEFAULT_TZ))
        except Exception:  # noqa: BLE001
            raise ConnectorError("bad_settings", "fuseau horaire inconnu") from None
        self._token, self.locality = token, locality
        self._now = now or (lambda: datetime.now(self.tz))
        if client is None:
            import httpx

            client = httpx.Client(timeout=20.0, follow_redirects=False)
        self._client = client
        self._cals: list[ApiCal] | None = None

    # ── HTTP ──
    def _req(self, method: str, url: str, *, params: dict[str, Any] | None = None, body: Any = None,
             headers: dict[str, str] | None = None) -> Any:
        h = {"Authorization": f"Bearer {self._token}", "Accept": "application/json", **(headers or {})}
        try:
            r = self._client.request(method, url, params=params, json=body, headers=h)
        except Exception:  # noqa: BLE001 — réseau : sans détail
            raise ConnectorError("unreachable", "agenda injoignable") from None
        if r.status_code in (401, 403):
            raise ConnectorError("auth_failed", "accès refusé par le fournisseur (droits ou compte à reconnecter)")
        if r.status_code == 404:
            raise ValueError("événement ou agenda introuvable")
        if r.status_code >= 400:
            raise ConnectorError("protocol_error", f"réponse {r.status_code} du fournisseur")
        if r.status_code == 204 or not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            raise ConnectorError("protocol_error", "réponse illisible") from None

    # ── agendas ──
    def _discover(self) -> list[ApiCal]:
        if self._cals is None:
            if self.mode == "google":
                items = self._req("GET", f"{GOOGLE}/users/me/calendarList").get("items") or []
                cals = [ApiCal(i["id"], str(i.get("summary") or i["id"]), i.get("accessRole") in ("owner", "writer"))
                        for i in items if i.get("id")]
                cals.sort(key=lambda c: next((0 for i in items if i.get("id") == c.id and i.get("primary")), 1))
            else:
                items = self._req("GET", f"{GRAPH}/me/calendars").get("value") or []
                cals = [ApiCal(i["id"], str(i.get("name") or "Calendar"), bool(i.get("canEdit", True)))
                        for i in items if i.get("id")]
                cals.sort(key=lambda c: next((0 for i in items if i.get("id") == c.id and i.get("isDefaultCalendar")), 1))
            if not cals:
                raise ConnectorError("protocol_error", "aucun agenda sur ce compte")
            self._cals = cals
        return self._cals

    def _select(self, calendar: str | None, *, default_all: bool = False) -> list[ApiCal]:
        cals = self._discover()
        c = (calendar or "").strip()
        if c.lower() == "all" or (not c and default_all):
            return cals
        if not c or c.lower() == "primary":
            return cals[:1]
        for cal in cals:
            if c.lower() in (cal.name.lower(), cal.id.lower()):
                return [cal]
        raise ValueError(f"agenda inconnu : {c!r} (disponibles : {', '.join(x.name for x in cals)})")

    # ── rendu (même forme que calendar._render) ──
    def _fmt(self, when: dict[str, Any] | None, *, all_day: bool, end: bool = False) -> str:
        when = when or {}
        if all_day:
            d = date.fromisoformat(str(when.get("date") or when.get("dateTime", ""))[:10])
            return (d - timedelta(days=1)).isoformat() if end else d.isoformat()  # fin exclusive → fin incluse
        raw = re.sub(r"(\.\d{6})\d+", r"\1", str(when.get("dateTime") or ""))  # Graph : 7 décimales
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if t.tzinfo is None:  # Graph : heure dans le fuseau demandé (en-tête Prefer) ou celui donné
            t = t.replace(tzinfo=ZoneInfo(str(when.get("timeZone") or "UTC")) if when.get("timeZone") not in (None, "UTC")
                          else UTC)
        return t.astimezone(self.tz).isoformat(timespec="minutes")

    def _render(self, e: dict[str, Any], native_id: str) -> dict[str, Any]:
        if self.mode == "google":
            all_day = "date" in (e.get("start") or {})
            summary, loc = e.get("summary") or "", e.get("location") or ""
            desc, att = e.get("description") or "", [a.get("email", "") for a in e.get("attendees") or []]
        else:
            all_day = bool(e.get("isAllDay"))
            summary, loc = e.get("subject") or "", (e.get("location") or {}).get("displayName") or ""
            desc = e.get("bodyPreview") or ""
            att = [(a.get("emailAddress") or {}).get("address", "") for a in e.get("attendees") or []]
        return {"id": native_id, "summary": summary, "start": self._fmt(e.get("start"), all_day=all_day),
                "end": self._fmt(e.get("end"), all_day=all_day, end=True), "location": loc,
                "description": str(desc)[:2000], "attendees": [a for a in att if a], "all_day": all_day}

    def _gid(self, cal: ApiCal, event_id: str) -> str:
        return f"{cal.id}::{event_id}" if self.mode == "google" else event_id

    def _split(self, native: str) -> tuple[str | None, str]:
        if self.mode == "google":
            cal, sep, ev = native.rpartition("::")
            if not sep:
                raise ValueError("id d'événement invalide (utiliser un id de calendar_events)")
            return cal, ev
        return None, native

    # ── outils ──
    def specs(self) -> list[ToolSpec]:
        return _specs(self.locality)

    def test(self) -> str:
        return f"agenda joignable ({len(self._discover())} agenda(s))"

    def _events(self, a: dict[str, Any]) -> dict[str, Any]:
        try:
            days = int(a.get("days", 7))
        except (TypeError, ValueError):
            raise ValueError("`days` doit être un entier") from None
        if not 1 <= days <= 366:
            raise ValueError("`days` doit être compris entre 1 et 366")
        t0 = self._now()
        t1 = t0 + timedelta(days=days)
        evs: list[tuple[str, dict[str, Any]]] = []
        for cal in self._select(a.get("calendar")):
            if self.mode == "google":
                items = self._req("GET", f"{GOOGLE}/calendars/{urllib.parse.quote(cal.id, safe='')}/events", params={
                    "timeMin": t0.astimezone(UTC).isoformat(), "timeMax": t1.astimezone(UTC).isoformat(),
                    "singleEvents": "true", "orderBy": "startTime", "maxResults": 250}).get("items") or []
                for e in items:
                    if e.get("status") != "cancelled":
                        r = self._render(e, self._gid(cal, str(e.get("id"))))
                        evs.append((r["start"], r))
            else:
                items = self._req("GET", f"{GRAPH}/me/calendars/{urllib.parse.quote(cal.id, safe='')}/calendarView", params={
                    "startDateTime": t0.astimezone(UTC).isoformat(), "endDateTime": t1.astimezone(UTC).isoformat(),
                    "$top": 250}, headers={"Prefer": f'outlook.timezone="{self.tz.key}"'}).get("value") or []
                for e in items:
                    if not e.get("isCancelled"):
                        r = self._render(e, str(e.get("id")))
                        evs.append((r["start"], r))
        evs.sort(key=lambda x: (x[0], x[1]["summary"]))
        events = [r for _, r in evs]
        out: dict[str, Any] = {"count": min(len(events), MAX_EVENTS), "events": events[:MAX_EVENTS]}
        if len(events) > MAX_EVENTS:
            out["truncated"] = len(events)
        return out

    def _times(self, a: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
        if partial and not a.get("start") and not a.get("end"):
            return {}
        start, all_day = parse_input_time(a.get("start") or "", self.tz)
        end, end_all = parse_input_time(a.get("end") or "", self.tz)
        if all_day != end_all:
            raise ValueError("`start` et `end` doivent être tous deux des dates ou tous deux des heures")
        if all_day:
            end = end + timedelta(days=1)  # type: ignore[operator]
        if end <= start:  # type: ignore[operator]
            raise ValueError("`end` doit être postérieur à `start`")
        if self.mode == "google":
            if all_day:
                return {"start": {"date": start.isoformat()}, "end": {"date": end.isoformat()}}
            return {"start": {"dateTime": start.isoformat(), "timeZone": self.tz.key},
                    "end": {"dateTime": end.isoformat(), "timeZone": self.tz.key}}
        fmt = (lambda v: f"{v.isoformat()}T00:00:00") if all_day else (
            lambda v: v.astimezone(self.tz).replace(tzinfo=None).isoformat(timespec="seconds"))
        return {"isAllDay": all_day, "start": {"dateTime": fmt(start), "timeZone": self.tz.key},
                "end": {"dateTime": fmt(end), "timeZone": self.tz.key}}

    def _body(self, a: dict[str, Any], *, partial: bool = False) -> tuple[dict[str, Any], list[str]]:
        attendees = [str(x).strip() for x in (a.get("attendees") or []) if str(x).strip()]
        if self.mode == "google":
            b: dict[str, Any] = {**self._times(a, partial=partial)}
            if a.get("summary") or not partial:
                b["summary"] = str(a.get("summary") or "")
            for k in ("location", "description"):
                if a.get(k) is not None:
                    b[k] = str(a[k])
            if attendees:
                b["attendees"] = [{"email": x} for x in attendees]
            return b, attendees
        b = {**self._times(a, partial=partial)}
        if a.get("summary") or not partial:
            b["subject"] = str(a.get("summary") or "")
        if a.get("location") is not None:
            b["location"] = {"displayName": str(a["location"])}
        desc = str(a.get("description") or "")
        if attendees:  # jamais des participants Graph : ils recevraient une invitation
            desc = (desc + "\n\n" if desc else "") + "Participants : " + ", ".join(attendees)
        if desc or a.get("description") is not None:
            b["body"] = {"contentType": "text", "content": desc}
        return b, attendees

    def _create(self, a: dict[str, Any]) -> dict[str, Any]:
        if not str(a.get("summary") or "").strip():
            raise ValueError("`summary` requis")
        cal = self._select(a.get("calendar"))[0]
        if not cal.writable:
            raise ValueError(f"agenda en lecture seule : {cal.name}")
        body, attendees = self._body(a)
        if self.mode == "google":
            e = self._req("POST", f"{GOOGLE}/calendars/{urllib.parse.quote(cal.id, safe='')}/events",
                          params={"sendUpdates": "none"}, body=body)
        else:
            e = self._req("POST", f"{GRAPH}/me/calendars/{urllib.parse.quote(cal.id, safe='')}/events", body=body)
        out: dict[str, Any] = {"created": True, "id": self._gid(cal, str(e.get("id"))), "calendar": cal.name}
        if attendees:
            out["note"] = "invités inscrits dans l'événement ; aucune invitation envoyée"
        return out

    def _update(self, a: dict[str, Any]) -> dict[str, Any]:
        cal_id, ev = self._split(str(a.get("event_id") or ""))
        body, _ = self._body(a, partial=True)
        if self.mode == "google":
            self._req("PATCH", f"{GOOGLE}/calendars/{urllib.parse.quote(cal_id or '', safe='')}/events/"
                               f"{urllib.parse.quote(ev, safe='')}", params={"sendUpdates": "none"}, body=body)
        else:
            self._req("PATCH", f"{GRAPH}/me/events/{urllib.parse.quote(ev, safe='')}", body=body)
        return {"updated": True, "id": str(a.get("event_id"))}

    def _delete(self, a: dict[str, Any]) -> dict[str, Any]:
        cal_id, ev = self._split(str(a.get("event_id") or ""))
        if self.mode == "google":
            self._req("DELETE", f"{GOOGLE}/calendars/{urllib.parse.quote(cal_id or '', safe='')}/events/"
                                f"{urllib.parse.quote(ev, safe='')}", params={"sendUpdates": "none"})
        else:
            self._req("DELETE", f"{GRAPH}/me/events/{urllib.parse.quote(ev, safe='')}")
        return {"deleted": True, "id": str(a.get("event_id"))}

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        fn = {"calendar_events": self._events, "calendar_create": self._create, "calendar_update": self._update,
              "calendar_delete": self._delete}.get(name)
        if fn is None:
            return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)
        try:
            out = fn(arguments or {})
            return ToolResult(call_id="", name=name,
                              content=fence_events(out) if name == "calendar_events" else json.dumps(out, ensure_ascii=False))
        except ConnectorError as e:
            msg = f"{e.code} : {scrub(e.message, self._token)}"
        except ValueError as e:
            msg = scrub(str(e), self._token)
        except Exception as e:  # noqa: BLE001 — jamais d'exception hors de call(), jamais le jeton
            msg = f"erreur interne : {scrub(type(e).__name__, self._token)}"
        return ToolResult(call_id="", name=name, content=msg, is_error=True)


__all__ = ["ApiCal", "ApiCalendar", "GOOGLE", "GRAPH", "MODES", "SOURCE"]
