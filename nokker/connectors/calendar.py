"""Connecteur agenda du compte d'un testeur : iCal (lecture seule, URL secrète) ou CalDAV (lecture + écriture).

Réglages (en clair) / secret :
- `{"mode": "ical", "label": "<hôte affiché>", "tz"?}` / secret = l'URL iCal complète (l'URL EST le secret) ;
- `{"mode": "caldav", "url": "https://…", "username": "…", "tz"?}` / secret = mot de passe d'application.

`event_id` = l'UID iCal de l'événement (stable d'un client à l'autre ; pour une série récurrente, c'est l'UID de la série).
Pour retrouver un événement par UID : GET `<calendrier>/<uid>.ics` (les événements créés ici y sont rangés), sinon REPORT
calendar-query avec filtre UID. Entrées et sorties de journée entière : `end` = dernier jour INCLUS.

Réseau : chaque hôte (y compris ceux d'une redirection ou d'un href) passe par `client_factory` = `net.https_client`
(SSRF, ports, IP épinglée). L'en-tête Authorization n'est envoyé qu'à l'hôte configuré et à ses frères du même domaine
enregistrable (iCloud répond avec pNN-caldav.icloud.com), jamais ailleurs, jamais en mode iCal.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import re
import ssl
import uuid
import xml.etree.ElementTree as ET
from base64 import b64encode
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import quote, urljoin, urlsplit
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from nokker.connectors.errors import ConnectorError, scrub
from nokker.connectors.net import https_client
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult

DEFAULT_TZ = "Europe/Zurich"
MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_EVENTS = 50
MAX_ITER = 3000
SOURCE = "connector:calendar"

DAV = "{DAV:}"
CALDAV = "{urn:ietf:params:xml:ns:caldav}"
_EMAIL_RE = re.compile(r"^[^@\s<>\",;:]+@[^@\s<>\",;:]+\.[A-Za-z]{2,}$")
_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_MULTI_SUFFIX = {"co", "com", "org", "net", "gov", "ac", "edu"}


# ───────────────────────────────── parseur iCal ─────────────────────────────────


def unfold(text: str) -> list[str]:
    out: list[str] = []
    for raw in re.split(r"\r\n|\n|\r", text):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        elif raw:
            out.append(raw)
    return out


def split_line(line: str) -> tuple[str, dict[str, str], str]:
    """`NAME;P=V;P2="x:y":valeur` → (NAME, {P: V}, valeur). Les `;` et `:` entre guillemets ne coupent pas."""
    in_q, cut = False, -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif not in_q and ch == ":":
            cut = i
            break
    if cut < 0:
        raise ValueError("ligne sans ':'")
    head, value = line[:cut], line[cut + 1:]
    parts, buf, in_q = [], [], False
    for ch in head:
        if ch == '"':
            in_q = not in_q
            buf.append(ch)
        elif ch == ";" and not in_q:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def unescape(v: str) -> str:
    out, i = [], 0
    while i < len(v):
        c = v[i]
        if c == "\\" and i + 1 < len(v):
            n = v[i + 1]
            out.append("\n" if n in "nN" else n)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def escape_text(v: str) -> str:
    v = "".join(ch for ch in v.replace("\r\n", "\n").replace("\r", "\n") if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return v.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> str:
    """Pliage RFC 5545 à 75 octets UTF-8, sans couper un caractère."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode())
        if size + n > (75 if not out else 74):
            out.append(cur)
            cur, size = "", 0
        cur += ch
        size += n
    out.append(cur)
    return "\r\n ".join(out)


@dataclass
class Event:
    uid: str
    summary: str = ""
    location: str = ""
    description: str = ""
    start: date | datetime | None = None
    end: date | datetime | None = None
    all_day: bool = False
    attendees: list[str] = field(default_factory=list)
    rrule: dict[str, str] | None = None
    unsupported_rrule: bool = False
    exdates: set[tuple[str, str]] = field(default_factory=set)
    recurrence_id: tuple[str, str] | None = None
    cancelled: bool = False
    notes: list[str] = field(default_factory=list)


def _tz(name: str | None, default: ZoneInfo, notes: list[str]) -> ZoneInfo:
    if not name:
        return default
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        notes.append(f"fuseau {name} inconnu, fuseau du compte utilisé")
        return default


def _parse_dt(value: str, params: dict[str, str], default: ZoneInfo, notes: list[str]) -> date | datetime:
    v = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or len(v) == 8:
        return date(int(v[0:4]), int(v[4:6]), int(v[6:8]))
    utc = v.endswith(("Z", "z"))
    d = datetime.strptime(v.rstrip("Zz"), "%Y%m%dT%H%M%S")
    return d.replace(tzinfo=UTC if utc else _tz(params.get("TZID"), default, notes))


_DUR_RE = re.compile(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def _parse_duration(v: str) -> timedelta:
    m = _DUR_RE.match(v.strip())
    if not m:
        raise ValueError("durée illisible")
    sign, w, d, h, mi, s = m.groups()
    td = timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0), seconds=int(s or 0))
    return -td if sign == "-" else td


def _key(v: date | datetime) -> tuple[str, str]:
    if isinstance(v, datetime):
        return "t", v.astimezone(UTC).strftime("%Y%m%dT%H%M%S")
    return "d", v.isoformat()


def _parse_rrule(value: str) -> tuple[dict[str, str], bool]:
    parts = {}
    for item in value.split(";"):
        k, _, v = item.partition("=")
        parts[k.upper()] = v
    freq = parts.get("FREQ", "")
    ok = freq in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}
    ok = ok and all(k in {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST"} for k in parts)
    if "BYDAY" in parts:
        ok = ok and freq == "WEEKLY" and all(d in _DAYS for d in parts["BYDAY"].split(","))
    for k in ("INTERVAL", "COUNT"):
        if k in parts and not parts[k].isdigit():
            ok = False
    if parts.get("INTERVAL") == "0" or parts.get("COUNT") == "0":
        ok = False
    if "COUNT" in parts and "UNTIL" in parts:
        ok = False
    return parts, not ok


@dataclass
class Parsed:
    events: list[Event]
    warnings: list[str]


def parse_ics(text: str, tz: ZoneInfo) -> Parsed:
    """Extrait les VEVENT. Un événement illisible est écarté et signalé, jamais d'exception pour un contenu mal formé."""
    if "BEGIN:VCALENDAR" not in text.upper():
        raise ConnectorError("protocol_error", "le contenu n'est pas un calendrier iCal")
    blocks: list[list[tuple[str, dict[str, str], str]]] = []
    stack: list[str] = []
    cur: list[tuple[str, dict[str, str], str]] | None = None
    for line in unfold(text):
        try:
            name, params, value = split_line(line)
        except ValueError:
            continue
        if name == "BEGIN":
            stack.append(value.upper())
            if value.upper() == "VEVENT" and len(stack) == 2:
                cur = []
        elif name == "END":
            if stack and stack[-1] == value.upper():
                stack.pop()
            if value.upper() == "VEVENT" and cur is not None and len(stack) == 1:
                blocks.append(cur)
                cur = None
        elif cur is not None and stack[-1] == "VEVENT":
            cur.append((name, params, value))
    events, warnings = [], []
    for props in blocks:
        try:
            ev = _event(props, tz)
        except (ValueError, KeyError, OverflowError):
            warnings.append("un événement illisible a été ignoré")
            continue
        if ev is not None:
            events.append(ev)
    return Parsed(events, warnings)


def _event(props: list[tuple[str, dict[str, str], str]], tz: ZoneInfo) -> Event | None:
    notes: list[str] = []
    ev = Event(uid="")
    duration: timedelta | None = None
    for name, params, value in props:
        if name == "UID":
            ev.uid = value.strip()
        elif name == "SUMMARY":
            ev.summary = unescape(value)
        elif name == "LOCATION":
            ev.location = unescape(value)
        elif name == "DESCRIPTION":
            ev.description = unescape(value)
        elif name == "STATUS" and value.strip().upper() == "CANCELLED":
            ev.cancelled = True
        elif name == "DTSTART":
            ev.start = _parse_dt(value, params, tz, notes)
        elif name == "DTEND":
            ev.end = _parse_dt(value, params, tz, notes)
        elif name == "DURATION":
            duration = _parse_duration(value)
        elif name == "ATTENDEE":
            ev.attendees.append(re.sub(r"^mailto:", "", value.strip(), flags=re.I))
        elif name == "RRULE":
            ev.rrule, ev.unsupported_rrule = _parse_rrule(value)
        elif name == "EXDATE":
            for item in value.split(","):
                if item.strip():
                    ev.exdates.add(_key(_parse_dt(item, params, tz, notes)))
        elif name == "RECURRENCE-ID":
            ev.recurrence_id = _key(_parse_dt(value, params, tz, notes))
    if ev.start is None:
        return None
    ev.all_day = not isinstance(ev.start, datetime)
    if ev.end is None:
        if duration is not None:
            ev.end = ev.start + duration
        else:
            ev.end = ev.start + timedelta(days=1) if ev.all_day else ev.start
    if isinstance(ev.end, datetime) != isinstance(ev.start, datetime) or ev.end < ev.start:  # type: ignore[operator]
        ev.end = ev.start + timedelta(days=1) if ev.all_day else ev.start
    ev.uid = ev.uid or "anon-" + hashlib.sha1(f"{ev.summary}{_key(ev.start)}".encode()).hexdigest()[:16]
    ev.notes = notes
    if ev.unsupported_rrule:
        ev.notes.append("récurrence non gérée : seule la première occurrence est affichée")
    return ev


def _as_dt(v: date | datetime, tz: ZoneInfo) -> datetime:
    return v if isinstance(v, datetime) else datetime.combine(v, time(), tz)


def _until(v: str, tz: ZoneInfo) -> datetime:
    if len(v) == 8:
        return datetime.combine(date(int(v[:4]), int(v[4:6]), int(v[6:8])), time(23, 59, 59), tz)
    d = datetime.strptime(v.rstrip("Zz"), "%Y%m%dT%H%M%S")
    return d.replace(tzinfo=UTC if v.endswith(("Z", "z")) else tz)


def _candidates(ev: Event):
    """Débuts successifs d'une série (heure murale conservée d'une occurrence à l'autre), sans filtrer ni compter."""
    s = ev.start
    assert s is not None and ev.rrule is not None
    freq, interval = ev.rrule["FREQ"], int(ev.rrule.get("INTERVAL", "1"))
    d0 = s.date() if isinstance(s, datetime) else s

    def at(d: date) -> date | datetime:
        return d if not isinstance(s, datetime) else datetime.combine(d, s.time()).replace(tzinfo=s.tzinfo)

    if freq == "DAILY":
        for i in itertools.count():
            yield at(d0 + timedelta(days=i * interval))
    elif freq == "WEEKLY":
        days = sorted(_DAYS[d] for d in ev.rrule["BYDAY"].split(",")) if "BYDAY" in ev.rrule else [d0.weekday()]
        monday = d0 - timedelta(days=d0.weekday())
        for i in itertools.count():
            for wd in days:
                d = monday + timedelta(weeks=i * interval, days=wd)
                if d >= d0:
                    yield at(d)
    elif freq == "MONTHLY":
        for i in itertools.count():
            y, m = divmod(d0.year * 12 + d0.month - 1 + i * interval, 12)
            try:
                yield at(date(y, m + 1, d0.day))
            except ValueError:
                continue
    else:
        for i in itertools.count():
            try:
                yield at(date(d0.year + i * interval, d0.month, d0.day))
            except ValueError:
                continue


def _occurrences(ev: Event, t1: datetime, tz: ZoneInfo):
    assert ev.start is not None
    if ev.rrule is None or ev.unsupported_rrule:
        yield ev.start
        return
    count = int(ev.rrule["COUNT"]) if "COUNT" in ev.rrule else None
    until = _until(ev.rrule["UNTIL"], tz) if "UNTIL" in ev.rrule else None
    for n, occ in enumerate(itertools.islice(_candidates(ev), MAX_ITER), 1):
        at = _as_dt(occ, tz)
        if (count and n > count) or (until and at > until) or at >= t1:
            return
        yield occ


def events_between(events: list[Event], t0: datetime, t1: datetime, tz: ZoneInfo) -> list[dict[str, Any]]:
    """Occurrences qui chevauchent [t0, t1[, triées, au format de sortie des outils."""
    overridden = {(e.uid, e.recurrence_id) for e in events if e.recurrence_id}
    found: list[tuple[datetime, dict[str, Any]]] = []
    for ev in events:
        if ev.cancelled or ev.start is None or ev.end is None:
            continue
        dur = ev.end - ev.start
        try:
            for occ in _occurrences(ev, t1, tz):
                if ev.recurrence_id is None and (ev.uid, _key(occ)) in overridden:
                    continue
                if ev.recurrence_id is None and _key(occ) in ev.exdates:
                    continue
                start, end = _as_dt(occ, tz), _as_dt(occ + dur, tz)
                if (start < t1 and end > t0) or (start == end and t0 <= start < t1):
                    found.append((start, _render(ev, occ, dur, tz)))
        except (OverflowError, ValueError):
            continue
    found.sort(key=lambda t: (t[0], t[1]["summary"]))
    return [d for _, d in found]


def _render(ev: Event, occ: date | datetime, dur: timedelta, tz: ZoneInfo) -> dict[str, Any]:
    if ev.all_day:
        s = occ if isinstance(occ, date) and not isinstance(occ, datetime) else occ.date()
        last = max(s, s + dur - timedelta(days=1))
        start, end = s.isoformat(), last.isoformat()
    else:
        assert isinstance(occ, datetime)
        start, end = occ.astimezone(tz).isoformat(timespec="minutes"), (occ + dur).astimezone(tz).isoformat(timespec="minutes")
    out: dict[str, Any] = {"id": ev.uid, "summary": ev.summary, "start": start, "end": end, "location": ev.location,
                           "description": ev.description[:2000], "attendees": ev.attendees, "all_day": ev.all_day}
    if ev.notes:
        out["note"] = "; ".join(dict.fromkeys(ev.notes))
    return out


# ───────────────────────────────── écriture iCal ─────────────────────────────────


def parse_input_time(value: str, tz: ZoneInfo) -> tuple[date | datetime, bool]:
    """ISO 8601 (« 2026-10-27T14:00 ») → datetime avec fuseau (celui du compte si absent) ; « 2026-10-27 » = journée entière."""
    s = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return date.fromisoformat(s), True
        d = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"date/heure invalide : {s!r} — attendu ISO 8601, ex. 2026-10-27T14:00 (ou 2026-10-27 pour une journée entière)") from None
    return (d if d.tzinfo else d.replace(tzinfo=tz)), False


def _ics_time(name: str, v: date | datetime) -> str:
    if isinstance(v, datetime):
        return f"{name}:{v.astimezone(UTC):%Y%m%dT%H%M%SZ}"
    return f"{name};VALUE=DATE:{v:%Y%m%d}"


def _attendee_lines(attendees: list[str]) -> list[str]:
    lines = []
    for a in attendees:
        a = str(a).strip()
        if not _EMAIL_RE.match(a):
            raise ValueError(f"adresse d'invité invalide : {a!r}")
        lines.append(f"ATTENDEE:mailto:{a}")
    return lines


def _event_lines(uid: str, summary: str, start: date | datetime, end: date | datetime, description: str | None,
                 location: str | None, attendees: list[str], now: datetime) -> list[str]:
    lines = [f"UID:{uid}", f"DTSTAMP:{now.astimezone(UTC):%Y%m%dT%H%M%SZ}", _ics_time("DTSTART", start),
             _ics_time("DTEND", end), f"SUMMARY:{escape_text(summary)}"]
    if description:
        lines.append(f"DESCRIPTION:{escape_text(description)}")
    if location:
        lines.append(f"LOCATION:{escape_text(location)}")
    return lines + _attendee_lines(attendees)


def build_ics(event_lines: list[str]) -> str:
    body = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Nokker//Connecteur agenda//FR", "CALSCALE:GREGORIAN",
            "BEGIN:VEVENT", *event_lines, "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(fold(line) for line in body) + "\r\n"


def _same_name(line: str, names: set[str]) -> bool:
    return re.split(r"[;:]", line, maxsplit=1)[0].upper() in names


def edit_ics(text: str, drop: set[str], add: list[str], sequence_bump: bool = True) -> str:
    """Retire/ajoute des propriétés dans le VEVENT maître (celui sans RECURRENCE-ID), le reste est conservé tel quel."""
    lines = unfold(text)
    start = end = -1
    depth = 0
    for i, line in enumerate(lines):
        u = line.upper()
        if u == "BEGIN:VEVENT" and start < 0:
            start, depth = i, 0
        elif start >= 0 and end < 0:
            if u.startswith("BEGIN:"):
                depth += 1
            elif u.startswith("END:"):
                if depth == 0:
                    block = lines[start:i]
                    if any(_same_name(x, {"RECURRENCE-ID"}) for x in block):
                        start = -1
                    else:
                        end = i
                else:
                    depth -= 1
    if start < 0 or end < 0:
        raise ValueError("événement maître introuvable")
    seq = 0
    kept, depth = [], 0
    for line in lines[start + 1:end]:
        u = line.upper()
        if u.startswith("BEGIN:"):
            depth += 1
        elif u.startswith("END:"):
            depth -= 1
        elif depth == 0:
            if _same_name(line, {"SEQUENCE"}):
                try:
                    seq = int(line.split(":", 1)[1])
                except (IndexError, ValueError):
                    seq = 0
                continue
            if _same_name(line, drop | {"DTSTAMP", "LAST-MODIFIED"}):
                continue
        kept.append(line)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    extra = [f"DTSTAMP:{stamp}", f"LAST-MODIFIED:{stamp}"] + ([f"SEQUENCE:{seq + 1}"] if sequence_bump else [])
    new = lines[:start + 1] + kept + add + extra + lines[end:]
    return "\r\n".join(fold(line) for line in new) + "\r\n"


# ───────────────────────────────── réseau ─────────────────────────────────


@dataclass
class Resp:
    status: int
    headers: httpx.Headers
    content: bytes
    url: str

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


@dataclass
class Cal:
    url: str
    name: str


def _registrable(host: str) -> str:
    labels = host.lower().split(".")
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _MULTI_SUFFIX else 2
    return ".".join(labels[-keep:])


ClientFactory = Callable[[str, int], tuple[Any, httpx.Client]]


class CalendarConnector:
    def __init__(self, settings: dict[str, Any], secret: str, *, client_factory: ClientFactory | None = None,
                 now: Callable[[], datetime] | None = None, locality: str | None = None) -> None:
        self._secret = secret
        self.locality = locality  # docs/LOKKAL.md §6.7 : « cloud » chez nokker.ai, « local » dans Lokkal
        self._factory = client_factory or https_client
        self._now = now or (lambda: datetime.now(UTC))
        mode = settings.get("mode")
        if mode not in ("ical", "caldav"):
            raise ConnectorError("bad_settings", "mode agenda inconnu (ical ou caldav)")
        self.mode = mode
        try:
            self.tz = ZoneInfo(settings.get("tz") or DEFAULT_TZ)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ConnectorError("bad_settings", "fuseau horaire inconnu") from None
        if not secret:
            raise ConnectorError("bad_settings", "secret manquant")
        self._calendars: list[Cal] | None = None
        if mode == "ical":
            self._url = self._ical_url(secret)
            self._auth = ""
        else:
            url, user = str(settings.get("url") or ""), str(settings.get("username") or "")
            parts = urlsplit(url)
            if parts.scheme != "https" or not parts.hostname or not user:
                raise ConnectorError("bad_settings", "CalDAV : adresse https et identifiant requis")
            self._url = url
            self._origin_host = parts.hostname.lower()
            self._auth = "Basic " + b64encode(f"{user}:{secret}".encode()).decode()

    @staticmethod
    def _ical_url(secret: str) -> str:
        url = secret.strip()
        if url.lower().startswith("webcal://"):
            url = "https://" + url[9:]
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ConnectorError("bad_settings", "adresse iCal : une URL https (ou webcal) sans identifiant est attendue")
        return url

    # — specs / appels —

    def specs(self) -> list[ToolSpec]:
        cal_param = {"type": "string", "description": "nom de l'agenda ; « primary » (défaut) = le principal"}
        events = ToolSpec(
            name="calendar_events",
            description="Événements des `days` prochains jours de l'agenda du compte (fuseau du compte). Pour une journée "
                        "entière, `end` est le dernier jour inclus.",
            parameters={"type": "object", "properties": {
                "days": {"type": "integer", "description": "nombre de jours à partir de maintenant (1-366, défaut 7)"},
                "calendar": {"type": "string", "description": "nom de l'agenda, « primary » (défaut) ou « all »"}}},
            effect=Effect.READ, source=SOURCE, locality=self.locality)
        if self.mode == "ical":
            return [events]
        when = "ISO 8601, ex. 2026-10-27T14:00 (fuseau du compte) ; 2026-10-27 seul = journée entière (fin incluse)"
        fields = {"summary": {"type": "string"}, "start": {"type": "string", "description": when},
                  "end": {"type": "string", "description": when}, "description": {"type": "string"},
                  "location": {"type": "string"},
                  "attendees": {"type": "array", "items": {"type": "string"},
                                "description": "adresses e-mail ; inscrites dans l'événement, AUCUNE invitation n'est envoyée"},
                  "calendar": cal_param}
        return [
            events,
            ToolSpec(name="calendar_create", description="Crée un événement dans l'agenda du compte. N'envoie aucune invitation.",
                     parameters={"type": "object", "properties": fields, "required": ["summary", "start", "end"]},
                     effect=Effect.WRITE, source=SOURCE, locality=self.locality),
            ToolSpec(name="calendar_update",
                     description="Modifie un événement existant (id renvoyé par calendar_events) ; seuls les champs donnés changent.",
                     parameters={"type": "object", "properties": {"event_id": {"type": "string"}, **fields},
                                 "required": ["event_id"]},
                     effect=Effect.WRITE, source=SOURCE, locality=self.locality),
            ToolSpec(name="calendar_delete", description="Supprime un événement (série entière si récurrent). Action destructive.",
                     parameters={"type": "object", "properties": {"event_id": {"type": "string"}, "calendar": cal_param},
                                 "required": ["event_id"]},
                     effect=Effect.DESTRUCTIVE, source=SOURCE, locality=self.locality),
        ]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        handlers = {"calendar_events": self._events_tool}
        if self.mode == "caldav":
            handlers |= {"calendar_create": self._create, "calendar_update": self._update, "calendar_delete": self._delete}
        fn = handlers.get(name)
        if fn is None:
            return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)
        try:
            return ToolResult(call_id="", name=name, content=json.dumps(fn(arguments or {}), ensure_ascii=False))
        except ConnectorError as e:
            msg = f"{e.code} : {scrub(e.message, self._secret, self._url if self.mode == 'ical' else '')}"
        except ValueError as e:
            msg = scrub(str(e), self._secret)
        except Exception:  # noqa: BLE001 — rien d'interne ni de secret ne sort d'un outil
            msg = "erreur interne du connecteur agenda"
        return ToolResult(call_id="", name=name, content=msg, is_error=True)

    def test(self) -> str:
        try:
            now = self._now()
            if self.mode == "ical":
                n = len(self._window(now, now + timedelta(days=30), None))
                return f"agenda iCal lu : {n} événement(s) dans les 30 prochains jours"
            cals = self._discover()
            n = len(self._window(now, now + timedelta(days=30), cals[:1]))
            return f"{len(cals)} agenda(s) trouvé(s) ; {n} événement(s) dans les 30 prochains jours sur « {cals[0].name} »"
        except ConnectorError as e:
            raise ConnectorError(e.code, scrub(e.message, self._secret, self._url if self.mode == "ical" else "")) from None

    # — HTTP —

    def _auth_ok(self, host: str) -> bool:
        return bool(self._auth) and (host == self._origin_host or _registrable(host) == _registrable(self._origin_host))

    def _req(self, method: str, url: str, *, headers: dict[str, str] | None = None, body: bytes | None = None) -> Resp:
        for hop in range(MAX_REDIRECTS + 1):
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            if parts.scheme != "https" or not host:
                raise ConnectorError("bad_settings", "seules les adresses https sont acceptées")
            h = {"User-Agent": "Nokker-connector/1", **(headers or {})}
            if self._auth_ok(host):
                h["Authorization"] = self._auth
            _hop, client = self._factory(host, parts.port or 443)
            try:
                resp = self._send(client, method, url, h, body)
            finally:
                client.close()
            if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                if hop == MAX_REDIRECTS:
                    raise ConnectorError("protocol_error", "trop de redirections")
                url = urljoin(url, resp.headers["location"])
                if resp.status == 303:
                    method, body = "GET", None
                continue
            return resp
        raise ConnectorError("protocol_error", "trop de redirections")

    def _send(self, client: httpx.Client, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Resp:
        try:
            with client.stream(method, url, headers=headers, content=body) as r:
                chunks, size = [], 0
                for part in r.iter_bytes():
                    size += len(part)
                    if size > MAX_BYTES:
                        raise ConnectorError("protocol_error", "réponse trop volumineuse (5 Mo maximum)")
                    chunks.append(part)
                return Resp(r.status_code, r.headers, b"".join(chunks), url)
        except ConnectorError:
            raise
        except httpx.ConnectError as e:
            if isinstance(e.__cause__, ssl.SSLError) or "CERTIFICATE" in str(e).upper():
                raise ConnectorError("tls_failed", "certificat TLS refusé par le serveur d'agenda") from None
            raise ConnectorError("unreachable", "serveur d'agenda injoignable") from None
        except httpx.TimeoutException:
            raise ConnectorError("unreachable", "le serveur d'agenda ne répond pas") from None
        except httpx.HTTPError:
            raise ConnectorError("unreachable", "erreur réseau vers le serveur d'agenda") from None

    @staticmethod
    def _check(r: Resp, ok: tuple[int, ...]) -> Resp:
        if r.status in ok:
            return r
        if r.status in (401, 403):
            raise ConnectorError("auth_failed", "identifiants refusés par le serveur d'agenda")
        if r.status == 404:
            raise ConnectorError("protocol_error", "ressource introuvable sur le serveur d'agenda")
        if r.status == 412:
            raise ConnectorError("protocol_error", "événement modifié entre-temps : relire puis réessayer")
        raise ConnectorError("protocol_error", f"réponse inattendue du serveur d'agenda (HTTP {r.status})")

    @staticmethod
    def _xml(r: Resp) -> ET.Element:
        raw = r.content
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise ConnectorError("protocol_error", "réponse XML refusée")
        try:
            return ET.fromstring(raw)
        except ET.ParseError:
            raise ConnectorError("protocol_error", "réponse XML illisible") from None

    def _dav(self, method: str, url: str, body: str, depth: str | None = None) -> ET.Element:
        h = {"Content-Type": "application/xml; charset=utf-8"}
        if depth is not None:
            h["Depth"] = depth
        return self._xml(self._check(self._req(method, url, headers=h, body=body.encode()), (200, 207)))

    # — CalDAV : découverte —

    _PROPFIND = ('<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                 "<d:prop><d:resourcetype/><d:displayname/><d:current-user-principal/><c:calendar-home-set/>"
                 "<c:supported-calendar-component-set/></d:prop></d:propfind>")

    @staticmethod
    def _entries(root: ET.Element, base: str) -> list[dict[str, Any]]:
        out = []
        for resp in root.iter(f"{DAV}response"):
            href = resp.findtext(f"{DAV}href") or ""
            info: dict[str, Any] = {"href": urljoin(base, href.strip()), "types": set(), "comps": set(), "name": "",
                                    "principal": "", "home": ""}
            for ps in resp.findall(f"{DAV}propstat"):
                if " 200 " not in f" {ps.findtext(f'{DAV}status') or '200'} ":
                    continue
                prop = ps.find(f"{DAV}prop")
                if prop is None:
                    continue
                rt = prop.find(f"{DAV}resourcetype")
                info["types"] |= {c.tag for c in rt} if rt is not None else set()
                info["name"] = (prop.findtext(f"{DAV}displayname") or info["name"]).strip()
                info["principal"] = (prop.findtext(f"{DAV}current-user-principal/{DAV}href") or info["principal"]).strip()
                info["home"] = (prop.findtext(f"{CALDAV}calendar-home-set/{DAV}href") or info["home"]).strip()
                info["comps"] |= {c.get("name", "").upper() for c in prop.iter(f"{CALDAV}comp")}
            out.append(info)
        return out

    def _discover(self) -> list[Cal]:
        if self._calendars is not None:
            return self._calendars
        parts = urlsplit(self._url)
        cands = [self._url]
        if parts.path in ("", "/"):
            cands.append(f"https://{parts.netloc}/.well-known/caldav")
        first: dict[str, Any] | None = None
        base = self._url
        for cand in cands:
            try:
                root = self._dav("PROPFIND", cand, self._PROPFIND, "0")
            except ConnectorError as e:
                if e.code == "auth_failed" or cand == cands[-1]:
                    raise
                continue
            entries = self._entries(root, cand)
            if entries and (entries[0]["principal"] or entries[0]["home"] or f"{CALDAV}calendar" in entries[0]["types"]):
                first, base = entries[0], cand
                break
        if first is None:
            raise ConnectorError("protocol_error", "ce serveur ne répond pas comme un serveur CalDAV")
        cals: list[Cal] = []
        if f"{CALDAV}calendar" in first["types"]:
            cals = [Cal(first["href"], first["name"] or "agenda")]
        else:
            home = first["home"]
            if not home and first["principal"]:
                pr = self._entries(self._dav("PROPFIND", urljoin(base, first["principal"]), self._PROPFIND, "0"),
                                   urljoin(base, first["principal"]))
                home = pr[0]["home"] if pr else ""
                base = urljoin(base, first["principal"])
            if not home:
                raise ConnectorError("protocol_error", "aucun espace d'agendas (calendar-home-set) annoncé par le serveur")
            home_url = urljoin(base, home)
            for e in self._entries(self._dav("PROPFIND", home_url, self._PROPFIND, "1"), home_url):
                if f"{CALDAV}calendar" in e["types"] and (not e["comps"] or "VEVENT" in e["comps"]):
                    name = e["name"] or e["href"].rstrip("/").rsplit("/", 1)[-1]
                    cals.append(Cal(e["href"] if e["href"].endswith("/") else e["href"] + "/", name))
        if not cals:
            raise ConnectorError("protocol_error", "aucun agenda d'événements trouvé sur ce compte")
        self._calendars = cals
        return cals

    def _select(self, calendar: str | None, *, default_all: bool = False) -> list[Cal]:
        cals = self._discover()
        c = (calendar or "").strip()
        if c.lower() == "all" or (not c and default_all):
            return cals
        if not c or c.lower() == "primary":
            return cals[:1]
        for cal in cals:
            if c.lower() in (cal.name.lower(), cal.url.lower()):
                return [cal]
        raise ValueError(f"agenda inconnu : {c!r} (disponibles : {', '.join(x.name for x in cals)})")

    # — lecture —

    def _window(self, t0: datetime, t1: datetime, cals: list[Cal] | None) -> list[dict[str, Any]]:
        parsed: list[Event] = []
        if self.mode == "ical":
            r = self._check(self._req("GET", self._url, headers={"Accept": "text/calendar"}), (200,))
            parsed = parse_ics(r.text, self.tz).events
        else:
            body = ('<?xml version="1.0" encoding="utf-8"?><c:calendar-query xmlns:d="DAV:" '
                    'xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><d:getetag/><c:calendar-data/></d:prop><c:filter>'
                    '<c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"><c:time-range '
                    f'start="{t0.astimezone(UTC):%Y%m%dT%H%M%SZ}" end="{t1.astimezone(UTC):%Y%m%dT%H%M%SZ}"/>'
                    "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>")
            for cal in cals or self._select(None):
                root = self._dav("REPORT", cal.url, body, "1")
                for data in root.iter(f"{CALDAV}calendar-data"):
                    if data.text and "BEGIN:VEVENT" in data.text.upper():
                        parsed += parse_ics(data.text, self.tz).events
        return events_between(parsed, t0, t1, self.tz)

    def _events_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            days = int(args.get("days", 7))
        except (TypeError, ValueError):
            raise ValueError("`days` doit être un entier") from None
        if not 1 <= days <= 366:
            raise ValueError("`days` doit être compris entre 1 et 366")
        now = self._now()
        cals = self._select(args.get("calendar")) if self.mode == "caldav" else None
        evs = self._window(now, now + timedelta(days=days), cals)
        out: dict[str, Any] = {"count": min(len(evs), MAX_EVENTS), "events": evs[:MAX_EVENTS]}
        if len(evs) > MAX_EVENTS:
            out["truncated"] = len(evs)
        return out

    # — écriture (CalDAV seulement) —

    def _find(self, uid: str, calendar: str | None) -> tuple[Cal, str, str, str]:
        """(agenda, href, etag, texte iCal) de l'événement d'UID `uid`."""
        for cal in self._select(calendar, default_all=True):
            href = urljoin(cal.url, quote(uid, safe="") + ".ics")
            r = self._req("GET", href)
            if r.status == 200 and any(e.uid == uid for e in parse_ics(r.text, self.tz).events):
                return cal, r.url, r.headers.get("etag", ""), r.text
            if r.status in (401, 403):
                self._check(r, ())
            body = ('<?xml version="1.0" encoding="utf-8"?><c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                    "<d:prop><d:getetag/><c:calendar-data/></d:prop><c:filter><c:comp-filter name=\"VCALENDAR\">"
                    '<c:comp-filter name="VEVENT"><c:prop-filter name="UID"><c:text-match collation="i;octet">'
                    f"{xml_escape(uid)}</c:text-match></c:prop-filter></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>")
            root = self._dav("REPORT", cal.url, body, "1")
            for resp in root.iter(f"{DAV}response"):
                data = resp.findtext(f".//{CALDAV}calendar-data") or ""
                if "BEGIN:VCALENDAR" in data.upper() and any(e.uid == uid for e in parse_ics(data, self.tz).events):
                    etag = (resp.findtext(f".//{DAV}getetag") or "").strip()
                    return cal, urljoin(cal.url, (resp.findtext(f"{DAV}href") or "").strip()), etag, data
        raise ValueError("événement introuvable (id inconnu de cet agenda)")

    def _put(self, url: str, ics: str, *, etag: str = "", create: bool = False) -> Resp:
        h = {"Content-Type": "text/calendar; charset=utf-8"}
        if create:
            h["If-None-Match"] = "*"
        elif etag:
            h["If-Match"] = etag
        return self._check(self._req("PUT", url, headers=h, body=ics.encode()), (200, 201, 204))

    def _create(self, a: dict[str, Any]) -> dict[str, Any]:
        summary = str(a.get("summary") or "").strip()
        if not summary:
            raise ValueError("`summary` requis")
        start, all_day = parse_input_time(a.get("start") or "", self.tz)
        end, end_all = parse_input_time(a.get("end") or "", self.tz)
        if all_day != end_all:
            raise ValueError("`start` et `end` doivent être tous deux des dates ou tous deux des heures")
        if all_day:
            end = end + timedelta(days=1)  # type: ignore[operator]
        if end <= start:  # type: ignore[operator]
            raise ValueError("`end` doit être postérieur à `start`")
        attendees = [str(x) for x in (a.get("attendees") or [])]
        uid = uuid.uuid4().hex
        lines = _event_lines(uid, summary, start, end, a.get("description"), a.get("location"), attendees, self._now())
        cal = self._select(a.get("calendar"))[0]
        self._put(urljoin(cal.url, uid + ".ics"), build_ics(lines), create=True)
        out: dict[str, Any] = {"created": True, "id": uid, "calendar": cal.name}
        if attendees:
            out["note"] = "invités inscrits dans l'événement ; aucune invitation envoyée"
        return out

    def _update(self, a: dict[str, Any]) -> dict[str, Any]:
        uid = str(a.get("event_id") or "").strip()
        if not uid:
            raise ValueError("`event_id` requis")
        cal, href, etag, text = self._find(uid, a.get("calendar"))
        master = next((e for e in parse_ics(text, self.tz).events if e.uid == uid and e.recurrence_id is None), None)
        if master is None or master.start is None or master.end is None:
            raise ValueError("événement illisible")
        drop: set[str] = set()
        add: list[str] = []
        if "start" in a or "end" in a:
            if master.rrule:
                raise ValueError("série récurrente : la modification des horaires n'est pas prise en charge ici")
            s_in = parse_input_time(a["start"], self.tz) if a.get("start") else None
            e_in = parse_input_time(a["end"], self.tz) if a.get("end") else None
            all_day = (s_in or e_in)[1]  # type: ignore[index]
            if (s_in and s_in[1] != all_day) or (e_in and e_in[1] != all_day):
                raise ValueError("`start` et `end` doivent être tous deux des dates ou tous deux des heures")
            if master.all_day != all_day and not (s_in and e_in):
                raise ValueError("changer entre journée entière et heures impose de donner `start` et `end`")
            dur = master.end - master.start
            start = s_in[0] if s_in else master.start
            if e_in:
                end = e_in[0] + timedelta(days=1) if all_day else e_in[0]
            else:
                end = start + dur
            if end <= start:  # type: ignore[operator]
                raise ValueError("`end` doit être postérieur à `start`")
            drop |= {"DTSTART", "DTEND", "DURATION"}
            add += [_ics_time("DTSTART", start), _ics_time("DTEND", end)]
        for key, prop in (("summary", "SUMMARY"), ("description", "DESCRIPTION"), ("location", "LOCATION")):
            if key in a:
                drop.add(prop)
                if a[key]:
                    add.append(f"{prop}:{escape_text(str(a[key]))}")
        if "attendees" in a:
            drop.add("ATTENDEE")
            add += _attendee_lines([str(x) for x in (a["attendees"] or [])])
        if not drop:
            raise ValueError("rien à modifier : donner au moins un champ")
        self._put(href, edit_ics(text, drop, add), etag=etag)
        out: dict[str, Any] = {"updated": True, "id": uid}
        if master.rrule:
            out["note"] = "événement récurrent : la modification vaut pour toute la série"
        if "attendees" in a:
            out["note"] = (out.get("note", "") + "; " if out.get("note") else "") + "aucune invitation envoyée"
        return out

    def _delete(self, a: dict[str, Any]) -> dict[str, Any]:
        uid = str(a.get("event_id") or "").strip()
        if not uid:
            raise ValueError("`event_id` requis")
        _cal, href, etag, _text = self._find(uid, a.get("calendar"))
        h = {"If-Match": etag} if etag else {}
        self._check(self._req("DELETE", href, headers=h), (200, 202, 204))
        return {"deleted": True, "id": uid}
