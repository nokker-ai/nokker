"""iCalendar des lignes du calendrier familial : un VCALENDAR par événement (CalDAV, `put_new_event`) ou un fichier
pour toutes les lignes (téléchargement quand le compte n'a pas d'agenda CalDAV relié).

Heures murales du document dans le fuseau du foyer (Europe/Zurich par défaut) : `DTSTART;TZID=…` et un VTIMEZONE
(règle européenne : dernier dimanche de mars et d'octobre), pour qu'une série hebdomadaire garde son heure au passage
à l'heure d'hiver. Journée entière : `VALUE=DATE`, fin exclusive. Fin absente : une heure. `UNTIL` d'une série à
l'heure : fin du dernier jour en UTC (RFC 5545 §3.3.10). Aucun ATTENDEE : aucune invitation.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from nokker.connectors.calendar import escape_text, fold
from nokker.household.famcal.review import FREQS, Line

PRODID = "-//Nokker//Calendrier familial//FR"


def supported_tz(name: str) -> bool:
    """Fuseaux acceptés : `Europe/*` (règle d'été européenne) et UTC."""
    try:
        ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return False
    return name == "UTC" or name.startswith("Europe/")


def _offset(td: timedelta | None) -> str:
    s = int((td or timedelta()).total_seconds())
    sign = "+" if s >= 0 else "-"
    s = abs(s)
    return f"{sign}{s // 3600:02d}{s % 3600 // 60:02d}"


def vtimezone(tzname: str, year: int) -> list[str]:
    tz = ZoneInfo(tzname)
    winter, summer = datetime(year, 1, 15, 12, tzinfo=tz), datetime(year, 7, 15, 12, tzinfo=tz)
    std, dst = winter.utcoffset(), summer.utcoffset()
    out = ["BEGIN:VTIMEZONE", f"TZID:{tzname}"]
    if std == dst:
        return out + ["BEGIN:STANDARD", "DTSTART:19700101T000000", f"TZOFFSETFROM:{_offset(std)}",
                      f"TZOFFSETTO:{_offset(std)}", f"TZNAME:{winter.tzname()}", "END:STANDARD", "END:VTIMEZONE"]
    return out + [
        "BEGIN:DAYLIGHT", f"TZOFFSETFROM:{_offset(std)}", f"TZOFFSETTO:{_offset(dst)}", f"TZNAME:{summer.tzname()}",
        "DTSTART:19700329T020000", "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", "END:DAYLIGHT",
        "BEGIN:STANDARD", f"TZOFFSETFROM:{_offset(dst)}", f"TZOFFSETTO:{_offset(std)}", f"TZNAME:{winter.tzname()}",
        "DTSTART:19701025T030000", "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", "END:STANDARD", "END:VTIMEZONE"]


def span(line: Line, tz: ZoneInfo) -> tuple[date | datetime, date | datetime]:
    """(début, fin) : dates (journée entière, fin exclusive) ou datetimes dans le fuseau du foyer."""
    d = date.fromisoformat(line.date)
    if not line.start:
        return d, d + timedelta(days=1)
    h, m = (int(x) for x in line.start.split(":"))
    start = datetime.combine(d, time(h, m), tz)
    if line.end:
        h2, m2 = (int(x) for x in line.end.split(":"))
        return start, datetime.combine(d, time(h2, m2), tz)
    return start, start + timedelta(hours=1)


def _dt(name: str, v: date | datetime, tzname: str) -> str:
    if isinstance(v, datetime):
        return f"{name};TZID={tzname}:{v:%Y%m%dT%H%M%S}"
    return f"{name};VALUE=DATE:{v:%Y%m%d}"


def vevent(line: Line, *, tzname: str, description: str, uid: str, now: datetime) -> list[str]:
    tz = ZoneInfo(tzname)
    start, end = span(line, tz)
    out = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{now.astimezone(UTC):%Y%m%dT%H%M%SZ}",
           _dt("DTSTART", start, tzname), _dt("DTEND", end, tzname), f"SUMMARY:{escape_text(line.summary())}"]
    if line.location:
        out.append(f"LOCATION:{escape_text(line.location)}")
    out.append(f"DESCRIPTION:{escape_text(description)}")
    rec = line.recurrence
    if rec:
        last = date.fromisoformat(rec["until"])
        if isinstance(start, datetime):
            until = datetime.combine(last, time(23, 59, 59), tz).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        else:
            until = f"{last:%Y%m%d}"
        rule = f"RRULE:FREQ={FREQS[rec['freq']]}"
        if int(rec.get("interval") or 1) > 1:
            rule += f";INTERVAL={int(rec['interval'])}"
        out.append(rule + f";UNTIL={until}")
        for x in rec.get("except") or []:
            xd = date.fromisoformat(x)
            v = datetime.combine(xd, start.timetz().replace(tzinfo=None), tz) if isinstance(start, datetime) else xd
            out.append(_dt("EXDATE", v, tzname))
    return out + ["END:VEVENT"]


def build(lines: list[Line], *, tzname: str, description: str, now: datetime | None = None,
          uids: list[str] | None = None) -> str:
    now = now or datetime.now(UTC)
    years = {date.fromisoformat(x.date).year for x in lines} or {now.year}
    body = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN",
            *vtimezone(tzname, min(years))]
    for i, line in enumerate(lines):
        uid = uids[i] if uids else new_uid()
        body += vevent(line, tzname=tzname, description=description, uid=uid, now=now)
    body.append("END:VCALENDAR")
    return "\r\n".join(fold(x) for x in body) + "\r\n"


def new_uid() -> str:
    return f"fc-{uuid.uuid4().hex}"


__all__ = ["build", "new_uid", "span", "supported_tz", "vevent", "vtimezone"]
