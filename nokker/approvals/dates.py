"""Contrôle déterministe des dates et heures d'une carte (relecteur automatique, docs/APPROVALS.md §8).

Avant tout appel au modèle relecteur : chaque date et chaque heure présentes dans les ARGUMENTS de la carte doivent se
retrouver dans la DEMANDE de l'utilisateur, une fois les deux normalisées (`AAAA-MM-JJ`, `HH:MM`). Les dates relatives
de la demande (« aujourd'hui », « demain », « après-demain », « jeudi », « le 24 », « 24 octobre ») sont résolues dans le
fuseau du compte, à l'instant de la carte. Un jour de semaine qui est AUJOURD'HUI (« vendredi » un vendredi) est
ambigu (le jour même ou dans 7 jours) : il ne résout rien, la carte va à l'humain. Une date ou une heure de la carte
absente de la demande → escalade, sans appel au modèle.

Rien ici n'est vu par le modèle.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

MONTHS = {"janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7, "aout": 8,
          "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
          "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
          "september": 9, "october": 10, "november": 11, "december": 12}
WEEKDAYS = {"lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4, "samedi": 5, "dimanche": 6,
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{1,2}):(\d{2}))?")
_DMY = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4}|\d{2})\b")
_DM = re.compile(r"\b(\d{1,2})[./](\d{1,2})\b(?![./]\d)")
_TIME = re.compile(r"(?<![\d.:/-])(\d{1,2})\s?(?:h|:)\s?(\d{2})?(?![\d])", re.IGNORECASE)
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:er)?\s+(" + "|".join(MONTHS) + r")\b(?:\s+(\d{4}))?")
_DAY_ALONE = re.compile(r"\b(?:le|du|au|ce)\s+(\d{1,2})(?:er)?\b(?!\s*(?:h|:|[./]|%|\d))")


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _year(y: str) -> int:
    n = int(y)
    return n + 2000 if n < 100 else n


def _times(text: str) -> set[str]:
    out = set()
    for h, m in _TIME.findall(text):
        if 0 <= int(h) <= 23 and (not m or 0 <= int(m) <= 59):
            out.add(f"{int(h):02d}:{int(m or 0):02d}")
    return out


# Fin d'un créneau : son heure découle du début et d'une durée, la demande ne la nomme presque jamais. Son JOUR reste
# contrôlé ; son heure, non.
END_KEYS = frozenset({"end", "fin", "end_time", "ends_at", "end_at", "until", "to"})
_TZ_SUFFIX = re.compile(r"(\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)(?:Z|[+-]\d{2}:?\d{2})\b")


def card_values(arguments: Any) -> tuple[set[str], set[str]]:
    """(dates, heures) écrites dans les arguments de la carte (formes absolues seulement)."""
    texts: list[tuple[str, bool]] = []

    def walk(v: Any, end: bool = False) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, end or str(k).lower() in END_KEYS)
        elif isinstance(v, list):
            for x in v:
                walk(x, end)
        elif isinstance(v, str):
            texts.append((v, end))

    walk(arguments)
    dates, times = set(), set()
    for raw, end in texts:
        t = _TZ_SUFFIX.sub(r"\1", raw)  # décalage de fuseau (« +02:00 », « Z ») : pas une heure
        found: set[str] = set()
        rest = t
        for y, mo, d, hh, mm in _ISO.findall(t):
            dt = _date(int(y), int(mo), int(d))
            if dt:
                dates.add(dt.isoformat())
            if hh:
                found.add(f"{int(hh):02d}:{mm}")
        rest = _ISO.sub(" ", rest)
        for d, mo, y in _DMY.findall(rest):
            dt = _date(_year(y), int(mo), int(d))
            if dt:
                dates.add(dt.isoformat())
        rest = _DMY.sub(" ", rest)
        if len(rest) <= 80:  # une heure seule (« 15:00 ») dans un champ court ; jamais dans un texte libre
            found |= {x for x in _times(rest) if ":" in rest or "h" in rest.lower()}
        if not end:
            times |= found
    return dates, times


def request_values(text: str, now: datetime) -> tuple[set[str], set[str]]:
    """(dates, heures) que la demande nomme, relatives résolues à `now` (déjà dans le fuseau du compte)."""
    t = _fold(text or "")
    today = now.date()
    dates: set[str] = set()
    for y, mo, d, hh, mm in _ISO.findall(t):
        dt = _date(int(y), int(mo), int(d))
        if dt:
            dates.add(dt.isoformat())
    for d, mo, y in _DMY.findall(t):
        dt = _date(_year(y), int(mo), int(d))
        if dt:
            dates.add(dt.isoformat())
    for d, mo in _DM.findall(_DMY.sub(" ", t)):
        dt = _next_dm(today, int(d), int(mo))
        if dt:
            dates.add(dt.isoformat())
    for d, name, y in _DAY_MONTH.findall(t):
        dt = _date(int(y), MONTHS[name], int(d)) if y else _next_dm(today, int(d), MONTHS[name])
        if dt:
            dates.add(dt.isoformat())
    for d in _DAY_ALONE.findall(t):
        dt = _next_day(today, int(d))
        if dt:
            dates.add(dt.isoformat())
    if re.search(r"\baujourd'?hui\b|\btoday\b|\bce soir\b|\bce matin\b|\bcet apres-midi\b", t):
        dates.add(today.isoformat())
    if re.search(r"\bapres-demain\b", t):
        dates.add((today + timedelta(days=2)).isoformat())
    if re.search(r"(?<!apres-)\bdemain\b|\btomorrow\b", t):
        dates.add((today + timedelta(days=1)).isoformat())
    for name, wd in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", t):
            delta = (wd - today.weekday()) % 7
            if delta == 0 and re.search(rf"\b{name}\s+prochain|\bnext\s+{name}", t):
                delta = 7
            if delta:  # le jour même sans « prochain » : ambigu (aujourd'hui ou dans 7 jours), rien n'est résolu
                dates.add((today + timedelta(days=delta)).isoformat())
    return dates, _times(_DMY.sub(" ", _ISO.sub(" ", t)))


def _next_dm(today: date, d: int, m: int) -> date | None:
    for y in (today.year, today.year + 1):
        dt = _date(y, m, d)
        if dt and dt >= today:
            return dt
    return None


def _next_day(today: date, d: int) -> date | None:
    """« le 24 » : ce mois-ci s'il n'est pas passé, sinon le mois suivant."""
    y, m = today.year, today.month
    for _ in range(2):
        dt = _date(y, m, d)
        if dt and dt >= today:
            return dt
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return None


def unmatched(request: str, arguments: Any, now: datetime) -> list[str]:
    """Dates (`AAAA-MM-JJ`) et heures (`HH:MM`) de la carte que la demande ne nomme pas ; vide = rien à redire."""
    cd, ct = card_values(arguments)
    if not cd and not ct:
        return []
    rd, rt = request_values(request, now)
    return sorted(cd - rd) + sorted(ct - rt)


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def unmatched_addresses(request: str, arguments: Any, own: Iterable[str] = ()) -> list[str]:
    """Adresses de courriel des arguments (un destinataire, un agenda, un invité) que ni la demande ni les adresses du
    compte (`own`) ne nomment : la carte agirait pour quelqu'un que l'utilisateur n'a pas désigné."""
    known = {a.lower() for a in _EMAIL.findall(request or "")} | {a.lower() for a in own}
    found = {a.lower() for a in _EMAIL.findall(json.dumps(arguments, ensure_ascii=False))}
    return sorted(found - known)


__all__ = ["card_values", "request_values", "unmatched", "unmatched_addresses"]
