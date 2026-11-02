"""Relecture des lignes du calendrier familial (drapeau `family_calendar`, docs/FAMILY-CALENDAR.md §4), SANS modèle.

Le modèle lit le document (photo jointe, PDF importé, pièce jointe texte) et propose des événements ; chaque ligne
passe d'abord par la relecture des dates des Papiers du foyer (`nokker/household/paper/dates.py` : source explicite,
année écrite, J/M ou M/J, jour de la semaine, heure écrite, personne du foyer, source présente dans le texte), puis
par les contrôles propres au calendrier familial :

- date déjà passée (un événement isolé) ;
- personne qui ressemble à un compte (adresse, identifiant) hors du foyer : une ligne ne vise jamais un autre compte ;
- récurrence : fréquence connue, date de FIN écrite dans le texte de la série (jamais une série sans fin devinée),
  exceptions écrites, jour de la semaine de la série cohérent avec la première date ;
- doublons : avec l'agenda existant (même jour, même heure, titre proche) et entre lignes du brouillon.

Une ligne qui échoue est SIGNALÉE et n'est pas écrite ; l'utilisateur la corrige (`family_calendar_edit`, ou la carte
web), la rejette, ou garde un doublon. Rien n'est jamais deviné en silence.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from nokker.household.paper import dates as pdates
from nokker.qa.untrusted import inline

MAX_EVENTS = pdates.MAX_EVENTS
MAX_EXCEPT = 30
MAX_SPAN_DAYS = 400
# Valeurs du document recopiées HORS du cadre <untrusted-data> (motif de carte, « NON écrit ») : `inline()` et coupées.
MAX_SHOWN = 80
FREQS = {"daily": "DAILY", "weekly": "WEEKLY", "monthly": "MONTHLY"}
_FREQ_LABEL = {"daily": "jour", "weekly": "semaine", "monthly": "mois"}
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_ACCOUNTISH = re.compile(r"@|^(?:us|tn|acc)_", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9]+")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(s.split())


def _clean(v: Any, limit: int) -> str:
    return " ".join(str(v or "").split())[:limit]


@dataclass
class Line:
    index: int
    title: str = ""
    date: str = ""
    start: str = ""
    end: str = ""
    location: str = ""
    person: str = ""
    source: str = ""
    year_source: str = ""
    recurrence: dict[str, Any] | None = None  # {freq, interval, until, except, source}, normalisée
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    duplicate_of: str = ""
    rejected: bool = False
    keep_duplicate: bool = False
    edited: bool = False

    @property
    def status(self) -> str:
        if self.rejected:
            return "rejected"
        if self.problems:
            return "check"
        if self.duplicate_of and not self.keep_duplicate:
            return "duplicate"
        return "ok"

    @property
    def writable(self) -> bool:
        return self.status == "ok"

    def when(self) -> str:
        out = self.date + (f" {self.start}" if self.start else "") + (f"-{self.end}" if self.end else "")
        rec = self.recurrence
        if rec:
            n = int(rec.get("interval") or 1)
            unit = _FREQ_LABEL.get(rec.get("freq", ""), "?")
            every = f"chaque {unit}" if n == 1 else f"toutes les {n} {unit}s" if unit != "mois" else f"tous les {n} mois"
            out += f", {every} jusqu'au {rec.get('until') or '?'}"
            if rec.get("except"):
                out += f" (sauf {', '.join(rec['except'])})"
        return out

    def text(self) -> str:
        """Ligne lisible (carte, résultats) ; les valeurs venues du document passent par `inline` (aucune balise,
        aucun faux bloc)."""
        who = f" [{inline(self.person, 40)}]" if self.person else ""
        where = f" @ {inline(self.location, MAX_SHOWN)}" if self.location else ""
        return f"{self.index + 1}. {self.when()}{who} {inline(self.title, MAX_SHOWN) or 'sans titre'}{where}"

    def why_not(self) -> str:
        if self.status == "rejected":
            return "rejeté par l'utilisateur"
        if self.status == "duplicate":
            return f"doublon probable de {self.duplicate_of}"
        return "; ".join(self.problems)

    def summary(self) -> str:
        return f"[{self.person}] {self.title}" if self.person else self.title

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Line:
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def _match_written(when: date, src: str, year_hint: str, doc_text: str | None) -> list[str]:
    """Problèmes si `when` n'est pas une date écrite dans `src` (jour+mois, année écrite ou déduite)."""
    cands = [c for c in pdates.candidates(src) if c.day == when.day and c.month == when.month]
    if not cands:
        return ["absente du texte"]
    c = cands[0]
    out = []
    if c.slash and c.day <= 12 and c.month <= 12 and c.day != c.month:
        out.append("format J/M ou M/J ambigu")
    year = c.year if c.year is not None else pdates.year_from(year_hint or src, when.month)
    if year is None and doc_text:
        year = pdates.year_from(doc_text, when.month)
    if year is None:
        out.append("année absente")
    elif year != when.year:
        out.append(f"année écrite {year}, proposée {when.year}")
    return out


def check_recurrence(rec: Any, first: date | None, *, year_source: str, doc_text: str | None
                     ) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    """(récurrence normalisée, problèmes, notes)."""
    if rec in (None, {}, ""):
        return None, [], []
    if not isinstance(rec, dict):
        return None, ["récurrence illisible"], []
    problems: list[str] = []
    notes: list[str] = []
    freq = str(rec.get("freq") or "").strip().lower()
    try:
        interval = int(rec.get("interval") or 1)
    except (TypeError, ValueError):
        interval = 0
    src = _clean(rec.get("source"), 300)
    until = _clean(rec.get("until"), 10)
    excepts = rec.get("except") or []
    out: dict[str, Any] = {"freq": freq, "interval": interval, "until": until, "except": [], "source": src}
    if freq not in FREQS:
        problems.append("récurrence illisible (daily, weekly ou monthly)")
    if not 1 <= interval <= 4:
        problems.append("intervalle de récurrence hors de 1 à 4")
        out["interval"] = 1
    if not src:
        problems.append("texte source de la récurrence manquant")
    elif doc_text is not None and norm(src) not in norm(doc_text):
        problems.append("texte source de la récurrence introuvable dans le document")
    if not until:
        problems.append("fin de la récurrence non écrite : à préciser (jamais une série sans fin)")
        return out, problems, notes
    try:
        end = date.fromisoformat(until)
    except ValueError:
        problems.append("fin de la récurrence illisible (AAAA-MM-JJ attendu)")
        return out, problems, notes
    if first is not None:
        if end <= first:
            problems.append("fin de la récurrence avant la première date")
        elif (end - first).days > MAX_SPAN_DAYS:
            problems.append(f"série de plus de {MAX_SPAN_DAYS} jours")
    if src:
        problems += [f"fin de la récurrence {p}" for p in _match_written(end, src, year_source, doc_text)]
    if not isinstance(excepts, list) or len(excepts) > MAX_EXCEPT:
        problems.append(f"exceptions illisibles (au plus {MAX_EXCEPT} dates)")
        excepts = []
    for raw in excepts:
        x = _clean(raw, 10)
        try:
            xd = date.fromisoformat(x)
        except ValueError:
            problems.append(f"exception illisible : {inline(x, 20)}")
            continue
        if first is not None and not first <= xd <= end:
            problems.append(f"exception {x} hors de la série")
            continue
        found = _match_written(xd, src, year_source, doc_text) if src else ["absente du texte"]
        if found and doc_text:
            found = _match_written(xd, doc_text, year_source, doc_text)
        if found:
            problems.append(f"exception {x} {found[0]}")
            continue
        out["except"].append(x)
    if freq == "weekly" and first is not None and src:
        days = {pdates._WEEKDAYS[w.rstrip("s")] for w in _WORD.findall(norm(src)) if w.rstrip("s") in pdates._WEEKDAYS}
        if days and first.weekday() not in days:
            problems.append("jour de la semaine de la série incohérent avec la première date")
    return out, problems, notes


def review(events: Any, *, people: list[str] | None, document_text: str | None, today: date) -> list[Line]:
    """Relit chaque événement proposé ; ne lève que pour une liste vide ou trop longue."""
    if not isinstance(events, list) or not events:
        raise ValueError("events : une liste d'événements non vide")
    if len(events) > MAX_EVENTS:
        raise ValueError(f"au plus {MAX_EVENTS} événements par brouillon")
    known = {norm(p) for p in (people or []) if p}
    base = pdates.review([e if isinstance(e, dict) else {} for e in events], people=people,
                         document_text=document_text)
    out: list[Line] = []
    for r, ev in zip(base, events):
        ev = ev if isinstance(ev, dict) else {}
        line = Line(index=r.index, title=r.title, date=r.date, start=r.start, end=r.end, location=r.location,
                    person=r.person, source=r.source, year_source=_clean(ev.get("year_source"), 200),
                    problems=list(r.problems), notes=list(r.notes))
        _common_checks(line, known, today)
        rec, problems, notes = check_recurrence(ev.get("recurrence"), _date(line.date), year_source=line.year_source,
                                                doc_text=document_text)
        line.recurrence, line.problems, line.notes = rec, line.problems + problems, line.notes + notes
        _past(line, today)
        out.append(line)
    return out


def _date(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _common_checks(line: Line, known: set[str], today: date) -> None:
    if line.person and _ACCOUNTISH.search(line.person) and norm(line.person) not in known:
        line.problems.append("vise un compte hors du foyer")
    line.problems = list(dict.fromkeys(line.problems))


def _past(line: Line, today: date) -> None:
    d = _date(line.date)
    if d is None or d >= today:
        return
    until = _date(str((line.recurrence or {}).get("until") or ""))
    if until is not None and until >= today:
        line.notes.append(f"série commencée le {line.date}")
    else:
        line.problems.append("date déjà passée")


def revise(line: Line, fields: dict[str, Any], *, people: list[str] | None, today: date) -> Line:
    """Correction demandée par l'utilisateur (outil d'édition, carte web) : les valeurs données remplacent celles du
    document. La source n'est plus exigée (c'est l'utilisateur qui parle), mais la forme l'est toujours ; la ligne est
    marquée « corrigée » et la carte la montre comme telle."""
    for k in ("title", "location"):
        if k in fields:
            setattr(line, k, _clean(fields[k], 120))
    if "person" in fields:
        line.person = _clean(fields["person"], 60)
    for k in ("date", "start", "end"):
        if k in fields:
            setattr(line, k, _clean(fields[k], 10))
    line.problems, line.notes, line.edited = [], ["corrigé à la demande de l'utilisateur"], True
    d = _date(line.date)
    if not line.title:
        line.problems.append("titre manquant")
    if d is None:
        line.problems.append("date illisible (AAAA-MM-JJ attendu)")
    for label, v in (("début", line.start), ("fin", line.end)):
        if v and not _HHMM.match(v):
            line.problems.append(f"heure de {label} illisible (HH:MM)")
    if line.end and not line.start:
        line.problems.append("heure de fin sans heure de début")
    if line.start and line.end and _HHMM.match(line.start) and _HHMM.match(line.end) and \
            _hm(line.end) <= _hm(line.start):
        line.problems.append("fin avant le début")
    known = {norm(p) for p in (people or []) if p}
    if line.person and known and norm(line.person) not in known:
        line.problems.append("personne inconnue du foyer")
    _common_checks(line, known, today)
    if "recurrence" in fields:
        rec = fields["recurrence"]
        if rec in (None, {}, ""):
            line.recurrence = None
        else:
            line.recurrence, problems = _user_recurrence(rec, d, (line.recurrence or {}).get("source", ""))
            line.problems += problems
    _past(line, today)
    return line


def _user_recurrence(rec: Any, first: date | None, old_source: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Série corrigée par l'utilisateur : la forme est exigée (fréquence, fin, exceptions dans la série), pas une
    citation du document."""
    if not isinstance(rec, dict):
        return None, ["récurrence illisible"]
    freq = str(rec.get("freq") or "").strip().lower()
    try:
        interval = int(rec.get("interval") or 1)
    except (TypeError, ValueError):
        interval = 0
    until = _clean(rec.get("until"), 10)
    out: dict[str, Any] = {"freq": freq, "interval": interval if 1 <= interval <= 4 else 1, "until": until,
                           "except": [], "source": _clean(rec.get("source"), 300) or old_source}
    problems = []
    if freq not in FREQS:
        problems.append("récurrence illisible (daily, weekly ou monthly)")
    if not 1 <= interval <= 4:
        problems.append("intervalle de récurrence hors de 1 à 4")
    end = _date(until)
    if end is None:
        problems.append("fin de la récurrence non écrite : à préciser (jamais une série sans fin)")
        return out, problems
    if first is not None and end <= first:
        problems.append("fin de la récurrence avant la première date")
    elif first is not None and (end - first).days > MAX_SPAN_DAYS:
        problems.append(f"série de plus de {MAX_SPAN_DAYS} jours")
    for raw in (rec.get("except") or [])[:MAX_EXCEPT]:
        xd = _date(_clean(raw, 10))
        if xd is None or (first is not None and not first <= xd <= end):
            problems.append(f"exception {inline(raw, 20)} illisible ou hors de la série")
        else:
            out["except"].append(xd.isoformat())
    return out, problems


def _hm(v: str) -> int:
    h, m = v.split(":")
    return int(h) * 60 + int(m)


# ─────────────────────────────── doublons ───────────────────────────────


def _tokens(title: str) -> set[str]:
    t = re.sub(r"^\[[^\]]*\]\s*", "", title or "")
    return {w for w in _WORD.findall(norm(t)) if len(w) >= 3}


def _similar(a: str, b: str) -> bool:
    na, nb = norm(re.sub(r"^\[[^\]]*\]\s*", "", a)), norm(re.sub(r"^\[[^\]]*\]\s*", "", b))
    if na and nb and (na in nb or nb in na):
        return True
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= 0.5


def _slot(start_iso: str) -> tuple[str, str]:
    """« 2026-11-14T09:00+01:00 » → (« 2026-11-14 », « 09:00 ») ; « 2026-11-14 » → (date, « »)."""
    if "T" not in start_iso:
        return start_iso[:10], ""
    try:
        dt = datetime.fromisoformat(start_iso)
    except ValueError:
        return start_iso[:10], start_iso[11:16]
    return dt.date().isoformat(), dt.strftime("%H:%M")


def mark_duplicates(lines: list[Line], existing: list[dict[str, Any]] | None) -> None:
    """Marque `duplicate_of` : un événement de l'agenda au même jour et à la même heure (ou tous deux sur la journée)
    avec un titre proche, ou une ligne précédente du brouillon identique. `existing` = événements rendus par
    `calendar_events` (heures déjà dans le fuseau du compte)."""
    seen: list[Line] = []
    slots: dict[tuple[str, str], list[str]] = {}
    for ev in existing or []:
        slots.setdefault(_slot(str(ev.get("start") or "")), []).append(str(ev.get("summary") or ""))
    for line in lines:
        line.duplicate_of = ""
        if line.rejected or not _date(line.date):
            continue
        key = (line.date, line.start)
        for prev in seen:
            if (prev.date, prev.start) == key and _similar(prev.title, line.title):
                line.duplicate_of = f"la ligne {prev.index + 1}"
                break
        if not line.duplicate_of:
            for summary in slots.get(key, []):
                if _similar(summary, line.title):
                    line.duplicate_of = (f"« {inline(summary, MAX_SHOWN)} » déjà dans l'agenda le {line.date}"
                                         + (f" à {line.start}" if line.start else ""))
                    break
        seen.append(line)


def window_days(lines: list[Line], today: date) -> int:
    """Jours d'agenda à relire pour les doublons (1 à 366, la limite de `calendar_events`)."""
    last = max((d for d in (_date(x.date) for x in lines) if d is not None), default=today)
    return max(1, min(366, (last - today).days + 2))


def beyond_window(lines: list[Line], today: date) -> bool:
    return any((d - today) > timedelta(days=365) for d in (_date(x.date) for x in lines) if d is not None)


__all__ = [
    "FREQS",
    "MAX_EVENTS",
    "Line",
    "beyond_window",
    "check_recurrence",
    "mark_duplicates",
    "norm",
    "review",
    "revise",
    "window_days",
]
