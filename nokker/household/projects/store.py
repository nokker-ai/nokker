"""Magasin des projets du foyer (drapeau `family_projects`, migration 084, docs/FAMILY-PROJECTS.md §3).

Deux locataires, deux usages :

- **Brouillons** (`household_project_drafts`) : dans le locataire de l'AUTEUR (un compte = un locataire), comme ceux du
  calendrier familial. Personne d'autre ne les lit ; l'effacement du compte les emporte par `tenant_id`.
- **Projets, tâches, réglages** (`household_projects`, `household_tasks`, `household_project_settings`) : dans le
  locataire du FOYER (celui du payeur, `HouseholdMember.tenant_id`). Le service y entre par `tenant_scope` pour un
  membre du foyer et vérifie les droits à chaque appel (un mineur ne voit que ses tâches, jamais un projet réservé aux
  parents). RLS forcée, politique `tenant_isolation` : un autre foyer ne lit rien.

Écritures concurrentes :
- `save_draft(d, expected)` n'écrit que si la révision rangée est encore `expected` ET le brouillon ouvert
  (comparer-et-poser) ; c'est ce qui rend caduque une carte posée sur une révision dépassée ;
- `insert_plan` / `apply_replan` : une transaction ; une tâche cochée entre-temps n'est jamais replanifiée.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from typing import Any

from nokker.tenancy.context import current_tenant

TASK_STATUSES = ("todo", "done")


@dataclass
class Draft:
    draft_id: str
    account_id: str
    household_id: str
    kind: str  # create | replan
    created_at: float
    updated_at: float
    revision: int = 1
    status: str = "open"  # open | committed
    project_id: str = ""  # replan : projet visé ; create : rempli à l'écriture
    header_raw: dict[str, Any] = field(default_factory=dict)
    lines: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class Project:
    project_id: str
    household_id: str
    created_by: str
    title: str
    deadline: str
    created_at: float
    updated_at: float
    goal: str = ""
    kind: str = "autre"
    members: list[str] = field(default_factory=list)
    visibility: str = "household"  # household | parents
    budget: dict[str, str] | None = None
    milestones: list[dict[str, str]] = field(default_factory=list)  # [{title, due}]
    status: str = "active"  # active | done | archived


@dataclass
class Task:
    task_id: str
    household_id: str
    project_id: str
    title: str
    assignee: str
    due: str
    created_at: float
    updated_at: float
    milestone: str = ""
    document_id: str = ""
    document_title: str = ""
    status: str = "todo"  # todo | done ; « en retard » est calculé (échéance passée, pas fait)
    done_at: float | None = None
    done_by: str = ""
    nudged_at: float | None = None
    nudged_by: str = ""
    nudge_text: str = ""


@dataclass
class Settings:
    household_id: str
    overdue_by_member: bool = False  # nom de qui est en retard dans les vues communes (mur, point hebdo)
    guest_until: float = 0.0  # mur F1 : mode invité jusqu'à (epoch) ; 0 = éteint
    shopping_done: list[str] = field(default_factory=list)  # mur F1 : ids `sh_…` cochés
    wall_ha: list[str] = field(default_factory=list)  # mur F1 : entités Home Assistant choisies
    wall_place: dict[str, Any] | None = None  # mur F1 : {label, lat, lon, stops: [id]} (météo, départs)


def _cp(x: Any) -> Any:
    if isinstance(x, Draft):
        return replace(x, header_raw=dict(x.header_raw), lines=[dict(v) for v in x.lines], result=dict(x.result))
    if isinstance(x, Project):
        return replace(x, members=list(x.members), milestones=[dict(m) for m in x.milestones],
                       budget=dict(x.budget) if x.budget else None)
    return replace(x)


class MemoryProjectStore:
    def __init__(self) -> None:
        self._drafts: dict[str, dict[str, Draft]] = {}
        self._projects: dict[str, dict[str, Project]] = {}
        self._tasks: dict[str, dict[str, Task]] = {}
        self._settings: dict[str, dict[str, Settings]] = {}
        self._lock = threading.RLock()

    def _t(self, table: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return table.setdefault(current_tenant(), {})

    # ── brouillons (locataire de l'auteur) ──
    def create_draft(self, d: Draft) -> None:
        with self._lock:
            self._t(self._drafts)[d.draft_id] = _cp(d)

    def get_draft(self, draft_id: str) -> Draft | None:
        with self._lock:
            d = self._t(self._drafts).get(draft_id)
            return _cp(d) if d else None

    def save_draft(self, d: Draft, expected: int) -> bool:
        with self._lock:
            cur = self._t(self._drafts).get(d.draft_id)
            if cur is None or cur.revision != expected or cur.status != "open" or cur.account_id != d.account_id:
                return False
            self._t(self._drafts)[d.draft_id] = _cp(d)
            return True

    def reopen_draft(self, draft_id: str, revision: int) -> None:
        with self._lock:
            cur = self._t(self._drafts).get(draft_id)
            if cur is not None and cur.revision == revision:
                cur.status, cur.result = "open", {}

    def drafts(self, account_id: str) -> list[Draft]:
        with self._lock:
            rows = [_cp(d) for d in self._t(self._drafts).values() if d.account_id == account_id and d.status == "open"]
        return sorted(rows, key=lambda d: -d.created_at)

    # ── projets et tâches (locataire du foyer) ──
    def projects(self, household_id: str) -> list[Project]:
        with self._lock:
            rows = [_cp(p) for p in self._t(self._projects).values() if p.household_id == household_id]
        return sorted(rows, key=lambda p: (p.deadline, p.created_at))

    def project(self, household_id: str, project_id: str) -> Project | None:
        with self._lock:
            p = self._t(self._projects).get(project_id)
            return _cp(p) if p and p.household_id == household_id else None

    def tasks(self, household_id: str, project_id: str | None = None) -> list[Task]:
        with self._lock:
            rows = [_cp(t) for t in self._t(self._tasks).values() if t.household_id == household_id
                    and (project_id is None or t.project_id == project_id)]
        return sorted(rows, key=lambda t: (t.due, t.created_at, t.task_id))

    def task(self, household_id: str, task_id: str) -> Task | None:
        with self._lock:
            t = self._t(self._tasks).get(task_id)
            return _cp(t) if t and t.household_id == household_id else None

    def insert_plan(self, project: Project, tasks: list[Task]) -> None:
        with self._lock:
            self._t(self._projects)[project.project_id] = _cp(project)
            for t in tasks:
                self._t(self._tasks)[t.task_id] = _cp(t)

    def apply_replan(self, project: Project, updates: list[Task], new: list[Task]) -> list[str]:
        """Remplace le projet, met à jour les tâches ENCORE à faire, ajoute les nouvelles. Rend les ids sautés."""
        skipped: list[str] = []
        with self._lock:
            self._t(self._projects)[project.project_id] = _cp(project)
            for t in updates:
                cur = self._t(self._tasks).get(t.task_id)
                if cur is None or cur.status != "todo" or cur.project_id != project.project_id:
                    skipped.append(t.task_id)
                    continue
                cur.title, cur.due, cur.assignee, cur.updated_at = t.title, t.due, t.assignee, t.updated_at
            for t in new:
                self._t(self._tasks)[t.task_id] = _cp(t)
        return skipped

    def set_done(self, household_id: str, task_id: str, done: bool, by: str, now: float) -> Task | None:
        with self._lock:
            t = self._t(self._tasks).get(task_id)
            if t is None or t.household_id != household_id:
                return None
            t.status, t.done_at, t.done_by, t.updated_at = ("done", now, by, now) if done else ("todo", None, "", now)
            return _cp(t)

    def nudge(self, household_id: str, task_id: str, by: str, text: str, now: float, min_gap: float) -> Task | None:
        with self._lock:
            t = self._t(self._tasks).get(task_id)
            if t is None or t.household_id != household_id or t.status != "todo":
                return None
            if t.nudged_at is not None and now - t.nudged_at < min_gap:
                return None
            t.nudged_at, t.nudged_by, t.nudge_text, t.updated_at = now, by, text, now
            return _cp(t)

    def unassign(self, household_id: str, account_id: str, now: float) -> int:
        n = 0
        with self._lock:
            for t in self._t(self._tasks).values():
                if t.household_id == household_id and t.assignee == account_id:
                    t.assignee, t.nudged_at, t.nudged_by, t.nudge_text, t.updated_at = "", None, "", "", now
                    n += 1
            for p in self._t(self._projects).values():
                if p.household_id == household_id and account_id in p.members:
                    p.members = [m for m in p.members if m != account_id]
        return n

    def settings(self, household_id: str) -> Settings:
        with self._lock:
            s = self._t(self._settings).get(household_id)
            return replace(s, shopping_done=list(s.shopping_done), wall_ha=list(s.wall_ha),
                           wall_place=dict(s.wall_place) if s.wall_place else None) if s \
                else Settings(household_id=household_id)

    def put_settings(self, s: Settings) -> None:
        with self._lock:
            self._t(self._settings)[s.household_id] = replace(s, shopping_done=list(s.shopping_done),
                                                              wall_ha=list(s.wall_ha))


_D_COLS = ("draft_id, account_id, household_id, kind, created_at, updated_at, revision, status, project_id, "
           "header_raw, lines, result")
_P_COLS = ("project_id, household_id, created_by, title, deadline, created_at, updated_at, goal, kind, members, "
           "visibility, budget, milestones, status")
_T_COLS = ("task_id, household_id, project_id, title, assignee, due, created_at, updated_at, milestone, document_id, "
           "document_title, status, done_at, done_by, nudged_at, nudged_by, nudge_text")


class PgProjectStore:
    """Tables de la migration 084, RLS forcée par locataire (`tenant_isolation`), une connexion courte par opération."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):
        from nokker.store.conn import connect

        return connect(self.dsn)

    @staticmethod
    def _draft(r: Any) -> Draft:
        return Draft(draft_id=r[0], account_id=r[1], household_id=r[2], kind=r[3], created_at=float(r[4]),
                     updated_at=float(r[5]), revision=int(r[6]), status=r[7], project_id=r[8] or "",
                     header_raw=dict(r[9] or {}), lines=list(r[10] or []), result=dict(r[11] or {}))

    @staticmethod
    def _project(r: Any) -> Project:
        return Project(project_id=r[0], household_id=r[1], created_by=r[2], title=r[3], deadline=r[4],
                       created_at=float(r[5]), updated_at=float(r[6]), goal=r[7] or "", kind=r[8] or "autre",
                       members=list(r[9] or []), visibility=r[10], budget=dict(r[11]) if r[11] else None,
                       milestones=list(r[12] or []), status=r[13])

    @staticmethod
    def _task(r: Any) -> Task:
        return Task(task_id=r[0], household_id=r[1], project_id=r[2], title=r[3], assignee=r[4] or "", due=r[5] or "",
                    created_at=float(r[6]), updated_at=float(r[7]), milestone=r[8] or "", document_id=r[9] or "",
                    document_title=r[10] or "", status=r[11], done_at=float(r[12]) if r[12] is not None else None,
                    done_by=r[13] or "", nudged_at=float(r[14]) if r[14] is not None else None,
                    nudged_by=r[15] or "", nudge_text=r[16] or "")

    # ── brouillons ──
    def create_draft(self, d: Draft) -> None:
        from psycopg.types.json import Jsonb

        with self._conn() as c:
            c.execute(f"INSERT INTO household_project_drafts ({_D_COLS}) VALUES ({', '.join(['%s'] * 12)})",
                      (d.draft_id, d.account_id, d.household_id, d.kind, d.created_at, d.updated_at, d.revision,
                       d.status, d.project_id, Jsonb(d.header_raw), Jsonb(d.lines), Jsonb(d.result)))

    def get_draft(self, draft_id: str) -> Draft | None:
        with self._conn() as c:
            r = c.execute(f"SELECT {_D_COLS} FROM household_project_drafts WHERE draft_id = %s", (draft_id,)).fetchone()
        return self._draft(r) if r else None

    def save_draft(self, d: Draft, expected: int) -> bool:
        from psycopg.types.json import Jsonb

        with self._conn() as c:
            cur = c.execute("UPDATE household_project_drafts SET updated_at = %s, revision = %s, status = %s, "
                            "project_id = %s, header_raw = %s, lines = %s, result = %s WHERE draft_id = %s "
                            "AND account_id = %s AND revision = %s AND status = 'open'",
                            (d.updated_at, d.revision, d.status, d.project_id, Jsonb(d.header_raw), Jsonb(d.lines),
                             Jsonb(d.result), d.draft_id, d.account_id, expected))
            return cur.rowcount == 1

    def reopen_draft(self, draft_id: str, revision: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE household_project_drafts SET status = 'open', result = '{}'::jsonb "
                      "WHERE draft_id = %s AND revision = %s", (draft_id, revision))

    def drafts(self, account_id: str) -> list[Draft]:
        with self._conn() as c:
            rows = c.execute(f"SELECT {_D_COLS} FROM household_project_drafts WHERE account_id = %s AND status = "
                             "'open' ORDER BY created_at DESC LIMIT 50", (account_id,)).fetchall()
        return [self._draft(r) for r in rows]

    # ── projets et tâches ──
    def projects(self, household_id: str) -> list[Project]:
        with self._conn() as c:
            rows = c.execute(f"SELECT {_P_COLS} FROM household_projects WHERE household_id = %s "
                             "ORDER BY deadline, created_at LIMIT 200", (household_id,)).fetchall()
        return [self._project(r) for r in rows]

    def project(self, household_id: str, project_id: str) -> Project | None:
        with self._conn() as c:
            r = c.execute(f"SELECT {_P_COLS} FROM household_projects WHERE household_id = %s AND project_id = %s",
                          (household_id, project_id)).fetchone()
        return self._project(r) if r else None

    def tasks(self, household_id: str, project_id: str | None = None) -> list[Task]:
        q = f"SELECT {_T_COLS} FROM household_tasks WHERE household_id = %s"
        args: list[Any] = [household_id]
        if project_id is not None:
            q += " AND project_id = %s"
            args.append(project_id)
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY due, created_at, task_id LIMIT 2000", args).fetchall()
        return [self._task(r) for r in rows]

    def task(self, household_id: str, task_id: str) -> Task | None:
        with self._conn() as c:
            r = c.execute(f"SELECT {_T_COLS} FROM household_tasks WHERE household_id = %s AND task_id = %s",
                          (household_id, task_id)).fetchone()
        return self._task(r) if r else None

    @staticmethod
    def _put_project(c: Any, p: Project) -> None:
        from psycopg.types.json import Jsonb

        c.execute(f"INSERT INTO household_projects ({_P_COLS}) VALUES ({', '.join(['%s'] * 14)}) "
                  "ON CONFLICT (project_id) DO UPDATE SET title = EXCLUDED.title, deadline = EXCLUDED.deadline, "
                  "updated_at = EXCLUDED.updated_at, goal = EXCLUDED.goal, kind = EXCLUDED.kind, "
                  "members = EXCLUDED.members, visibility = EXCLUDED.visibility, budget = EXCLUDED.budget, "
                  "milestones = EXCLUDED.milestones, status = EXCLUDED.status",
                  (p.project_id, p.household_id, p.created_by, p.title, p.deadline, p.created_at, p.updated_at, p.goal,
                   p.kind, Jsonb(p.members), p.visibility, Jsonb(p.budget) if p.budget else None, Jsonb(p.milestones),
                   p.status))

    @staticmethod
    def _put_task(c: Any, t: Task) -> None:
        c.execute(f"INSERT INTO household_tasks ({_T_COLS}) VALUES ({', '.join(['%s'] * 17)})",
                  (t.task_id, t.household_id, t.project_id, t.title, t.assignee, t.due, t.created_at, t.updated_at,
                   t.milestone, t.document_id, t.document_title, t.status, t.done_at, t.done_by, t.nudged_at,
                   t.nudged_by, t.nudge_text))

    def insert_plan(self, project: Project, tasks: list[Task]) -> None:
        with self._conn() as c:
            self._put_project(c, project)
            for t in tasks:
                self._put_task(c, t)

    def apply_replan(self, project: Project, updates: list[Task], new: list[Task]) -> list[str]:
        skipped: list[str] = []
        with self._conn() as c:
            self._put_project(c, project)
            for t in updates:
                cur = c.execute("UPDATE household_tasks SET title = %s, due = %s, assignee = %s, updated_at = %s "
                                "WHERE task_id = %s AND project_id = %s AND household_id = %s AND status = 'todo'",
                                (t.title, t.due, t.assignee, t.updated_at, t.task_id, project.project_id,
                                 project.household_id))
                if cur.rowcount != 1:
                    skipped.append(t.task_id)
            for t in new:
                self._put_task(c, t)
        return skipped

    def set_done(self, household_id: str, task_id: str, done: bool, by: str, now: float) -> Task | None:
        with self._conn() as c:
            r = c.execute(f"UPDATE household_tasks SET status = %s, done_at = %s, done_by = %s, updated_at = %s "
                          f"WHERE household_id = %s AND task_id = %s RETURNING {_T_COLS}",
                          ("done" if done else "todo", now if done else None, by if done else "", now, household_id,
                           task_id)).fetchone()
        return self._task(r) if r else None

    def nudge(self, household_id: str, task_id: str, by: str, text: str, now: float, min_gap: float) -> Task | None:
        with self._conn() as c:
            r = c.execute(f"UPDATE household_tasks SET nudged_at = %s, nudged_by = %s, nudge_text = %s, updated_at = %s "
                          "WHERE household_id = %s AND task_id = %s AND status = 'todo' "
                          f"AND (nudged_at IS NULL OR nudged_at <= %s) RETURNING {_T_COLS}",
                          (now, by, text, now, household_id, task_id, now - min_gap)).fetchone()
        return self._task(r) if r else None

    def unassign(self, household_id: str, account_id: str, now: float) -> int:
        with self._conn() as c:
            n = c.execute("UPDATE household_tasks SET assignee = '', nudged_at = NULL, nudged_by = '', nudge_text = '', "
                          "updated_at = %s WHERE household_id = %s AND assignee = %s",
                          (now, household_id, account_id)).rowcount
            c.execute("UPDATE household_projects SET members = members - %s WHERE household_id = %s "
                      "AND members ? %s", (account_id, household_id, account_id))
        return int(n)

    def settings(self, household_id: str) -> Settings:
        with self._conn() as c:
            r = c.execute("SELECT overdue_by_member, guest_until, shopping_done, wall_ha, wall_place "
                          "FROM household_project_settings WHERE household_id = %s", (household_id,)).fetchone()
        if not r:
            return Settings(household_id=household_id)
        return Settings(household_id=household_id, overdue_by_member=bool(r[0]), guest_until=float(r[1] or 0),
                        shopping_done=list(r[2] or []), wall_ha=list(r[3] or []),
                        wall_place=dict(r[4]) if r[4] else None)

    def put_settings(self, s: Settings) -> None:
        from psycopg.types.json import Jsonb

        with self._conn() as c:
            c.execute("INSERT INTO household_project_settings (household_id, overdue_by_member, guest_until, "
                      "shopping_done, wall_ha, wall_place) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id, "
                      "household_id) DO UPDATE SET overdue_by_member = EXCLUDED.overdue_by_member, guest_until = "
                      "EXCLUDED.guest_until, shopping_done = EXCLUDED.shopping_done, wall_ha = EXCLUDED.wall_ha, "
                      "wall_place = EXCLUDED.wall_place",
                      (s.household_id, s.overdue_by_member, s.guest_until, Jsonb(s.shopping_done), Jsonb(s.wall_ha),
                       Jsonb(s.wall_place) if s.wall_place else None))


__all__ = ["TASK_STATUSES", "Draft", "MemoryProjectStore", "PgProjectStore", "Project", "Settings", "Task"]
