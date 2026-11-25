"""Relecture d'un plan de projet du foyer (drapeau `family_projects`, docs/FAMILY-PROJECTS.md §4), SANS modèle.

Le modèle propose un plan (en-tête : titre, objectif, échéance, membres, visibilité, budget indicatif ; lignes : jalons
et tâches) ; le noyau relit chaque ligne avant toute carte :

- titre présent ; échéance lisible (AAAA-MM-JJ), pas déjà passée, pas après l'échéance du projet ;
- date relue contre les mots de la personne (`due_source`, comme les sources du calendrier familial) : jour et mois
  écrits, J/M ou M/J ambigu, jour de la semaine cohérent. Sans mots écrits (« dans deux semaines »), la date est
  calculée par l'assistant : la ligne passe, avec une note visible sur la carte ;
- assigné = un membre ACTIF du foyer (prénom ou identifiant) ; jamais une adresse ni un compte hors du foyer ; un
  profil mineur n'est jamais assigné dans un projet réservé aux parents ;
- doublons entre lignes (même titre, même assigné, même date) ;
- replanification : une ligne qui reprend une tâche existante (`task_id`) et ne change rien n'est pas réécrite ; une
  tâche en retard laissée à sa date passée est signalée.

Une ligne qui échoue est SIGNALÉE et n'est pas écrite ; un en-tête qui échoue bloque tout le plan. Rien n'est deviné en
silence. Les valeurs écrites par la personne ou le modèle sont recopiées hors clôture par `inline()`, coupées à
`MAX_SHOWN` caractères.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from nokker.household.paper import dates as pdates
from nokker.qa.untrusted import inline

MAX_LINES = 60
MAX_SHOWN = 80
MAX_MEMBERS = 6
VISIBILITIES = ("household", "parents")
KINDS = ("anniversaire", "vacances", "demenagement", "rentree", "travaux", "impots", "autre")
LINE_KINDS = ("task", "milestone")
MAX_BUDGET = Decimal("1000000")
_ACCOUNTISH = re.compile(r"@|^(?:us|tn|acc)_", re.IGNORECASE)
_WEEKDAYS = pdates._WEEKDAYS  # noqa: SLF001 — même table que la relecture des Papiers


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(s.split())


def clean(v: Any, limit: int) -> str:
    return " ".join(str(v or "").split())[:limit]


@dataclass(frozen=True)
class Person:
    """Membre actif du foyer, tel que le noyau le connaît (jamais tel que le modèle l'écrit)."""

    account_id: str
    name: str
    profile: str = "adult"  # adult | teen | child

    @property
    def minor(self) -> bool:
        return self.profile in ("teen", "child")


def find_person(value: str, people: list[Person]) -> Person | None:
    """Prénom (ou identifiant `us_…`) → membre du foyer. Un prénom porté par deux membres ne désigne personne."""
    v = norm(value)
    if not v:
        return None
    exact = [p for p in people if p.account_id == value.strip() or norm(p.name) == v]
    if len(exact) == 1:
        return exact[0]
    first = [p for p in people if norm(p.name).split(" ")[0] == v]
    return first[0] if len(first) == 1 else None


def _date(s: str) -> date | None:
    try:
        return date.fromisoformat(s) if len(s) == 10 else None
    except ValueError:
        return None


def check_written(due: date, source: str) -> tuple[list[str], list[str]]:
    """(problèmes, notes) de la date `due` relue contre les mots de la personne (`source`)."""
    if not source:
        return [], ["date calculée par l'assistant, à relire"]
    cands = pdates.candidates(source)
    problems: list[str] = []
    if not cands:
        notes = ["date calculée par l'assistant depuis « " + inline(source, MAX_SHOWN) + " », à relire"]
    else:
        notes = []
        match = [c for c in cands if c.day == due.day and c.month == due.month]
        if not match:
            problems.append("la date ne correspond à aucune date écrite dans la demande")
        else:
            c = match[0]
            if c.slash and c.day <= 12 and c.month <= 12 and c.day != c.month:
                problems.append("format J/M ou M/J ambigu")
            if c.year is not None and c.year != due.year:
                problems.append(f"année écrite {c.year}, date proposée {due.year}")
    wd = {_WEEKDAYS[w] for w in re.findall(r"[a-z]+", norm(source)) if w in _WEEKDAYS}
    if wd and due.weekday() not in wd:
        problems.append("jour de la semaine incohérent avec la date")
    return problems, notes


# ─────────────────────────────── en-tête ───────────────────────────────


@dataclass
class Header:
    title: str = ""
    goal: str = ""
    deadline: str = ""
    deadline_source: str = ""
    kind: str = "autre"
    members: list[str] = field(default_factory=list)  # account_id des membres (relus)
    visibility: str = "household"
    budget: dict[str, str] | None = None  # {"amount": "1200.00", "currency": "CHF"} : indicatif, aucun paiement
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Header:
        known = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _budget(v: Any) -> tuple[dict[str, str] | None, str | None]:
    if v in (None, "", {}):
        return None, None
    raw = v.get("amount") if isinstance(v, dict) else v
    cur = clean(v.get("currency") if isinstance(v, dict) else "", 3).upper() or "CHF"
    try:
        amount = Decimal(str(raw).replace("'", "").replace(" ", ""))
    except (InvalidOperation, ValueError):
        return None, "budget illisible (un montant, ex. 1200)"
    if not amount.is_finite() or amount < 0 or amount > MAX_BUDGET:
        return None, "budget hors bornes (0 à 1 000 000)"
    if not re.fullmatch(r"[A-Z]{3}", cur):
        return None, "devise illisible (code à 3 lettres, ex. CHF)"
    return {"amount": f"{amount.quantize(Decimal('0.01'))}", "currency": cur}, None


def review_header(raw: Any, *, people: list[Person], author: str, today: date, replan: bool = False) -> Header:
    raw = raw if isinstance(raw, dict) else {}
    h = Header(title=clean(raw.get("title"), 120), goal=clean(raw.get("goal"), 500),
               deadline=clean(raw.get("deadline"), 10), deadline_source=clean(raw.get("deadline_source"), 200),
               kind=clean(raw.get("kind"), 20) or "autre",
               visibility=clean(raw.get("visibility"), 12) or "household")
    if not h.title:
        h.problems.append("titre du projet manquant")
    if h.kind not in KINDS:
        h.notes.append(f"genre « {inline(h.kind, 20)} » inconnu : rangé en « autre »")
        h.kind = "autre"
    if h.visibility not in VISIBILITIES:
        h.problems.append("visibilité : household (tout le foyer) ou parents")
    d = _date(h.deadline)
    if d is None:
        h.problems.append("échéance du projet illisible (AAAA-MM-JJ attendu)")
    else:
        if d < today and not replan:
            h.problems.append("échéance du projet déjà passée")
        p, n = check_written(d, h.deadline_source)
        h.problems += p
        h.notes += n
    members: list[str] = [author]
    names = raw.get("members") if isinstance(raw.get("members"), list) else []
    for v in names[:MAX_MEMBERS + 2]:
        s = clean(v, 60)
        if _ACCOUNTISH.search(s) and not s.startswith("us_"):
            h.problems.append(f"membre « {inline(s, MAX_SHOWN)} » : une adresse n'est pas un membre du foyer")
            continue
        who = find_person(s, people)
        if who is None:
            h.problems.append(f"membre « {inline(s, MAX_SHOWN)} » inconnu dans le foyer")
        elif who.account_id not in members:
            members.append(who.account_id)
    h.members = members
    if h.visibility == "parents":
        for who in people:
            if who.account_id in members and who.minor:
                h.problems.append(f"{inline(who.name, MAX_SHOWN)} est un profil mineur : pas dans un projet réservé "
                                  "aux parents")
    h.budget, err = _budget(raw.get("budget"))
    if err:
        h.problems.append(err)
    return h


# ─────────────────────────────── lignes ───────────────────────────────


@dataclass
class Line:
    index: int
    kind: str = "task"
    title: str = ""
    due: str = ""
    due_source: str = ""
    assignee: str = ""  # account_id relu ; vide pour un jalon
    assignee_name: str = ""
    milestone: str = ""  # titre du jalon de rattachement (tâche)
    task_id: str = ""  # replanification : tâche existante
    original: dict[str, str] = field(default_factory=dict)  # replanification : {title, due, assignee}
    document_id: str = ""  # papier du foyer rattaché (facture, échéance), relu par le noyau
    document_title: str = ""
    rejected: bool = False
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def unchanged(self) -> bool:
        return bool(self.task_id) and self.original == {"title": self.title, "due": self.due,
                                                         "assignee": self.assignee}

    @property
    def status(self) -> str:
        if self.rejected:
            return "rejected"
        if self.problems:
            return "check"
        if self.unchanged:
            return "same"
        return "ok"

    @property
    def writable(self) -> bool:
        return self.status == "ok"

    def why_not(self) -> str:
        if self.rejected:
            return "rejetée par l'utilisateur"
        if self.problems:
            return "; ".join(self.problems)
        if self.unchanged:
            return "inchangée"
        return ""

    def text(self) -> str:
        label = "jalon" if self.kind == "milestone" else "tâche"
        who = f" → {inline(self.assignee_name, MAX_SHOWN)}" if self.assignee_name else ""
        when = f" pour le {self.due}" if self.due else ""
        moved = ""
        if self.task_id and self.original.get("due") and self.original["due"] != self.due:
            moved = f" (avant : {self.original['due']})"
        doc = f" [papier : {inline(self.document_title, MAX_SHOWN)}]" if self.document_title else ""
        return f"{self.index + 1}. {label} : {inline(self.title, MAX_SHOWN)}{who}{when}{moved}{doc}"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Line:
        known = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _review_line(line: Line, *, people: list[Person], header: Header, today: date,
                 paper_lookup: Any = None) -> None:
    line.problems, line.notes = [], []
    if line.kind not in LINE_KINDS:
        line.problems.append("genre de ligne : task ou milestone")
    if not line.title:
        line.problems.append("titre manquant")
    deadline = _date(header.deadline)
    d = _date(line.due)
    if line.document_id and paper_lookup is not None:
        found = paper_lookup(line.document_id)
        if found is None:
            line.problems.append("papier rattaché introuvable dans les Papiers de ce compte")
            line.document_title = ""
        else:
            line.document_title, bill_due = found
            if not line.due and bill_due:
                line.due, d = bill_due, _date(bill_due)
                line.notes.append("échéance reprise du papier")
    elif line.document_id:
        line.problems.append("papiers du foyer non allumés : aucun papier à rattacher")
    if d is None:
        if line.kind == "task" or line.due:
            line.problems.append("échéance illisible ou absente (AAAA-MM-JJ attendu)")
    else:
        overdue_kept = bool(line.task_id) and line.original.get("due") == line.due and d < today
        if overdue_kept:
            line.problems.append(f"en retard (échéance {line.due} passée) : nouvelle date à poser")
        elif d < today:
            line.problems.append("date déjà passée")
        if deadline is not None and d > deadline:
            line.problems.append(f"après l'échéance du projet ({header.deadline})")
        if not line.task_id or line.original.get("due") != line.due:
            p, n = check_written(d, line.due_source)
            line.problems += p
            line.notes += n
    if line.kind == "task":
        raw = line.assignee_name or line.assignee
        if not raw:
            line.problems.append("assigné manquant (un membre du foyer)")
            line.assignee = ""
        elif _ACCOUNTISH.search(raw) and not raw.startswith("us_"):
            line.problems.append("une adresse n'est pas un membre du foyer : rien ne part hors du foyer")
            line.assignee = ""
        else:
            who = find_person(raw, people)
            if who is None:
                line.problems.append(f"« {inline(raw, MAX_SHOWN)} » n'est pas un membre actif du foyer")
                line.assignee = ""
            else:
                line.assignee, line.assignee_name = who.account_id, who.name
                if header.visibility == "parents" and who.minor:
                    line.problems.append("profil mineur : pas de tâche dans un projet réservé aux parents")
                elif who.account_id not in header.members:
                    header.members.append(who.account_id)
                    line.notes.append(f"{inline(who.name, MAX_SHOWN)} ajouté aux membres du projet")
    else:
        line.assignee, line.assignee_name = "", ""


def _dupes(lines: list[Line]) -> None:
    seen: dict[tuple[str, str, str, str], int] = {}
    for x in lines:
        if x.rejected:
            continue
        key = (x.kind, norm(x.title), x.assignee, x.due)
        if key in seen:
            x.problems.append(f"doublon de la ligne {seen[key] + 1}")
        else:
            seen[key] = x.index


def line_from(raw: Any, index: int) -> Line:
    raw = raw if isinstance(raw, dict) else {}
    return Line(index=index, kind=clean(raw.get("kind"), 12) or "task", title=clean(raw.get("title"), 120),
                due=clean(raw.get("due"), 10), due_source=clean(raw.get("due_source"), 200),
                assignee_name=clean(raw.get("assignee"), 60), milestone=clean(raw.get("milestone"), 120),
                document_id=clean(raw.get("document_id"), 40))


def review(header: Header, lines: list[Line], *, people: list[Person], today: date, paper_lookup: Any = None) -> None:
    """Relit toutes les lignes EN PLACE (après une création ou une édition) ; l'en-tête peut gagner des membres."""
    for x in lines:
        _review_line(x, people=people, header=header, today=today, paper_lookup=paper_lookup)
    milestones = {norm(x.title) for x in lines if x.kind == "milestone" and not x.rejected}
    for x in lines:
        if x.kind == "task" and x.milestone and norm(x.milestone) not in milestones:
            x.notes.append(f"jalon « {inline(x.milestone, MAX_SHOWN)} » absent du plan : tâche rattachée à aucun jalon")
            x.milestone = ""
    _dupes(lines)


__all__ = ["KINDS", "LINE_KINDS", "MAX_LINES", "MAX_SHOWN", "VISIBILITIES", "Header", "Line", "Person",
           "check_written", "find_person", "line_from", "norm", "review", "review_header"]
