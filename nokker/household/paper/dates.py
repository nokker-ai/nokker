"""Relecture des événements extraits d'un programme (école, club) AVANT toute écriture dans l'agenda.

Le modèle lit le document et propose des événements ; chacun cite le texte SOURCE de sa date (`source`) et, si
l'année n'y est pas, celui qui la donne (`year_source`). Le noyau vérifie, sans modèle :

- la source contient un jour ET un mois explicites (« tous les mardis », « semaine prochaine » : à préciser) ;
- la date proposée est l'une de celles écrites dans la source ;
- l'année est écrite dans la source, ou dans `year_source` (année seule, ou année scolaire « 2026-2027 » : août à
  décembre = première année, janvier à juillet = seconde), ou, à défaut, le texte du document n'en porte qu'une ;
- le format J/M avec deux nombres ≤ 12 séparés par « / » est ambigu (J/M ou M/J) ; « 03.04. » ne l'est pas (usage suisse) ;
- un jour de la semaine écrit dans la source correspond à la date ;
- une heure proposée est écrite dans la source ;
- la personne concernée est une des personnes du foyer déclarées (quand la liste existe) ;
- quand le document a un texte extrait, la source s'y trouve (le modèle ne cite pas ce qui n'y est pas).

Une date qui échoue à l'un de ces contrôles est SIGNALÉE et n'est pas écrite : rien n'est jamais deviné.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any

MAX_EVENTS = 60

_MONTHS = {
    # fr
    "janvier": 1, "janv": 1, "fevrier": 2, "fevr": 2, "fev": 2, "mars": 3, "avril": 4, "avr": 4, "mai": 5,
    "juin": 6, "juillet": 7, "juil": 7, "aout": 8, "septembre": 9, "sept": 9, "octobre": 10, "oct": 10,
    "novembre": 11, "nov": 11, "decembre": 12, "dec": 12,
    # de
    "januar": 1, "jan": 1, "februar": 2, "feb": 2, "marz": 3, "maerz": 3, "april": 4, "apr": 4, "juni": 6,
    "juli": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "oktober": 10, "okt": 10, "november": 11,
    "dezember": 12, "dez": 12,
    # en
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7, "october": 10, "december": 12,
    # it
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "dicembre": 12,
}
_WEEKDAYS = {
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4, "samedi": 5, "dimanche": 6,
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5, "sonntag": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    "lunedi": 0, "martedi": 1, "mercoledi": 2, "giovedi": 3, "venerdi": 4, "sabato": 5, "domenica": 6,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_NUMERIC = re.compile(r"(?<![\d.])(\d{1,2})([./-])(\d{1,2})(?:\2(\d{4}|\d{2}))?(?![\d])")
_ISO = re.compile(r"(?<!\d)((?:19|20)\d{2})-(\d{2})-(\d{2})(?!\d)")
_DAY_MONTH = re.compile(rf"(?<!\d)(\d{{1,2}})(?:er|\.|e|st|nd|rd|th)?\s+({_MONTH_ALT})\.?(?:\s+((?:19|20)\d{{2}}))?\b")
_MONTH_DAY = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+((?:19|20)\d{{2}}))?(?!\d)")
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_SCHOOL_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*[-/–]\s*((?:19|20)?\d{2})(?!\d)")
_TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3])\s*(?:h|:|\.|uhr\b)\s*([0-5]\d)?(?!\d)")
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(s.split())


@dataclass
class Candidate:
    day: int
    month: int
    year: int | None
    slash: bool = False


def candidates(source: str) -> list[Candidate]:
    s = _norm(source)
    out: list[Candidate] = []
    taken: list[tuple[int, int]] = []

    def add(span: tuple[int, int], c: Candidate) -> None:
        if any(a < span[1] and span[0] < b for a, b in taken):
            return
        if 1 <= c.day <= 31 and 1 <= c.month <= 12:
            taken.append(span)
            out.append(c)

    for m in _ISO.finditer(s):
        add(m.span(), Candidate(int(m.group(3)), int(m.group(2)), int(m.group(1))))
    for m in _DAY_MONTH.finditer(s):
        add(m.span(), Candidate(int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)) if m.group(3) else None))
    for m in _MONTH_DAY.finditer(s):
        add(m.span(), Candidate(int(m.group(2)), _MONTHS[m.group(1)], int(m.group(3)) if m.group(3) else None))
    for m in _NUMERIC.finditer(s):
        if m.group(2) == ":" or (m.group(2) == "." and not m.group(4) and s[m.end():m.end() + 1] not in (".", "")
                                 and not s[m.end():m.end() + 1].isspace()):
            continue
        y = m.group(4)
        year = None if not y else (int(y) if len(y) == 4 else 2000 + int(y))
        add(m.span(), Candidate(int(m.group(1)), int(m.group(3)), year, slash=m.group(2) == "/"))
    return out


def school_year(text: str, month: int) -> int | None:
    """« 2026-2027 » / « 2026/27 » → année du mois (août-déc. : la première ; janv.-juil. : la seconde)."""
    found = set()
    for m in _SCHOOL_YEAR.finditer(text or ""):
        first, second = int(m.group(1)), m.group(2)
        second_y = int(second) if len(second) == 4 else (first // 100) * 100 + int(second)
        if second_y == first + 1:
            found.add(first)
    if len(found) != 1:  # aucune, ou plusieurs années scolaires : on ne choisit pas
        return None
    first = found.pop()
    return first if month >= 8 else first + 1


def year_from(text: str, month: int) -> int | None:
    sy = school_year(text, month)
    if sy is not None:
        return sy
    years = {int(y) for y in _YEAR.findall(text or "")}
    return years.pop() if len(years) == 1 else None


def times(source: str) -> list[str]:
    out = []
    for m in _TIME.finditer(_norm(source)):
        out.append(f"{int(m.group(1)):02d}:{m.group(2) or '00'}")
    return out


@dataclass
class Reviewed:
    index: int
    title: str
    date: str
    start: str = ""
    end: str = ""
    location: str = ""
    person: str = ""
    source: str = ""
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def line(self) -> str:
        when = self.date + (f" {self.start}" if self.start else "") + (f"-{self.end}" if self.end else "")
        who = f" [{self.person}]" if self.person else ""
        where = f" @ {self.location}" if self.location else ""
        head = f"{self.index + 1}. {when}{who} {self.title}{where}"
        if self.problems:
            return f"{head} — À PRÉCISER : {'; '.join(self.problems)} (source : « {self.source[:80]} »)"
        return head + (f" ({'; '.join(self.notes)})" if self.notes else "")

    def to_json(self) -> dict[str, Any]:
        return {"index": self.index, "title": self.title, "date": self.date, "start": self.start or None,
                "end": self.end or None, "location": self.location or None, "person": self.person or None,
                "ok": self.ok, "problems": self.problems, "notes": self.notes}


REPORT_NOT_WRITTEN = ("Rapporte à l'utilisateur chaque élément NON écrit ci-dessus et pourquoi ; ne le présente "
                      "jamais comme ajouté.")


def not_written(reviewed: list[Reviewed]) -> str:
    """En tête d'un résultat ou d'une carte d'agenda, en clair : ce qui N'EST PAS écrit, puis la consigne de le
    rapporter. Vide quand tout est écrit."""
    bad = [r for r in reviewed if not r.ok]
    if not bad:
        return ""
    lines = [f"NON écrit (à préciser) : {r.title or 'sans titre'}" + (f" ({r.date})" if r.date else "")
             + f" — {'; '.join(r.problems)}." for r in bad]
    return "\n".join([*lines, REPORT_NOT_WRITTEN]) + "\n"


def review(events: Any, *, people: list[str] | None = None, document_text: str | None = None) -> list[Reviewed]:
    """Contrôle chaque événement proposé ; ne lève jamais pour un événement mal formé (il est signalé)."""
    if not isinstance(events, list) or not events:
        raise ValueError("events : une liste d'événements non vide")
    if len(events) > MAX_EVENTS:
        raise ValueError(f"au plus {MAX_EVENTS} événements par carte")
    known = {_norm(p) for p in (people or []) if p}
    doc = _norm(document_text) if document_text else None
    out: list[Reviewed] = []
    for i, ev in enumerate(events):
        ev = ev if isinstance(ev, dict) else {}
        r = Reviewed(index=i, title=" ".join(str(ev.get("title") or "").split())[:120],
                     date=str(ev.get("date") or "").strip(), start=str(ev.get("start") or "").strip(),
                     end=str(ev.get("end") or "").strip(), location=" ".join(str(ev.get("location") or "").split())[:120],
                     person=" ".join(str(ev.get("person") or "").split())[:60],
                     source=" ".join(str(ev.get("source") or "").split())[:300])
        year_source = " ".join(str(ev.get("year_source") or "").split())[:200]
        if not r.title:
            r.problems.append("titre manquant")
        try:
            d = date.fromisoformat(r.date)
        except ValueError:
            r.problems.append("date illisible (AAAA-MM-JJ attendu)")
            out.append(r)
            continue
        if not r.source:
            r.problems.append("texte source de la date manquant")
            out.append(r)
            continue
        if doc is not None and _norm(r.source) not in doc:
            r.problems.append("texte source introuvable dans le document")
        if doc is not None and year_source and _norm(year_source) not in doc:
            r.problems.append("texte source de l'année introuvable dans le document")
        cands = candidates(r.source)
        if not cands:
            r.problems.append("pas de jour et de mois explicites dans la source")
        else:
            match = [c for c in cands if c.day == d.day and c.month == d.month]
            if not match:
                r.problems.append("la date ne correspond à aucune date écrite dans la source")
            else:
                c = match[0]
                if c.slash and c.day <= 12 and c.month <= 12 and c.day != c.month:
                    r.problems.append("format J/M ou M/J ambigu")
                year = c.year if c.year is not None else year_from(year_source or r.source, d.month)
                if year is None and not year_source and document_text:
                    year = year_from(document_text, d.month)  # une seule année (ou année scolaire) dans le document
                    if year is not None:
                        r.notes.append("année lue dans le document")
                if year is None:
                    r.problems.append("année absente de la source")
                elif year != d.year:
                    r.problems.append(f"année écrite {year}, date proposée {d.year}")
        wd = {_WEEKDAYS[w] for w in re.findall(r"[a-z]+", _norm(r.source)) if w in _WEEKDAYS}
        if wd and d.weekday() not in wd:
            r.problems.append("jour de la semaine incohérent avec la date")
        written = times(r.source)
        for label, value in (("début", r.start), ("fin", r.end)):
            if not value:
                continue
            m = _HHMM.match(value)
            if not m:
                r.problems.append(f"heure de {label} illisible (HH:MM)")
            elif f"{int(m.group(1)):02d}:{m.group(2)}" not in written:
                r.problems.append(f"heure de {label} absente de la source")
        if r.start and r.end and _HHMM.match(r.start) and _HHMM.match(r.end) and r.end <= r.start:
            r.problems.append("fin avant le début")
        if written and not r.start:
            r.notes.append("heure écrite dans la source, événement posé sur la journée")
        if r.person and known and _norm(r.person) not in known:
            r.problems.append("personne inconnue du foyer")
        out.append(r)
    return out


__all__ = ["MAX_EVENTS", "Candidate", "Reviewed", "candidates", "not_written", "review", "school_year", "times", "year_from"]
