"""Magasin des objectifs (docs/OUTCOMES-GOAL.md) : mémoire (tests, dev) et Postgres (migration 050, RLS par locataire).

Un objectif appartient à un fil et au locataire courant (`tenant_scope`) : la mémoire range par (locataire, id), le
Postgres par la politique `tenant_isolation`. Aucune lecture ne traverse les locataires, sauf `live()` du moteur, qui
passe sur chaque locataire connu DANS son `tenant_scope` (jamais une valeur joker dans la politique).
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any

from nokker.tenancy.context import INTERNAL_TENANT, current_tenant_or_none

STATUSES = ("draft", "active", "done", "budget", "blocked", "stopped")
LIVE = ("draft", "active", "blocked")  # objectif « courant » du fil (un seul à la fois)
DRIVERS = ("kernel", "client")
MODES = ("task", "goal")


@dataclass
class Goal:
    id: str
    thread_id: str
    owner: str
    text: str
    mode: str = "goal"  # task : rubrique de la tâche, passes dans le tour ; goal : objectif sur plusieurs tours
    driver: str = "kernel"  # kernel : le noyau enchaîne les tours ; client : la CLI les envoie (metadata.cli_control)
    status: str = "active"
    rubric: list[str] | None = None  # critères figés (notés) ; None tant qu'aucune rubrique n'est figée
    rubric_source: str = "user"  # user | agent
    proposed: list[str] | None = None  # rubrique proposée par l'agent, pas encore figée
    budget: dict[str, Any] = field(default_factory=dict)  # tokens, seconds, cost, currency, turns
    spent: dict[str, Any] = field(default_factory=lambda: {"tokens": 0, "cost": {}, "turns": 0, "auto_turns": 0})
    gradings: int = 0
    last_grade: dict[str, Any] | None = None
    recent_totals: list[int] = field(default_factory=list)  # dernières notes (détection d'absence de progrès)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)  # 20 derniers points d'étape
    checkpoint_every: int = 3  # notes entre deux points d'étape
    checkpoint_s: float = 1800.0
    last_checkpoint_at: float = 0.0
    stop_reason: str = ""
    channel: str = "api"
    start_index: int = 0  # premier tour du fil couvert par l'objectif (preuves)
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float = 0.0
    ended_at: float | None = None

    def to_doc(self) -> dict[str, Any]:
        return {k: copy.deepcopy(v) for k, v in self.__dict__.items()}

    @classmethod
    def from_doc(cls, d: dict[str, Any]) -> Goal:
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class MemoryGoals:
    def __init__(self) -> None:
        self._goals: dict[tuple[str, str], dict[str, Any]] = {}
        self._grades: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    @staticmethod
    def _tenant() -> str:
        return current_tenant_or_none() or INTERNAL_TENANT

    def save(self, g: Goal) -> None:
        with self._lock:
            self._goals[(self._tenant(), g.id)] = g.to_doc()

    def get(self, goal_id: str) -> Goal | None:
        with self._lock:
            d = self._goals.get((self._tenant(), goal_id))
        return Goal.from_doc(d) if d is not None else None

    def current(self, thread_id: str) -> Goal | None:
        """L'objectif courant du fil (statut vivant), le plus récent."""
        t = self._tenant()
        with self._lock:
            docs = [d for (tn, _), d in self._goals.items() if tn == t and d["thread_id"] == thread_id
                    and d["status"] in LIVE]
        docs.sort(key=lambda d: d["created_at"], reverse=True)
        return Goal.from_doc(docs[0]) if docs else None

    def latest(self, thread_id: str) -> Goal | None:
        t = self._tenant()
        with self._lock:
            docs = [d for (tn, _), d in self._goals.items() if tn == t and d["thread_id"] == thread_id]
        docs.sort(key=lambda d: d["created_at"], reverse=True)
        return Goal.from_doc(docs[0]) if docs else None

    def live_kernel(self) -> list[Goal]:
        t = self._tenant()
        with self._lock:
            return [Goal.from_doc(d) for (tn, _), d in self._goals.items()
                    if tn == t and d["status"] == "active" and d["driver"] == "kernel" and d["mode"] == "goal"]

    def add_grade(self, goal_id: str, thread_id: str, turn_index: int | None, pass_no: int, grade: dict[str, Any],
                  at: float) -> None:
        with self._lock:
            self._grades.append((self._tenant(), {"goal_id": goal_id, "thread_id": thread_id, "turn_index": turn_index,
                                                  "pass": pass_no, "grade": copy.deepcopy(grade), "at": at}))

    def grades(self, goal_id: str, limit: int = 50) -> list[dict[str, Any]]:
        t = self._tenant()
        with self._lock:
            rows = [copy.deepcopy(r) for tn, r in self._grades if tn == t and r["goal_id"] == goal_id]
        return rows[-limit:]

    def tenants(self) -> list[str]:
        with self._lock:
            return sorted({tn for tn, _ in self._goals})


class PgGoals:
    """Tables `nokker_goals` et `nokker_goal_grades` (migration 050), sous la RLS du locataire courant."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):
        from nokker.store.conn import connect

        return connect(self.dsn)

    def save(self, g: Goal) -> None:
        from psycopg.types.json import Jsonb

        with self._conn() as conn:
            conn.execute(
                "INSERT INTO nokker_goals (id, thread_id, owner, status, driver, doc, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id, id) DO UPDATE SET "
                "status = EXCLUDED.status, driver = EXCLUDED.driver, doc = EXCLUDED.doc, updated_at = EXCLUDED.updated_at",
                (g.id, g.thread_id, g.owner, g.status, g.driver, Jsonb(g.to_doc()), g.created_at, g.updated_at))

    def get(self, goal_id: str) -> Goal | None:
        with self._conn() as conn:
            r = conn.execute("SELECT doc FROM nokker_goals WHERE id = %s", (goal_id,)).fetchone()
        return Goal.from_doc(r[0]) if r else None

    def current(self, thread_id: str) -> Goal | None:
        with self._conn() as conn:
            r = conn.execute("SELECT doc FROM nokker_goals WHERE thread_id = %s AND status = ANY(%s) "
                             "ORDER BY created_at DESC LIMIT 1", (thread_id, list(LIVE))).fetchone()
        return Goal.from_doc(r[0]) if r else None

    def latest(self, thread_id: str) -> Goal | None:
        with self._conn() as conn:
            r = conn.execute("SELECT doc FROM nokker_goals WHERE thread_id = %s ORDER BY created_at DESC LIMIT 1",
                             (thread_id,)).fetchone()
        return Goal.from_doc(r[0]) if r else None

    def live_kernel(self) -> list[Goal]:
        with self._conn() as conn:
            rows = conn.execute("SELECT doc FROM nokker_goals WHERE status = 'active' AND driver = 'kernel' "
                                "ORDER BY created_at").fetchall()
        return [g for g in (Goal.from_doc(r[0]) for r in rows) if g.mode == "goal"]

    def add_grade(self, goal_id: str, thread_id: str, turn_index: int | None, pass_no: int, grade: dict[str, Any],
                  at: float) -> None:
        from psycopg.types.json import Jsonb

        with self._conn() as conn:
            conn.execute("INSERT INTO nokker_goal_grades (goal_id, thread_id, turn_index, pass, grade, at) "
                         "VALUES (%s, %s, %s, %s, %s, %s)", (goal_id, thread_id, turn_index, pass_no, Jsonb(grade), at))

    def grades(self, goal_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT goal_id, thread_id, turn_index, pass, grade, at FROM nokker_goal_grades "
                                "WHERE goal_id = %s ORDER BY seq DESC LIMIT %s", (goal_id, limit)).fetchall()
        return [{"goal_id": r[0], "thread_id": r[1], "turn_index": r[2], "pass": r[3], "grade": r[4], "at": r[5]}
                for r in reversed(rows)]


__all__ = ["DRIVERS", "LIVE", "MODES", "STATUSES", "Goal", "MemoryGoals", "PgGoals"]
