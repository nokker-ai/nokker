"""Projets du foyer — l'agent chef de projet familial (lot F2, drapeau `family_projects`, docs/FAMILY-PROJECTS.md).

Un projet (anniversaire, vacances, déménagement, rentrée, travaux, impôts…) a un titre, un objectif, une échéance, des
membres du foyer, des jalons, des tâches (assigné, échéance, état : à faire, fait, en retard) et un budget INDICATIF
optionnel (aucun paiement, jamais).

Parcours d'écriture, comme le calendrier familial :
1. `propose` : le modèle donne un plan ; le noyau relit chaque ligne (`review.py`) et range un BROUILLON (révision 1,
   locataire de l'auteur). Rien n'est écrit dans le foyer.
2. `edit` (outil, ou carte web `PATCH …/project-drafts/{id}/lines/{n}`) : rejeter, rétablir, corriger, ajouter une
   ligne, corriger l'en-tête. Chaque modification fait monter la révision.
3. `commit` : SEULEMENT sous une carte `family:project` approuvée, liée à `{draft_id, revision}` (portes `gates.py`) ;
   une carte posée sur une révision dépassée n'écrit rien. Écrit dans le locataire du FOYER.

Suivi, sans carte, tout reste dans le foyer : lecture des projets et des tâches, coche d'une tâche, relance douce (une
note posée sur la tâche, vue par l'assigné dans Nokker ; aucun message ne part hors du foyer), point hebdo. Retard :
`replan` prépare un NOUVEAU brouillon (et donc une nouvelle carte), jamais une écriture directe.

Droits, vérifiés ici à chaque appel :
- adulte du foyer : tout (créer, éditer, cocher, relancer, replanifier, réglages) ;
- profil mineur (`teen`, `child`) : voit et coche SES tâches seulement ; aucun accès aux projets réservés aux parents
  (404, comme un projet qui n'existe pas) ; ni création, ni relance, ni réglage ;
- vues communes (mur, point hebdo) : jamais le nom de qui est en retard, sauf réglage `overdue_by_member` posé par un
  adulte ; aucun classement par membre.
Journaux : identifiants seulement (`hp_…`, `ht_…`, `pd_…`), jamais un titre.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from nokker.household.projects import review as rv
from nokker.household.projects.review import Header, Line, Person
from nokker.household.projects.store import Draft, Project, Settings, Task
from nokker.qa.untrusted import inline
from nokker.tenancy.context import current_tenant, tenant_scope

logger = logging.getLogger("nokker.household.projects")

TZ = "Europe/Zurich"
ACTIONS = ("reject", "restore", "update", "add", "header")
NUDGE_GAP_S = 20 * 3600  # une relance par tâche et par jour, au plus
NUDGE_MAX = 140
REPLAN_SHIFT_DEFAULT = 3
UPCOMING_DAYS = 7
_MONTHS_FR = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
              "novembre", "décembre")


class ProjectError(ValueError):
    """Demande refusée (400 / 403 / 404 / 409 côté route, texte d'erreur côté outil), sans contenu de projet."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def no_home() -> ProjectError:
    return ProjectError("aucun foyer : un projet familial demande un foyer Nokker", 404)


def adults_only() -> ProjectError:
    return ProjectError("réservé aux adultes du foyer", 403)


def not_found(what: str = "projet") -> ProjectError:
    return ProjectError(f"{what} introuvable", 404)


MAX_STOPS = 4


def _wall_place(v: Any) -> dict[str, Any] | None:
    """`{label, lat, lon, stops: [id]}` (identifiants d'arrêts de l'horaire suisse) ; None l'efface."""
    if v is None:
        return None
    if not isinstance(v, dict) or set(v) - {"label", "lat", "lon", "stops"}:
        raise ProjectError("wall_place : {label, lat, lon, stops}")
    try:
        lat, lon = float(v.get("lat")), float(v.get("lon"))
    except (TypeError, ValueError):
        raise ProjectError("wall_place : lat et lon numériques") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ProjectError("wall_place : lat et lon hors bornes")
    stops = v.get("stops") or []
    if not isinstance(stops, list) or len(stops) > MAX_STOPS or \
            not all(isinstance(x, str) and x.isdigit() and 5 <= len(x) <= 9 for x in stops):
        raise ProjectError(f"wall_place : stops, au plus {MAX_STOPS} identifiants d'arrêt (chiffres)")
    return {"label": " ".join(str(v.get("label") or "").split())[:60], "lat": lat, "lon": lon,
            "stops": list(dict.fromkeys(stops))}


@dataclass(frozen=True)
class Home:
    """Foyer du compte qui agit, tel que le noyau le connaît : membres ACTIFS seulement."""

    household_id: str
    tenant_id: str  # locataire du foyer (celui du payeur)
    me: Person
    people: tuple[Person, ...]

    def person(self, account_id: str) -> Person | None:
        return next((p for p in self.people if p.account_id == account_id), None)

    def name(self, account_id: str) -> str:
        p = self.person(account_id)
        return p.name if p is not None else ""


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=UTC).isoformat()


@dataclass
class FamilyProjects:
    store: Any  # MemoryProjectStore | PgProjectStore
    # Locataire → compte actif (un compte = un locataire), None sinon.
    account_of: Callable[[str], str | None]
    # Compte → son foyer (membres actifs, profils), None sans foyer ou profil suspendu.
    home_of: Callable[[str], Home | None]
    # Papiers du foyer (drapeau `household_paper`) : rattacher un papier à une tâche. None sinon.
    paper: Any = None
    # Calendrier familial (drapeau `family_calendar`) : poser les échéances dans l'agenda, par SA carte. None sinon.
    calendar: Any = None
    clock: Callable[[], float] = time.time
    new_id: Callable[[str], str] = field(default=lambda prefix: prefix + uuid.uuid4().hex[:16])

    # ─────────── identité, droits ───────────

    def home(self) -> Home:
        acc = self.account_of(current_tenant())
        if not acc:
            raise ProjectError("aucun compte pour ce locataire", 403)
        h = self.home_of(acc)
        if h is None:
            raise no_home()
        return h

    def adult_home(self) -> Home:
        h = self.home()
        if h.me.minor:
            raise adults_only()
        return h

    def today(self) -> date:
        return datetime.fromtimestamp(self.clock(), ZoneInfo(TZ)).date()

    @staticmethod
    def state(t: Task, today: date) -> str:
        if t.status == "done":
            return "done"
        return "overdue" if t.due and t.due < today.isoformat() else "todo"

    def _visible(self, h: Home, p: Project, tasks: list[Task]) -> list[Task] | None:
        """Tâches du projet que `h.me` peut voir, ou None si le projet lui est invisible."""
        if not h.me.minor:
            return tasks
        if p.visibility == "parents":
            return None
        own = [t for t in tasks if t.assignee == h.me.account_id]
        return own if own else None

    def _in_home(self, h: Home, fn: Callable[[], Any]) -> Any:
        with tenant_scope(h.tenant_id):
            return fn()

    # ─────────── lectures ───────────

    def task_json(self, h: Home, t: Task, today: date, *, project: Project | None = None,
                  hide_overdue: bool = False) -> dict[str, Any]:
        st = self.state(t, today)
        who = h.person(t.assignee) if t.assignee else None
        show_who = who is not None and not (hide_overdue and st == "overdue" and t.assignee != h.me.account_id)
        out: dict[str, Any] = {
            "id": t.task_id, "project_id": t.project_id, "project_title": project.title if project else "",
            "title": t.title, "assignee": {"account_id": who.account_id, "name": who.name} if show_who else None,
            "due": t.due or None, "state": st, "milestone": t.milestone or None, "done_at": _iso(t.done_at),
            "nudge": None, "document": None}
        if t.nudged_at is not None and t.status == "todo" and (t.assignee == h.me.account_id or not h.me.minor):
            out["nudge"] = {"at": _iso(t.nudged_at), "by": h.name(t.nudged_by) or None, "text": t.nudge_text}
        if t.document_id and not h.me.minor:
            out["document"] = {"id": t.document_id, "title": t.document_title}
        return out

    def project_json(self, h: Home, p: Project, tasks: list[Task], today: date, *,
                     hide_overdue: bool = False) -> dict[str, Any]:
        states = [self.state(t, today) for t in tasks]
        return {"id": p.project_id, "title": p.title, "goal": p.goal, "kind": p.kind, "deadline": p.deadline,
                "visibility": p.visibility, "status": p.status,
                "members": [{"account_id": a, "name": h.name(a)} for a in p.members if h.person(a) is not None],
                "budget": dict(p.budget) if p.budget else None,
                "milestones": [dict(m) for m in p.milestones],
                "progress": {"done": states.count("done"), "total": len(states), "overdue": states.count("overdue")},
                "tasks": [self.task_json(h, t, today, project=p, hide_overdue=hide_overdue) for t in tasks],
                "created_at": _iso(p.created_at), "updated_at": _iso(p.updated_at)}

    def _all(self, h: Home) -> list[tuple[Project, list[Task]]]:
        def read() -> list[tuple[Project, list[Task]]]:
            projects = self.store.projects(h.household_id)
            tasks = self.store.tasks(h.household_id)
            by: dict[str, list[Task]] = {}
            for t in tasks:
                by.setdefault(t.project_id, []).append(t)
            return [(p, by.get(p.project_id, [])) for p in projects]

        out = []
        for p, ts in self._in_home(h, read):
            seen = self._visible(h, p, ts)
            if seen is not None:
                out.append((p, seen))
        return out

    def settings(self) -> Settings:
        h = self.home()
        return self._in_home(h, lambda: self.store.settings(h.household_id))

    def put_settings(self, body: dict[str, Any]) -> Settings:
        h = self.adult_home()
        s = self._in_home(h, lambda: self.store.settings(h.household_id))
        if "overdue_by_member" in body:
            v = body.get("overdue_by_member")
            if not isinstance(v, bool):
                raise ProjectError("overdue_by_member : vrai ou faux")
            s.overdue_by_member = v
        if "wall_ha" in body:  # mur F1 : entités Home Assistant montrées (jamais serrure, alarme, garage, portail)
            from nokker.household.projects.wall import MAX_HA, ha_entity_ok

            v = body.get("wall_ha")
            if not isinstance(v, list) or len(v) > MAX_HA or not all(isinstance(x, str) for x in v):
                raise ProjectError(f"wall_ha : une liste d'au plus {MAX_HA} entités")
            bad = [x for x in v if not ha_entity_ok(x)]
            if bad:
                raise ProjectError("wall_ha : entité refusée sur le mur (serrure, alarme, garage ou portail, ou nom "
                                   "illisible) : " + ", ".join(bad[:5]))
            s.wall_ha = list(dict.fromkeys(v))
        if "wall_place" in body:  # mur F1 : lieu du foyer pour la météo et les départs (pack Essentiels)
            s.wall_place = _wall_place(body.get("wall_place"))
        self._in_home(h, lambda: self.store.put_settings(s))
        return s

    def projects(self, *, status: str | None = "active") -> dict[str, Any]:
        """Vue commune (mur F1) : projets visibles, tâches visibles ; le nom de qui est en retard masqué par défaut."""
        h = self.home()
        today = self.today()
        hide = not self.settings().overdue_by_member
        rows = [self.project_json(h, p, ts, today, hide_overdue=hide) for p, ts in self._all(h)
                if status is None or p.status == status]
        return {"today": today.isoformat(), "overdue_names_shown": not hide, "projects": rows}

    def project(self, project_id: str) -> tuple[Home, Project, list[Task]]:
        h = self.home()
        found = self._in_home(h, lambda: (self.store.project(h.household_id, str(project_id or "")),
                                          self.store.tasks(h.household_id, str(project_id or ""))))
        p, ts = found
        seen = self._visible(h, p, ts) if p is not None else None
        if p is None or seen is None:
            raise not_found()
        return h, p, seen

    def member_tasks(self, member: str | None = None, due_before: str | None = None,
                     project_id: str | None = None, include_done: bool = False) -> dict[str, Any]:
        """Tâches d'un membre (ou de tout le foyer pour un adulte sans `member`), triées par échéance. Un mineur ne lit
        que les siennes : `member` désignant quelqu'un d'autre → 403."""
        h = self.home()
        today = self.today()
        who: Person | None = None
        if member:
            who = rv.find_person(str(member), list(h.people))
            if who is None:
                raise ProjectError("membre inconnu du foyer", 404)
        if h.me.minor:
            if who is not None and who.account_id != h.me.account_id:
                raise ProjectError("un profil mineur ne voit que ses propres tâches", 403)
            who = h.me
        limit = None
        if due_before:
            try:
                limit = date.fromisoformat(str(due_before)).isoformat()
            except ValueError:
                raise ProjectError("due_before : AAAA-MM-JJ") from None
        out = []
        for p, ts in self._all(h):
            if project_id and p.project_id != project_id:
                continue
            for t in ts:
                if who is not None and t.assignee != who.account_id:
                    continue
                if not include_done and t.status == "done":
                    continue
                if limit is not None and (not t.due or t.due >= limit):
                    continue
                out.append(self.task_json(h, t, today, project=p))
        out.sort(key=lambda x: (x["due"] or "9999", x["title"]))
        return {"today": today.isoformat(), "member": {"account_id": who.account_id, "name": who.name} if who else None,
                "tasks": out}

    def weekly(self) -> dict[str, Any]:
        """Point hebdo structuré (mur F1, outil) : avancement par projet, échéances des 7 jours, retards. Aucun
        classement ; noms des retards seulement si le réglage `overdue_by_member` est posé par un adulte."""
        h = self.home()
        today = self.today()
        end = (today + timedelta(days=UPCOMING_DAYS - 1)).isoformat()
        since = self.clock() - 7 * 86400
        s = self.settings()
        hide = not s.overdue_by_member
        projects, due, late = [], [], []
        done_week = 0
        for p, ts in self._all(h):
            if p.status != "active":
                continue
            states = [self.state(t, today) for t in ts]
            projects.append({"id": p.project_id, "title": p.title, "deadline": p.deadline,
                             "days_left": (date.fromisoformat(p.deadline) - today).days if p.deadline else None,
                             "progress": {"done": states.count("done"), "total": len(states),
                                          "overdue": states.count("overdue")}})
            for t in ts:
                st = self.state(t, today)
                if st == "todo" and t.due and t.due <= end:
                    due.append(self.task_json(h, t, today, project=p))
                elif st == "overdue":
                    late.append(self.task_json(h, t, today, project=p, hide_overdue=hide))
                elif st == "done" and t.done_at is not None and t.done_at >= since:
                    done_week += 1
        due.sort(key=lambda x: (x["due"], x["title"]))
        late.sort(key=lambda x: (x["due"], x["title"]))
        return {"week": {"from": today.isoformat(), "to": end}, "projects": projects, "due_this_week": due,
                "overdue": late, "done_last_7_days": done_week, "overdue_names_shown": not hide,
                "replan_hint": bool(late) and not h.me.minor}

    # ─────────── coche, relance ───────────

    def _own_or_adult_task(self, h: Home, task_id: str) -> tuple[Project, Task]:
        def read() -> tuple[Task | None, Project | None]:
            t = self.store.task(h.household_id, str(task_id or ""))
            return t, (self.store.project(h.household_id, t.project_id) if t else None)

        t, p = self._in_home(h, read)
        if t is None or p is None:
            raise not_found("tâche")
        if h.me.minor and (p.visibility == "parents" or t.assignee != h.me.account_id):
            raise not_found("tâche")  # jamais « elle existe mais pas pour toi »
        return p, t

    def set_done(self, task_id: str, done: bool = True) -> dict[str, Any]:
        """Coche (ou décoche) une tâche : écriture LOCALE au foyer, aucun effet externe, aucune carte."""
        h = self.home()
        p, _ = self._own_or_adult_task(h, task_id)
        t = self._in_home(h, lambda: self.store.set_done(h.household_id, str(task_id), bool(done), h.me.account_id,
                                                         self.clock()))
        if t is None:
            raise not_found("tâche")
        logger.info("projets du foyer : tâche %s %s", t.task_id, "faite" if done else "rouverte")
        return self.task_json(h, t, self.today(), project=p)

    def nudge(self, task_id: str, message: str | None = None) -> dict[str, Any]:
        """Relance douce : une note posée sur la tâche, que l'assigné voit dans Nokker (ses tâches, le mur). Rien ne
        part hors du foyer ; le destinataire est TOUJOURS l'assigné de la tâche, jamais une adresse donnée."""
        h = self.adult_home()
        p, t = self._own_or_adult_task(h, task_id)
        if not t.assignee or h.person(t.assignee) is None:
            raise ProjectError("tâche sans assigné : rien à relancer", 409)
        if t.assignee == h.me.account_id:
            raise ProjectError("tâche de l'utilisateur lui-même : pas de relance", 409)
        if t.status != "todo":
            raise ProjectError("tâche déjà faite : pas de relance", 409)
        default = f"Petit rappel de {h.me.name} : « {t.title} »" + (f", prévu pour le {t.due}." if t.due else ".")
        text = inline(message, NUDGE_MAX) if message and str(message).strip() else inline(default, NUDGE_MAX * 2)
        done = self._in_home(h, lambda: self.store.nudge(h.household_id, t.task_id, h.me.account_id, text,
                                                         self.clock(), NUDGE_GAP_S))
        if done is None:
            raise ProjectError("déjà relancée il y a moins d'un jour", 409)
        logger.info("projets du foyer : relance de la tâche %s", t.task_id)
        return {"nudged": True, "to": h.name(t.assignee), "where": "dans Nokker (ses tâches et le mur du foyer)",
                "task": self.task_json(h, done, self.today(), project=p)}

    # ─────────── brouillons ───────────

    def _paper_lookup(self) -> Callable[[str], tuple[str, str] | None] | None:
        if self.paper is None:
            return None

        def look(doc_id: str) -> tuple[str, str] | None:
            try:
                doc = self.paper.document(doc_id)  # locataire de l'auteur : ses papiers seulement
            except ValueError:
                return None
            return (doc.title or doc.filename), ""

        return look

    def _review(self, h: Home, d: Draft) -> tuple[Header, list[Line]]:
        today = self.today()
        header = rv.review_header(d.header_raw, people=list(h.people), author=h.me.account_id, today=today,
                                  replan=d.kind == "replan")
        lines = [Line.from_json(x) for x in d.lines]
        rv.review(header, lines, people=list(h.people), today=today, paper_lookup=self._paper_lookup())
        return header, lines

    def propose(self, arguments: dict[str, Any]) -> Draft:
        h = self.adult_home()
        raw_lines = arguments.get("lines")
        if not isinstance(raw_lines, list) or not raw_lines:
            raise ProjectError("lines : une liste non vide de jalons et de tâches")
        if len(raw_lines) > rv.MAX_LINES:
            raise ProjectError(f"au plus {rv.MAX_LINES} lignes par plan")
        header_raw = {k: arguments.get(k) for k in ("title", "goal", "deadline", "deadline_source", "kind", "members",
                                                    "visibility", "budget") if k in arguments}
        now = self.clock()
        lines = [rv.line_from(x, i) for i, x in enumerate(raw_lines)]
        d = Draft(draft_id=self.new_id("pd_"), account_id=h.me.account_id, household_id=h.household_id,
                  kind="create", created_at=now, updated_at=now, header_raw=header_raw,
                  lines=[x.to_json() for x in lines])
        _, reviewed = self._review(h, d)
        d.lines = [x.to_json() for x in reviewed]
        self.store.create_draft(d)
        logger.info("projets du foyer : brouillon %s (%d ligne(s))", d.draft_id, len(lines))
        return d

    def replan(self, project_id: str, shift_days: Any = None) -> Draft:
        """Retard détecté : NOUVEAU brouillon (tâches encore à faire du projet, celles en retard déplacées à
        aujourd'hui + `shift_days`), donc une nouvelle carte. Rien n'est modifié avant la carte."""
        h = self.adult_home()
        _, p, ts = self.project(project_id)
        try:
            shift = int(shift_days) if shift_days not in (None, "") else REPLAN_SHIFT_DEFAULT
        except (TypeError, ValueError):
            raise ProjectError("shift_days : un nombre de jours (1 à 60)") from None
        if not 1 <= shift <= 60:
            raise ProjectError("shift_days : un nombre de jours (1 à 60)")
        today = self.today()
        new_due = (today + timedelta(days=shift)).isoformat()
        lines: list[Line] = []
        for t in ts:
            if t.status != "todo":
                continue
            ln = Line(index=len(lines), kind="task", title=t.title, due=t.due, assignee=t.assignee,
                      assignee_name=h.name(t.assignee), milestone=t.milestone, task_id=t.task_id,
                      original={"title": t.title, "due": t.due, "assignee": t.assignee},
                      document_id=t.document_id, document_title=t.document_title)
            if self.state(t, today) == "overdue":
                ln.due = new_due
            lines.append(ln)
        if not lines:
            raise ProjectError("aucune tâche à faire dans ce projet : rien à replanifier", 409)
        header_raw = {"title": p.title, "goal": p.goal, "deadline": p.deadline, "kind": p.kind,
                      "members": [a for a in p.members if h.person(a) is not None], "visibility": p.visibility,
                      "budget": dict(p.budget) if p.budget else None}
        now = self.clock()
        d = Draft(draft_id=self.new_id("pd_"), account_id=h.me.account_id, household_id=h.household_id, kind="replan",
                  created_at=now, updated_at=now, project_id=p.project_id, header_raw=header_raw,
                  lines=[x.to_json() for x in lines])
        _, reviewed = self._review(h, d)
        d.lines = [x.to_json() for x in reviewed]
        self.store.create_draft(d)
        logger.info("projets du foyer : replanification %s du projet %s", d.draft_id, p.project_id)
        return d

    def draft(self, draft_id: Any) -> Draft:
        h = self.adult_home()
        d = self.store.get_draft(str(draft_id or "")) if draft_id else None
        if d is None or d.account_id != h.me.account_id or d.household_id != h.household_id:
            raise not_found("brouillon")
        return d

    def reviewed(self, d: Draft) -> tuple[Header, list[Line]]:
        return self._review(self.adult_home(), d)

    def edit(self, draft_id: Any, line_numbers: Any, action: str, fields: dict[str, Any] | None = None) -> Draft:
        d = self.draft(draft_id)
        h = self.adult_home()
        if d.status != "open":
            raise ProjectError("brouillon déjà écrit : rien à modifier", 409)
        if action not in ACTIONS:
            raise ProjectError(f"action : une de {', '.join(ACTIONS)}")
        fields = fields if isinstance(fields, dict) else {}
        lines = [Line.from_json(x) for x in d.lines]
        if action == "header":
            allowed = {"title", "goal", "deadline", "deadline_source", "kind", "members", "visibility", "budget"}
            if not fields or set(fields) - allowed:
                raise ProjectError(f"header : champs parmi {', '.join(sorted(allowed))}")
            d.header_raw = {**d.header_raw, **fields}
        elif action == "add":
            if len(lines) >= rv.MAX_LINES:
                raise ProjectError(f"au plus {rv.MAX_LINES} lignes par plan")
            if not fields:
                raise ProjectError("add : les champs de la nouvelle ligne (fields)")
            lines.append(rv.line_from(fields, len(lines)))
        else:
            nums = line_numbers if isinstance(line_numbers, list) else [line_numbers]
            try:
                idx = sorted({int(n) - 1 for n in nums})
            except (TypeError, ValueError):
                raise ProjectError("line : numéro de ligne (1 à n)") from None
            if not idx or any(not 0 <= i < len(lines) for i in idx):
                raise ProjectError(f"line : numéro de ligne entre 1 et {len(lines)}")
            if action == "update" and (len(idx) != 1 or not fields):
                raise ProjectError("update : une seule ligne et des champs à corriger (fields)")
            for i in idx:
                ln = lines[i]
                if action == "reject":
                    ln.rejected = True
                elif action == "restore":
                    ln.rejected = False
                else:
                    for k in ("title", "due", "due_source", "milestone", "kind", "document_id"):
                        if k in fields:
                            setattr(ln, k, rv.clean(fields[k], 200 if k == "due_source" else 120))
                    if "assignee" in fields:
                        ln.assignee, ln.assignee_name = "", rv.clean(fields["assignee"], 60)
        _, reviewed = self._review(h, Draft(**{**d.__dict__, "lines": [x.to_json() for x in lines]}))
        expected = d.revision
        d.lines, d.revision, d.updated_at = [x.to_json() for x in reviewed], d.revision + 1, self.clock()
        if not self.store.save_draft(d, expected):
            raise ProjectError("brouillon modifié en même temps : relire et recommencer", 409)
        return d

    def card(self, draft_id: Any, revision: Any) -> tuple[Draft, Header, list[Line]]:
        """(brouillon, en-tête relu, lignes relues) pour la carte ; lève si la révision n'est pas la bonne."""
        d = self.draft(draft_id)
        if d.status != "open":
            raise ProjectError("brouillon déjà écrit", 409)
        try:
            rev = int(revision)
        except (TypeError, ValueError):
            raise ProjectError("revision : entier rendu par family_project_propose ou family_project_edit") from None
        if rev != d.revision:
            raise ProjectError(f"le brouillon est en révision {d.revision} (pas {rev}) : rappeler avec revision "
                               f"{d.revision}", 409)
        header, lines = self.reviewed(d)
        return d, header, lines

    @staticmethod
    def committable(d: Draft, header: Header, lines: list[Line]) -> str | None:
        """None si la carte peut écrire ; sinon la raison (rien ne sera écrit)."""
        if header.problems:
            return "en-tête à corriger : " + "; ".join(header.problems)
        if not any(x.writable for x in lines):
            return "aucune ligne valide" if d.kind == "create" else "aucune modification valide"
        if d.kind == "create" and not any(x.writable and x.kind == "task" for x in lines):
            return "aucune tâche valide (un projet a au moins une tâche)"
        return None

    def commit(self, draft_id: Any, revision: Any) -> dict[str, Any]:
        """Écrit le plan. Appelé SEULEMENT sous une carte approuvée (outil, `ctx.approval_id`)."""
        d, header, lines = self.card(draft_id, revision)
        h = self.adult_home()
        why = self.committable(d, header, lines)
        not_written = [{"line": x.index + 1, "title": x.title, "why": x.why_not()} for x in lines
                       if not x.writable and x.status != "same"]
        if why:
            return {"written": 0, "not_written": not_written, "message": f"{why} : rien n'est écrit"}
        now = self.clock()
        ok = [x for x in lines if x.writable]
        expected = d.revision
        d.status, d.updated_at = "committed", now
        if not self.store.save_draft(d, expected):  # réservation : une seule écriture par révision
            raise ProjectError("brouillon modifié en même temps : rien n'est écrit", 409)
        try:
            if d.kind == "create":
                result = self._write_new(h, d, header, ok, now)
            else:
                result = self._write_replan(h, d, header, ok, now)
        except Exception:
            self.store.reopen_draft(d.draft_id, d.revision)
            raise
        logger.info("projets du foyer : brouillon %s écrit (projet %s)", d.draft_id, result.get("project_id"))
        return {**result, "not_written": not_written}

    def _write_new(self, h: Home, d: Draft, header: Header, ok: list[Line], now: float) -> dict[str, Any]:
        pid = self.new_id("hp_")
        milestones = [{"title": x.title, "due": x.due} for x in ok if x.kind == "milestone"]
        p = Project(project_id=pid, household_id=h.household_id, created_by=h.me.account_id, title=header.title,
                    deadline=header.deadline, created_at=now, updated_at=now, goal=header.goal, kind=header.kind,
                    members=list(header.members), visibility=header.visibility, budget=header.budget,
                    milestones=milestones)
        tasks = [Task(task_id=self.new_id("ht_"), household_id=h.household_id, project_id=pid, title=x.title,
                      assignee=x.assignee, due=x.due, created_at=now, updated_at=now, milestone=x.milestone,
                      document_id=x.document_id if x.document_title else "", document_title=x.document_title)
                 for x in ok if x.kind == "task"]
        self._in_home(h, lambda: self.store.insert_plan(p, tasks))
        return {"project_id": pid, "written": len(tasks) + len(milestones), "tasks": len(tasks),
                "milestones": len(milestones)}

    def _write_replan(self, h: Home, d: Draft, header: Header, ok: list[Line], now: float) -> dict[str, Any]:
        p = self._in_home(h, lambda: self.store.project(h.household_id, d.project_id))
        if p is None:
            raise not_found()
        p.title, p.goal, p.deadline, p.kind = header.title, header.goal, header.deadline, header.kind
        p.members, p.visibility, p.budget, p.updated_at = list(header.members), header.visibility, header.budget, now
        p.milestones = [*p.milestones, *({"title": x.title, "due": x.due} for x in ok if x.kind == "milestone")]
        updates = [Task(task_id=x.task_id, household_id=h.household_id, project_id=p.project_id, title=x.title,
                        assignee=x.assignee, due=x.due, created_at=now, updated_at=now)
                   for x in ok if x.kind == "task" and x.task_id]
        new = [Task(task_id=self.new_id("ht_"), household_id=h.household_id, project_id=p.project_id, title=x.title,
                    assignee=x.assignee, due=x.due, created_at=now, updated_at=now, milestone=x.milestone)
               for x in ok if x.kind == "task" and not x.task_id]
        skipped = self._in_home(h, lambda: self.store.apply_replan(p, updates, new))
        return {"project_id": p.project_id, "written": len(updates) - len(skipped) + len(new),
                "updated": len(updates) - len(skipped), "added": len(new),
                "skipped_done_meanwhile": len(skipped)}

    def drafts(self) -> list[Draft]:
        h = self.adult_home()
        return [d for d in self.store.drafts(h.me.account_id) if d.household_id == h.household_id]

    def draft_json(self, d: Draft) -> dict[str, Any]:
        h = self.adult_home()
        header, lines = self._review(h, d)
        return {"id": d.draft_id, "kind": d.kind, "revision": d.revision, "status": d.status,
                "project_id": d.project_id or None,
                "header": {k: v for k, v in header.to_json().items() if k != "members"}
                | {"members": [{"account_id": a, "name": h.name(a)} for a in header.members]},
                "lines": [{**x.to_json(), "number": x.index + 1, "status": x.status, "text": x.text(),
                           "why_not": x.why_not()} for x in lines],
                "committable": self.committable(d, header, lines) is None,
                "created_at": _iso(d.created_at), "updated_at": _iso(d.updated_at)}

    # ─────────── lien optionnel : agenda (par le calendrier familial) ───────────

    def to_calendar(self, project_id: str) -> Any:
        """Échéances à venir du projet → BROUILLON du calendrier familial (sa relecture, SA carte `family:calendar`).
        Rien n'est écrit dans l'agenda ici."""
        if self.calendar is None:
            raise ProjectError("le calendrier familial n'est pas allumé sur ce compte", 409)
        h = self.adult_home()
        _, p, ts = self.project(project_id)
        today = self.today().isoformat()
        events = []
        items = [(m["title"], m.get("due", ""), "") for m in p.milestones]
        items += [(t.title, t.due, h.name(t.assignee)) for t in ts if t.status == "todo"]
        for title, due, who in items:
            if not due or due < today:
                continue
            dd = date.fromisoformat(due)
            label = f"{p.title} : {title}" + (f" ({who})" if who else "")
            events.append({"title": label[:120], "date": due,
                           "source": f"{label[:80]} : {dd.day} {_MONTHS_FR[dd.month - 1]} {dd.year}"})
        if not events:
            raise ProjectError("aucune échéance à venir dans ce projet", 409)
        d = self.calendar.propose({"events": events[:60]}, None)
        d.origin = f"projet du foyer « {inline(p.title, rv.MAX_SHOWN)} »"
        self.calendar.store.save(d, d.revision)  # libellé d'origine seulement, même révision
        return d

    # ─────────── crochet du foyer ───────────

    def member_gone(self, household_id: str, tenant_id: str, account_id: str) -> int:
        """Un membre quitte le foyer : ses tâches perdent leur assigné (à réassigner), ses relances partent."""
        with tenant_scope(tenant_id):
            n = self.store.unassign(household_id, account_id, self.clock())
        logger.info("projets du foyer : %d tâche(s) désassignée(s) au départ d'un membre", n)
        return n


__all__ = ["ACTIONS", "FamilyProjects", "Home", "ProjectError", "adults_only", "no_home", "not_found"]
