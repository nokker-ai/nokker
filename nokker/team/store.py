"""Stockage de l'équipe (lot X17) : `MemoryTeamStore` (dev, tests) et `PgTeamStore` (tables de
`store/migrations/023_team.sql`), même contrat. Les DROITS vivent dans `nokker/team/service.py`, pas ici.

Chaque méthode reçoit l'`actor` (le compte qui agit : `account_id`, `email`) : en Postgres, il devient `app.account_id` et
`app.account_email` de la connexion, à côté de `app.tenant_id` (locataire de l'acteur, nokker/store/conn.py). La RLS
de 023 ouvre une ligne d'un projet à son locataire (le propriétaire) OU à un compte membre de ce projet, quel que
soit son locataire ; les écritures d'un membre d'un autre locataire sont bornées par son rôle, en base aussi.
Un membre ne passe donc JAMAIS dans le locataire du propriétaire : ses fils, tours et connecteurs restent hors de
portée, même par une erreur de code.
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

from nokker.team import model as m
from nokker.team.model import Invite, Member, Note, Project, Skill, TeamError


class Actor(Protocol):
    account_id: str
    email: str
    tenant_id: str


class TeamStore(Protocol):
    def memberships(self, actor: Actor) -> list[Member]: ...
    def member(self, actor: Actor, project_id: str) -> Member | None: ...
    def project(self, actor: Actor, project_id: str) -> Project | None: ...
    def owned_count(self, actor: Actor) -> int: ...
    def create_project(self, actor: Actor, project: Project, owner: Member) -> None: ...
    def save_project(self, actor: Actor, project: Project) -> None: ...
    def delete_project(self, actor: Actor, project: Project) -> None: ...
    def members(self, actor: Actor, project_id: str) -> list[Member]: ...
    def set_role(self, actor: Actor, project: Project, account_id: str, role: str) -> None: ...
    def add_member(self, actor: Actor, member: Member) -> None: ...
    def remove_member(self, actor: Actor, project: Project, account_id: str) -> bool: ...
    def create_invite(self, actor: Actor, inv: Invite) -> None: ...
    def invites_of(self, actor: Actor, project_id: str) -> list[Invite]: ...
    def invites_for(self, actor: Actor) -> list[Invite]: ...
    def invite(self, actor: Actor, invite_id: str) -> Invite | None: ...
    def accept(self, actor: Actor, inv: Invite, member: Member, now: float) -> None: ...
    def set_invite_state(self, actor: Actor, inv: Invite, state: str, now: float) -> None: ...
    def skills(self, actor: Actor, project_id: str) -> list[Skill]: ...
    def put_skill(self, actor: Actor, skill: Skill) -> None: ...
    def delete_skill(self, actor: Actor, project: Project, name: str) -> bool: ...
    def note(self, actor: Actor, project: Project, name: str) -> Note | None: ...
    def notes_after(self, actor: Actor, project: Project, seq: int, limit: int) -> list[Note]: ...
    def count_notes(self, actor: Actor, project: Project) -> int: ...
    def put_note(self, actor: Actor, note: Note) -> Note: ...
    def quarantine(self, actor: Actor, note: Note, reason: str, now: float) -> None: ...
    def quarantined(self, actor: Actor, project: Project) -> list[dict[str, Any]]: ...
    def write_note(self, actor: Actor, project: Project, note: Note, base_sha: str, now: float) -> str: ...
    def restore_note(self, actor: Actor, project: Project, quarantine_id: int, by: str, now: float) -> Note: ...
    def audit(self, actor: Actor, project: Project, event: str, detail: dict[str, Any], now: float) -> None: ...
    def audit_events(self, actor: Actor, project: Project, limit: int) -> list[dict[str, Any]]: ...


def _too_many() -> TeamError:
    return TeamError(409, "too_many", f"{m.MAX_NOTES} notes au plus par projet")


def _restored(q: dict[str, Any], project: Project, now: float) -> Note:
    """La version de la quarantaine `q`, redevenue la version courante (datée de l'horloge de nokker.ai)."""
    return Note(project_id=project.id, tenant_id=project.tenant_id, name=q["name"], kind=q["kind"], title=q["title"],
                text=q["text"], sha=m.note_sha(q["kind"], q["title"], q["text"], False), updated_at=now,
                author_account=q["author_account"], author_machine=q["author_machine"], origin=q.get("origin") or "",
                derived_from=q.get("derived_from") or "", links=list(q.get("links") or []), deleted=False)


def _check_restore(q: dict[str, Any] | None, cur: Note | None, live: int) -> None:
    if q is None:
        raise TeamError(404, "not_found", "version inconnue dans la quarantaine de ce projet")
    if cur is not None and not cur.deleted and cur.sha == m.note_sha(q["kind"], q["title"], q["text"], False):
        raise TeamError(409, "already_current", "cette version est déjà la version courante")
    if (cur is None or cur.deleted) and live >= m.MAX_NOTES:
        raise _too_many()


@dataclass
class MemoryTeamStore:
    projects: dict[str, Project] = field(default_factory=dict)
    access: dict[tuple[str, str], Member] = field(default_factory=dict)  # (projet, compte)
    invites: dict[str, Invite] = field(default_factory=dict)
    skill_rows: dict[tuple[str, str], Skill] = field(default_factory=dict)
    note_rows: dict[tuple[str, str], Note] = field(default_factory=dict)
    quarantine_rows: list[dict[str, Any]] = field(default_factory=list)
    audit_rows: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0
    _qseq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def memberships(self, actor: Actor) -> list[Member]:
        with self._lock:
            return [copy.copy(m) for (_, acc), m in self.access.items() if acc == actor.account_id]

    def member(self, actor: Actor, project_id: str) -> Member | None:
        with self._lock:
            m = self.access.get((project_id, actor.account_id))
            return copy.copy(m) if m else None

    def project(self, actor: Actor, project_id: str) -> Project | None:
        with self._lock:
            p = self.projects.get(project_id)
            return copy.deepcopy(p) if p else None

    def owned_count(self, actor: Actor) -> int:
        with self._lock:
            return sum(1 for p in self.projects.values() if p.owner_account == actor.account_id)

    def create_project(self, actor: Actor, project: Project, owner: Member) -> None:
        with self._lock:
            self.projects[project.id] = copy.deepcopy(project)
            self.access[(project.id, owner.account_id)] = copy.copy(owner)

    def save_project(self, actor: Actor, project: Project) -> None:
        with self._lock:
            self.projects[project.id] = copy.deepcopy(project)

    def delete_project(self, actor: Actor, project: Project) -> None:
        pid = project.id
        with self._lock:
            self.projects.pop(pid, None)
            for d in (self.access, self.skill_rows, self.note_rows):
                for k in [k for k in d if k[0] == pid]:
                    del d[k]
            for k in [k for k, v in self.invites.items() if v.project_id == pid]:
                del self.invites[k]
            self.quarantine_rows[:] = [q for q in self.quarantine_rows if q["project_id"] != pid]
            self.audit_rows[:] = [a for a in self.audit_rows if a["project_id"] != pid]

    def members(self, actor: Actor, project_id: str) -> list[Member]:
        with self._lock:
            return sorted((copy.copy(m) for (p, _), m in self.access.items() if p == project_id),
                          key=lambda m: (-_rank(m.role), m.added_at))

    def set_role(self, actor: Actor, project: Project, account_id: str, role: str) -> None:
        with self._lock:
            self.access[(project.id, account_id)].role = role

    def add_member(self, actor: Actor, member: Member) -> None:
        # Adhésion posée dans le locataire du PROJET sans invitation : seulement le SSO (nokker/sso/service.py), jamais
        # avec le rôle owner (refusé ici et en base, migration 032).
        if member.role == "owner":
            raise ValueError("owner : le créateur du projet seulement")
        with self._lock:
            self.access[(member.project_id, member.account_id)] = copy.copy(member)

    def remove_member(self, actor: Actor, project: Project, account_id: str) -> bool:
        with self._lock:
            return self.access.pop((project.id, account_id), None) is not None

    def create_invite(self, actor: Actor, inv: Invite) -> None:
        with self._lock:
            self.invites[inv.id] = copy.copy(inv)

    def invites_of(self, actor: Actor, project_id: str) -> list[Invite]:
        with self._lock:
            return sorted((copy.copy(i) for i in self.invites.values() if i.project_id == project_id),
                          key=lambda i: i.created_at)

    def invites_for(self, actor: Actor) -> list[Invite]:
        with self._lock:
            return sorted((copy.copy(i) for i in self.invites.values() if i.state == "pending" and _addressed(i, actor)),
                          key=lambda i: i.created_at)

    def invite(self, actor: Actor, invite_id: str) -> Invite | None:
        with self._lock:
            i = self.invites.get(invite_id)
            return copy.copy(i) if i else None

    def accept(self, actor: Actor, inv: Invite, member: Member, now: float) -> None:
        with self._lock:
            self.access[(member.project_id, member.account_id)] = copy.copy(member)
            i = self.invites[inv.id]
            i.state, i.decided_at, i.invitee_account = "accepted", now, member.account_id

    def set_invite_state(self, actor: Actor, inv: Invite, state: str, now: float) -> None:
        with self._lock:
            i = self.invites[inv.id]
            i.state, i.decided_at = state, now

    def skills(self, actor: Actor, project_id: str) -> list[Skill]:
        with self._lock:
            return sorted((copy.copy(s) for (p, _), s in self.skill_rows.items() if p == project_id),
                          key=lambda s: s.name)

    def put_skill(self, actor: Actor, skill: Skill) -> None:
        with self._lock:
            self.skill_rows[(skill.project_id, skill.name)] = copy.copy(skill)

    def delete_skill(self, actor: Actor, project: Project, name: str) -> bool:
        with self._lock:
            return self.skill_rows.pop((project.id, name), None) is not None

    def note(self, actor: Actor, project: Project, name: str) -> Note | None:
        with self._lock:
            n = self.note_rows.get((project.id, name))
            return copy.deepcopy(n) if n else None

    def notes_after(self, actor: Actor, project: Project, seq: int, limit: int) -> list[Note]:
        with self._lock:
            rows = sorted((n for (p, _), n in self.note_rows.items() if p == project.id and n.seq > seq),
                          key=lambda n: n.seq)
            return [copy.deepcopy(n) for n in rows[:limit]]

    def count_notes(self, actor: Actor, project: Project) -> int:
        with self._lock:
            return sum(1 for (p, _), n in self.note_rows.items() if p == project.id and not n.deleted)

    def put_note(self, actor: Actor, note: Note) -> Note:
        with self._lock:
            self._seq += 1
            n = copy.deepcopy(note)
            n.seq = self._seq
            self.note_rows[(n.project_id, n.name)] = n
            return copy.deepcopy(n)

    def _q_append(self, note: Note, reason: str, now: float) -> None:
        self._qseq += 1
        self.quarantine_rows.append({"id": self._qseq, "project_id": note.project_id, "name": note.name,
                                     "kind": note.kind, "title": note.title, "text": note.text, "sha": note.sha,
                                     "updated_at": note.updated_at, "author_account": note.author_account,
                                     "author_machine": note.author_machine, "origin": note.origin,
                                     "derived_from": note.derived_from, "links": list(note.links), "reason": reason,
                                     "quarantined_at": now, "restored_at": None, "restored_by": None})

    def quarantine(self, actor: Actor, note: Note, reason: str, now: float) -> None:
        with self._lock:
            self._q_append(note, reason, now)

    def quarantined(self, actor: Actor, project: Project) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(q) for q in self.quarantine_rows if q["project_id"] == project.id]

    def restore_note(self, actor: Actor, project: Project, quarantine_id: int, by: str, now: float) -> Note:
        """Sous le verrou : la version gardée redevient la courante, la courante part en quarantaine (rien n'est
        effacé), la ligne restaurée est marquée (`restored_at`, `restored_by`)."""
        with self._lock:
            q = next((x for x in self.quarantine_rows if x["id"] == quarantine_id and x["project_id"] == project.id),
                     None)
            cur = self.note_rows.get((project.id, q["name"])) if q is not None else None
            live = sum(1 for (p, _), x in self.note_rows.items() if p == project.id and not x.deleted)
            _check_restore(q, cur, live)
            if cur is not None and not cur.deleted:
                self._q_append(cur, m.REASON_RESTORED, now)
            n = _restored(q, project, now)  # type: ignore[arg-type]
            self._seq += 1
            n.seq = self._seq
            self.note_rows[(project.id, n.name)] = n
            q["restored_at"], q["restored_by"] = now, by  # type: ignore[index]
            return copy.deepcopy(n)

    def audit(self, actor: Actor, project: Project, event: str, detail: dict[str, Any], now: float) -> None:
        with self._lock:
            self.audit_rows.append({"id": len(self.audit_rows) + 1, "project_id": project.id, "at": now,
                                    "actor_account": actor.account_id, "event": event, "detail": copy.deepcopy(detail)})

    def audit_events(self, actor: Actor, project: Project, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = [copy.deepcopy(a) for a in self.audit_rows if a["project_id"] == project.id]
        return sorted(rows, key=lambda a: (-a["at"], -a["id"]))[:limit]

    def write_note(self, actor: Actor, project: Project, note: Note, base_sha: str, now: float) -> str:
        """Lecture, décision (`model.resolve`) et écriture d'un bloc, sous le verrou : deux machines qui écrivent la
        même note en même temps sont mises l'une après l'autre (la seconde reçue gagne, la première est gardée)."""
        with self._lock:
            cur = self.note_rows.get((project.id, note.name))
            outcome = m.resolve(cur, note, base_sha)
            if outcome == m.SKIP:
                return outcome
            if outcome == m.INSERT and sum(1 for (p, _), x in self.note_rows.items()
                                           if p == project.id and not x.deleted) >= m.MAX_NOTES:
                raise _too_many()
            if cur is not None and not cur.deleted and (outcome == m.CONFLICT or note.deleted):
                self._q_append(cur, m.REASON_CONFLICT if outcome == m.CONFLICT else m.REASON_FORGOTTEN, now)
            self._seq += 1
            n = copy.deepcopy(note)
            n.seq = self._seq
            self.note_rows[(n.project_id, n.name)] = n
            return outcome


def _rank(role: str) -> int:
    return {"viewer": 0, "member": 1, "maintainer": 2, "owner": 3}.get(role, -1)


def _addressed(inv: Invite, actor: Actor) -> bool:
    return inv.invitee_account == actor.account_id or (bool(inv.email) and inv.email == (actor.email or "").lower())


# ─────────────────────────────── Postgres ───────────────────────────────

_P_COLS = ("project_id, tenant_id, name, description, owner_account, repos, secret_names, budget, rules, mirror, "
           "created_at, updated_at, kind, household_policy")
_M_COLS = "project_id, tenant_id, account_id, role, email, added_by, added_at"
_I_COLS = ("invite_id, project_id, tenant_id, project_name, email, invitee_account, role, invited_by, state, "
           "created_at, expires_at, decided_at, kind, profile")
_S_COLS = "project_id, tenant_id, name, source, description, requires_local, added_by, added_at"
_N_COLS = ("project_id, tenant_id, name, kind, title, text, sha, updated_at, author_account, author_machine, origin, "
           "derived_from, links, deleted, seq")
_Q_COLS = ("project_id, tenant_id, name, kind, title, text, sha, updated_at, author_account, author_machine, origin, "
           "derived_from, links, reason, quarantined_at")


def _project(r: dict[str, Any]) -> Project:
    return Project(id=r["project_id"], tenant_id=r["tenant_id"], name=r["name"], description=r["description"],
                   owner_account=r["owner_account"], repos=list(r["repos"] or []),
                   secret_names=list(r["secret_names"] or []), budget=r["budget"], rules=dict(r["rules"] or {}),
                   mirror=bool(r["mirror"]), created_at=r["created_at"], updated_at=r["updated_at"],
                   kind=r.get("kind") or "team", household_policy=dict(r.get("household_policy") or {}))


def _member(r: dict[str, Any]) -> Member:
    return Member(project_id=r["project_id"], tenant_id=r["tenant_id"], account_id=r["account_id"], role=r["role"],
                  email=r["email"], added_by=r["added_by"], added_at=r["added_at"])


def _invite(r: dict[str, Any]) -> Invite:
    return Invite(id=r["invite_id"], project_id=r["project_id"], tenant_id=r["tenant_id"],
                  project_name=r["project_name"], email=r["email"], invitee_account=r["invitee_account"],
                  role=r["role"], invited_by=r["invited_by"], state=r["state"], created_at=r["created_at"],
                  expires_at=r["expires_at"], decided_at=r["decided_at"], kind=r.get("kind") or "team",
                  profile=r.get("profile") or "adult")


def _skill(r: dict[str, Any]) -> Skill:
    return Skill(project_id=r["project_id"], tenant_id=r["tenant_id"], name=r["name"], source=r["source"],
                 description=r["description"], requires_local=bool(r["requires_local"]), added_by=r["added_by"],
                 added_at=r["added_at"])


def _note(r: dict[str, Any]) -> Note:
    return Note(project_id=r["project_id"], tenant_id=r["tenant_id"], name=r["name"], kind=r["kind"],
                title=r["title"], text=r["text"], sha=r["sha"], updated_at=r["updated_at"],
                author_account=r["author_account"], author_machine=r["author_machine"], origin=r["origin"],
                derived_from=r["derived_from"], links=list(r["links"] or []), deleted=bool(r["deleted"]),
                seq=int(r["seq"]))


class PgTeamStore:
    """Une connexion courte par opération (une transaction) : `app.tenant_id` = locataire courant (`connect`), puis
    `app.account_id` / `app.account_email` = l'acteur. Rien n'est mis en cache entre deux acteurs. Les lectures d'un
    projet se limitent aussi au locataire DU PROJET : une ligne glissée dans le locataire du membre (SQL brut, la
    `tenant_isolation` de ce membre l'accepterait) ne se mêle jamais au projet."""

    def __init__(self, dsn: str, *, migrate_now: bool = True) -> None:
        if not dsn:
            raise ValueError("PgTeamStore requiert un DSN Postgres")
        self.dsn = dsn
        if migrate_now:
            from pathlib import Path

            from nokker.store.conn import migrate

            mig = Path(__file__).parent.parent / "store" / "migrations"
            migrate(dsn, *((mig / f).read_text(encoding="utf-8")
                           for f in ("023_team.sql", "037_team_budget_brief.sql", "039_household.sql")))

    def _conn(self, actor: Actor):
        from psycopg.rows import dict_row

        from nokker.store.conn import connect

        conn = connect(self.dsn, row_factory=dict_row)
        try:
            conn.execute("SELECT set_config('app.account_id', %s, false), set_config('app.account_email', %s, false)",
                         (actor.account_id, (actor.email or "").lower()))
            conn.commit()
        except BaseException:
            conn.close()
            raise
        return conn

    def memberships(self, actor: Actor) -> list[Member]:
        with self._conn(actor) as c:
            rows = c.execute(f"SELECT {_M_COLS} FROM team_access WHERE account_id = %s ORDER BY added_at",
                             (actor.account_id,)).fetchall()
        return [_member(r) for r in rows]

    def member(self, actor: Actor, project_id: str) -> Member | None:
        with self._conn(actor) as c:
            r = c.execute(f"SELECT {_M_COLS} FROM team_access WHERE project_id = %s AND account_id = %s",
                          (project_id, actor.account_id)).fetchone()
        return _member(r) if r else None

    def project(self, actor: Actor, project_id: str) -> Project | None:
        with self._conn(actor) as c:
            r = c.execute(f"SELECT {_P_COLS} FROM team_projects WHERE project_id = %s", (project_id,)).fetchone()
        return _project(r) if r else None

    def owned_count(self, actor: Actor) -> int:
        with self._conn(actor) as c:
            return int(c.execute("SELECT count(*) AS n FROM team_projects WHERE owner_account = %s",
                                 (actor.account_id,)).fetchone()["n"])

    def create_project(self, actor: Actor, project: Project, owner: Member) -> None:
        from psycopg.types.json import Jsonb

        p = project
        with self._conn(actor) as c:
            c.execute(f"INSERT INTO team_projects ({_P_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      (p.id, p.tenant_id, p.name, p.description, p.owner_account, Jsonb(p.repos),
                       Jsonb(p.secret_names), Jsonb(p.budget), Jsonb(p.rules), p.mirror, p.created_at, p.updated_at,
                       p.kind, Jsonb(p.household_policy)))
            self._insert_member(c, owner)

    @staticmethod
    def _insert_member(c: Any, m: Member) -> None:
        vals = (m.project_id, m.tenant_id, m.account_id, m.role, m.email, m.added_by, m.added_at)
        c.execute(f"INSERT INTO team_access ({_M_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s)", vals)
        c.execute(f"INSERT INTO team_members ({_M_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s)", vals)

    def save_project(self, actor: Actor, project: Project) -> None:
        from psycopg.types.json import Jsonb

        p = project
        with self._conn(actor) as c:
            c.execute("UPDATE team_projects SET name=%s, description=%s, repos=%s, secret_names=%s, budget=%s, "
                      "rules=%s, mirror=%s, updated_at=%s, household_policy=%s WHERE project_id=%s AND tenant_id=%s",
                      (p.name, p.description, Jsonb(p.repos), Jsonb(p.secret_names), Jsonb(p.budget), Jsonb(p.rules),
                       p.mirror, p.updated_at, Jsonb(p.household_policy), p.id, p.tenant_id))

    def delete_project(self, actor: Actor, project: Project) -> None:
        with self._conn(actor) as c:
            for t in ("team_audit", "team_note_quarantine", "team_notes", "team_skills", "team_invites",
                      "team_members", "team_access", "team_projects"):
                c.execute(f"DELETE FROM {t} WHERE project_id = %s AND tenant_id = %s", (project.id, project.tenant_id))

    def members(self, actor: Actor, project_id: str) -> list[Member]:
        with self._conn(actor) as c:
            rows = c.execute(f"SELECT {_M_COLS} FROM team_members WHERE project_id = %s AND tenant_id = (SELECT tenant_id FROM team_projects WHERE project_id = %s)",
                             (project_id, project_id)).fetchall()
        return sorted((_member(r) for r in rows), key=lambda m: (-_rank(m.role), m.added_at))

    def set_role(self, actor: Actor, project: Project, account_id: str, role: str) -> None:
        with self._conn(actor) as c:
            for t in ("team_access", "team_members"):
                c.execute(f"UPDATE {t} SET role = %s WHERE project_id = %s AND tenant_id = %s AND account_id = %s",
                          (role, project.id, project.tenant_id, account_id))

    def add_member(self, actor: Actor, member: Member) -> None:
        """Adhésion sans invitation, dans le locataire du projet (le SSO, nokker/sso/service.py) : la politique
        `tenant_isolation` l'admet, le trigger de 032 refuse toute ligne `owner` qui ne soit pas le créateur."""
        if member.role == "owner":
            raise ValueError("owner : le créateur du projet seulement")
        with self._conn(actor) as c:
            self._insert_member(c, member)

    def remove_member(self, actor: Actor, project: Project, account_id: str) -> bool:
        with self._conn(actor) as c:
            c.execute("DELETE FROM team_members WHERE project_id = %s AND tenant_id = %s AND account_id = %s",
                      (project.id, project.tenant_id, account_id))
            n = c.execute("DELETE FROM team_access WHERE project_id = %s AND tenant_id = %s AND account_id = %s",
                          (project.id, project.tenant_id, account_id)).rowcount
        return n > 0

    def create_invite(self, actor: Actor, inv: Invite) -> None:
        i = inv
        with self._conn(actor) as c:
            c.execute(f"INSERT INTO team_invites ({_I_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      (i.id, i.project_id, i.tenant_id, i.project_name, i.email, i.invitee_account, i.role,
                       i.invited_by, i.state, i.created_at, i.expires_at, i.decided_at, i.kind, i.profile))

    def invites_of(self, actor: Actor, project_id: str) -> list[Invite]:
        with self._conn(actor) as c:
            rows = c.execute(f"SELECT {_I_COLS} FROM team_invites WHERE project_id = %s AND tenant_id = (SELECT tenant_id FROM team_projects WHERE project_id = %s) "
                             "ORDER BY created_at", (project_id, project_id)).fetchall()
        return [_invite(r) for r in rows]

    def invites_for(self, actor: Actor) -> list[Invite]:
        with self._conn(actor) as c:
            rows = c.execute(f"SELECT {_I_COLS} FROM team_invites WHERE state = 'pending' AND "
                             "(invitee_account = %s OR (email <> '' AND email = %s)) ORDER BY created_at",
                             (actor.account_id, (actor.email or "").lower())).fetchall()
        return [_invite(r) for r in rows]

    def invite(self, actor: Actor, invite_id: str) -> Invite | None:
        with self._conn(actor) as c:
            r = c.execute(f"SELECT {_I_COLS} FROM team_invites WHERE invite_id = %s", (invite_id,)).fetchone()
        return _invite(r) if r else None

    def accept(self, actor: Actor, inv: Invite, member: Member, now: float) -> None:
        # Ordre imposé par la RLS (023) : l'accès s'écrit tant que l'invitation est encore `pending`.
        with self._conn(actor) as c:
            self._insert_member(c, member)
            c.execute("UPDATE team_invites SET state = 'accepted', decided_at = %s, invitee_account = %s "
                      "WHERE invite_id = %s", (now, member.account_id, inv.id))

    def set_invite_state(self, actor: Actor, inv: Invite, state: str, now: float) -> None:
        with self._conn(actor) as c:
            c.execute("UPDATE team_invites SET state = %s, decided_at = %s WHERE invite_id = %s", (state, now, inv.id))

    def skills(self, actor: Actor, project_id: str) -> list[Skill]:
        with self._conn(actor) as c:
            rows = c.execute(f"SELECT {_S_COLS} FROM team_skills WHERE project_id = %s AND tenant_id = (SELECT tenant_id FROM team_projects WHERE project_id = %s) ORDER BY name",
                             (project_id, project_id)).fetchall()
        return [_skill(r) for r in rows]

    def put_skill(self, actor: Actor, skill: Skill) -> None:
        s = skill
        with self._conn(actor) as c:
            c.execute(f"INSERT INTO team_skills ({_S_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                      "ON CONFLICT (project_id, name) DO UPDATE SET source = EXCLUDED.source, "
                      "description = EXCLUDED.description, requires_local = EXCLUDED.requires_local, "
                      "added_by = EXCLUDED.added_by, added_at = EXCLUDED.added_at",
                      (s.project_id, s.tenant_id, s.name, s.source, s.description, s.requires_local, s.added_by,
                       s.added_at))

    def delete_skill(self, actor: Actor, project: Project, name: str) -> bool:
        with self._conn(actor) as c:
            return c.execute("DELETE FROM team_skills WHERE project_id = %s AND tenant_id = %s AND name = %s",
                             (project.id, project.tenant_id, name)).rowcount > 0

    def note(self, actor: Actor, project: Project, name: str) -> Note | None:
        with self._conn(actor) as c:
            r = c.execute(f"SELECT {_N_COLS} FROM team_notes WHERE project_id = %s AND tenant_id = %s AND name = %s",
                          (project.id, project.tenant_id, name)).fetchone()
        return _note(r) if r else None

    def notes_after(self, actor: Actor, project: Project, seq: int, limit: int) -> list[Note]:
        with self._conn(actor) as c:
            rows = c.execute(f"SELECT {_N_COLS} FROM team_notes WHERE project_id = %s AND tenant_id = %s AND seq > %s "
                             "ORDER BY seq LIMIT %s", (project.id, project.tenant_id, seq, limit)).fetchall()
        return [_note(r) for r in rows]

    def count_notes(self, actor: Actor, project: Project) -> int:
        with self._conn(actor) as c:
            return int(c.execute("SELECT count(*) AS n FROM team_notes WHERE project_id = %s AND tenant_id = %s "
                                 "AND NOT deleted", (project.id, project.tenant_id)).fetchone()["n"])

    def put_note(self, actor: Actor, note: Note) -> Note:
        from psycopg.types.json import Jsonb

        n = note
        cols = _N_COLS.removesuffix(", seq")
        with self._conn(actor) as c:
            r = c.execute(
                f"INSERT INTO team_notes ({cols}, seq) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, "
                "nextval('team_notes_seq')) ON CONFLICT (project_id, name) DO UPDATE SET kind = EXCLUDED.kind, "
                "title = EXCLUDED.title, text = EXCLUDED.text, sha = EXCLUDED.sha, updated_at = EXCLUDED.updated_at, "
                "author_account = EXCLUDED.author_account, author_machine = EXCLUDED.author_machine, "
                "origin = EXCLUDED.origin, derived_from = EXCLUDED.derived_from, links = EXCLUDED.links, "
                f"deleted = EXCLUDED.deleted, seq = nextval('team_notes_seq') RETURNING {_N_COLS}",
                (n.project_id, n.tenant_id, n.name, n.kind, n.title, n.text, n.sha, n.updated_at, n.author_account,
                 n.author_machine, n.origin, n.derived_from, Jsonb(n.links), n.deleted)).fetchone()
        return _note(r)

    @staticmethod
    def _q_insert(c: Any, n: Note, reason: str, now: float) -> None:
        from psycopg.types.json import Jsonb

        c.execute(f"INSERT INTO team_note_quarantine ({_Q_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                  (n.project_id, n.tenant_id, n.name, n.kind, n.title, n.text, n.sha, n.updated_at, n.author_account,
                   n.author_machine, n.origin, n.derived_from, Jsonb(list(n.links)), reason, now))

    def quarantine(self, actor: Actor, note: Note, reason: str, now: float) -> None:
        with self._conn(actor) as c:
            self._q_insert(c, note, reason, now)

    def quarantined(self, actor: Actor, project: Project) -> list[dict[str, Any]]:
        with self._conn(actor) as c:
            rows = c.execute("SELECT id, project_id, name, kind, title, text, sha, updated_at, author_account, "
                             "author_machine, origin, derived_from, links, reason, quarantined_at, restored_at, "
                             "restored_by FROM team_note_quarantine WHERE project_id = %s AND tenant_id = %s "
                             "ORDER BY quarantined_at, id", (project.id, project.tenant_id)).fetchall()
        return [{**dict(r), "links": list(r["links"] or [])} for r in rows]

    def restore_note(self, actor: Actor, project: Project, quarantine_id: int, by: str, now: float) -> Note:
        """Une transaction, sous le verrou d'écriture des notes du projet (celui de `write_note`)."""
        with self._conn(actor) as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("team_notes:" + project.id,))
            r = c.execute("SELECT id, name, kind, title, text, sha, author_account, author_machine, origin, "
                          "derived_from, links FROM team_note_quarantine WHERE id = %s AND project_id = %s "
                          "AND tenant_id = %s", (quarantine_id, project.id, project.tenant_id)).fetchone()
            q = dict(r) if r else None
            cur = None
            if q is not None:
                row = c.execute(f"SELECT {_N_COLS} FROM team_notes WHERE project_id = %s AND tenant_id = %s "
                                "AND name = %s", (project.id, project.tenant_id, q["name"])).fetchone()
                cur = _note(row) if row else None
            live = int(c.execute("SELECT count(*) AS n FROM team_notes WHERE project_id = %s AND tenant_id = %s "
                                 "AND NOT deleted", (project.id, project.tenant_id)).fetchone()["n"])
            _check_restore(q, cur, live)
            if cur is not None and not cur.deleted:
                self._q_insert(c, cur, m.REASON_RESTORED, now)
            n = _restored(q, project, now)  # type: ignore[arg-type]
            self._upsert_note(c, n)
            c.execute("UPDATE team_note_quarantine SET restored_at = %s, restored_by = %s WHERE id = %s "
                      "AND project_id = %s AND tenant_id = %s", (now, by, quarantine_id, project.id, project.tenant_id))
            row = c.execute(f"SELECT {_N_COLS} FROM team_notes WHERE project_id = %s AND tenant_id = %s AND name = %s",
                            (project.id, project.tenant_id, n.name)).fetchone()
        return _note(row)

    def audit(self, actor: Actor, project: Project, event: str, detail: dict[str, Any], now: float) -> None:
        from psycopg.types.json import Jsonb

        with self._conn(actor) as c:
            c.execute("INSERT INTO team_audit (project_id, tenant_id, at, actor_account, event, detail) "
                      "VALUES (%s,%s,%s,%s,%s,%s)", (project.id, project.tenant_id, now, actor.account_id, event,
                                                    Jsonb(detail)))

    def audit_events(self, actor: Actor, project: Project, limit: int) -> list[dict[str, Any]]:
        with self._conn(actor) as c:
            rows = c.execute("SELECT id, project_id, at, actor_account, event, detail FROM team_audit "
                             "WHERE project_id = %s AND tenant_id = %s ORDER BY at DESC, id DESC LIMIT %s",
                             (project.id, project.tenant_id, limit)).fetchall()
        return [{**dict(r), "detail": dict(r["detail"] or {})} for r in rows]

    @staticmethod
    def _upsert_note(c: Any, n: Note) -> None:
        from psycopg.types.json import Jsonb

        cols = _N_COLS.removesuffix(", seq")
        c.execute(
            f"INSERT INTO team_notes ({cols}, seq) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, "
            "nextval('team_notes_seq')) ON CONFLICT (project_id, name) DO UPDATE SET kind = EXCLUDED.kind, "
            "title = EXCLUDED.title, text = EXCLUDED.text, sha = EXCLUDED.sha, updated_at = EXCLUDED.updated_at, "
            "author_account = EXCLUDED.author_account, author_machine = EXCLUDED.author_machine, "
            "origin = EXCLUDED.origin, derived_from = EXCLUDED.derived_from, links = EXCLUDED.links, "
            "deleted = EXCLUDED.deleted, seq = nextval('team_notes_seq')",
            (n.project_id, n.tenant_id, n.name, n.kind, n.title, n.text, n.sha, n.updated_at, n.author_account,
             n.author_machine, n.origin, n.derived_from, Jsonb(n.links), n.deleted))

    def write_note(self, actor: Actor, project: Project, note: Note, base_sha: str, now: float) -> str:
        """Une transaction : verrou d'écriture DU PROJET (verrou consultatif, relâché au commit), lecture de la version
        courante, décision (`model.resolve`), quarantaine de la version remplacée, écriture. Le verrou sérialise les
        écrivains d'un projet : la règle « la dernière reçue gagne » ne perd aucune version, et le compteur `seq`
        (curseur du tirage) est pris dans l'ordre des commits — un tirage ne saute jamais une écriture encore en
        cours."""
        n = note
        with self._conn(actor) as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("team_notes:" + project.id,))
            r = c.execute(f"SELECT {_N_COLS} FROM team_notes WHERE project_id = %s AND tenant_id = %s AND name = %s",
                          (project.id, project.tenant_id, n.name)).fetchone()
            cur = _note(r) if r else None
            outcome = m.resolve(cur, n, base_sha)
            if outcome == m.SKIP:
                return outcome
            if outcome == m.INSERT and int(c.execute(
                    "SELECT count(*) AS n FROM team_notes WHERE project_id = %s AND tenant_id = %s AND NOT deleted",
                    (project.id, project.tenant_id)).fetchone()["n"]) >= m.MAX_NOTES:
                raise _too_many()
            if cur is not None and not cur.deleted and (outcome == m.CONFLICT or n.deleted):
                self._q_insert(c, cur, m.REASON_CONFLICT if outcome == m.CONFLICT else m.REASON_FORGOTTEN, now)
            self._upsert_note(c, n)
            return outcome


__all__ = ["Actor", "MemoryTeamStore", "PgTeamStore", "TeamStore"]
