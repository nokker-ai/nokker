"""Service de l'équipe (lot X17, docs/LOKKAL.md §7.15) : TOUS les droits sont vérifiés ici, à chaque appel, par
`_gate(acteur, projet, rôle minimal)`.

- Non-membre (ou projet inconnu, ou id mal formé) → 404 `not_found`, toujours la même réponse : on n'apprend pas
  qu'un projet existe.
- Membre au rôle insuffisant → 403 `forbidden` (il sait déjà que le projet existe) ; un viewer n'écrit jamais rien.
- Les secrets ne se partagent jamais en valeur : un projet liste des NOMS (`secret_names`), chaque membre pose sa
  valeur dans son coffre (X4) ; une valeur dans une requête est refusée (400 `secret_value_refused`).
- Les règles de permission ne font que restreindre (pas de `allow`, pas de mode `auto`, model.clean_rules) ; la
  machine les applique comme celles d'un `.nokker/settings.json` de dépôt (qa/permissions.py::restrict_with).
- Notes de projet (miroir, §6.3) : seulement si le propriétaire a mis le projet en miroir ; conflit = la plus récente
  gagne, l'autre version part en quarantaine pour relecture.

Le service ne connaît pas HTTP (nokker/team/http.py) ; il lève `TeamError(status, code, message)`.
"""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nokker.team import model as m
from nokker.team.model import Invite, Member, Note, Project, Skill, TeamError, not_found
from nokker.team.store import TeamStore


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=UTC).isoformat()


def project_json(p: Project, role: str) -> dict[str, Any]:
    return {"id": p.id, "name": p.name, "description": p.description, "role": role,
            "owner_account": p.owner_account,
            "repos": [{"key": k, "memory_project": m.memory_project(k)} for k in p.repos],
            "secret_names": [dict(s) for s in p.secret_names], "budget": p.budget,
            "rules": {"ask": [], "deny": [], **p.rules}, "mirror": p.mirror,
            "created_at": _iso(p.created_at), "updated_at": _iso(p.updated_at)}


def member_json(x: Member, me: str) -> dict[str, Any]:
    return {"account_id": x.account_id, "email": x.email, "role": x.role, "added_at": _iso(x.added_at),
            "you": x.account_id == me}


def invite_json(i: Invite) -> dict[str, Any]:
    # Jamais `invitee_account` : la réponse ne dit pas si l'adresse a déjà un compte nokker.ai.
    return {"id": i.id, "email": i.email, "role": i.role, "state": i.state, "created_at": _iso(i.created_at),
            "expires_at": _iso(i.expires_at), "decided_at": _iso(i.decided_at)}


def my_invite_json(i: Invite) -> dict[str, Any]:
    return {"id": i.id, "project": {"id": i.project_id, "name": i.project_name}, "role": i.role,
            "created_at": _iso(i.created_at), "expires_at": _iso(i.expires_at)}


def skill_json(s: Skill) -> dict[str, Any]:
    return {"name": s.name, "source": s.source, "description": s.description, "requires_local": s.requires_local,
            "added_by": s.added_by, "added_at": _iso(s.added_at)}


def note_json(n: Note) -> dict[str, Any]:
    out: dict[str, Any] = {"name": n.name, "kind": n.kind, "deleted": n.deleted, "sha": n.sha,
                           "updated_at": n.updated_at, "author_account": n.author_account, "seq": n.seq}
    if not n.deleted:
        out.update(title=n.title, text=n.text, origin=n.origin, derived_from=n.derived_from, links=list(n.links))
    return out


def _sha(kind: str, title: str, text: str, deleted: bool) -> str:
    return hashlib.sha256(f"{kind}\x00{title}\x00{text}\x00{int(deleted)}".encode()).hexdigest()


@dataclass
class Team:
    store: TeamStore
    tenancy: Any = None  # account(id), account_by_email(email) — tables hors RLS (nokker/tenancy)
    agents: Any = None  # AgentsHub : agents rattachés à un projet (GET /v1/projects/{id}/agents)
    mirror_allowed: Callable[[str], bool] | None = None  # compte → miroir permis (abonnés) ; None = miroir éteint
    scrub: Callable[[str], str] | None = None  # caviardage des invariants sur le texte des notes reçues
    clock: Callable[[], float] = field(default=time.time)

    # ── accès ──
    def _gate(self, actor: Any, project_id: str, need: str = "viewer") -> tuple[Project, Member]:
        if not m.is_project_id(project_id):
            raise not_found()
        me = self.store.member(actor, project_id)
        p = self.store.project(actor, project_id) if me is not None else None
        if me is None or p is None or p.tenant_id != me.tenant_id:
            raise not_found()
        if m.RANK[me.role] < m.RANK[need]:
            raise TeamError(403, "forbidden", f"rôle {me.role} : {need} au moins")
        return p, me

    def actor_for(self, account_id: str) -> Any:
        """Compte actif (machine liée : son compte) → acteur, ou None."""
        acc = self.tenancy.account(account_id) if self.tenancy is not None else None
        return acc if acc is not None and getattr(acc, "status", "active") == "active" else None

    # ── projets ──
    def list_projects(self, actor: Any) -> dict[str, Any]:
        out = []
        for mem in self.store.memberships(actor):
            p = self.store.project(actor, mem.project_id)
            if p is not None and p.tenant_id == mem.tenant_id:
                out.append(project_json(p, mem.role))
        out.sort(key=lambda x: x["created_at"] or "")
        return {"projects": out, "invitations": len(self.store.invites_for(actor))}

    def create(self, actor: Any, body: dict[str, Any]) -> dict[str, Any]:
        if self.store.owned_count(actor) >= m.MAX_OWNED_PROJECTS:
            raise TeamError(409, "too_many", f"{m.MAX_OWNED_PROJECTS} projets au plus par compte")
        now = self.clock()
        p = Project(id=m.new_project_id(), tenant_id=actor.tenant_id, name=m.clean_name(body.get("name")),
                    owner_account=actor.account_id, description=m.clean_description(body.get("description")),
                    repos=m.clean_repos(body.get("repos") or []), budget=m.clean_budget(body.get("budget")),
                    rules={"ask": [], "deny": []}, created_at=now, updated_at=now)
        owner = Member(project_id=p.id, tenant_id=p.tenant_id, account_id=actor.account_id, role="owner",
                       email=actor.email, added_by=actor.account_id, added_at=now)
        self.store.create_project(actor, p, owner)
        return project_json(p, "owner")

    def get(self, actor: Any, pid: str) -> dict[str, Any]:
        p, me = self._gate(actor, pid)
        return project_json(p, me.role)

    def patch(self, actor: Any, pid: str, body: dict[str, Any]) -> dict[str, Any]:
        allowed = {"name", "description", "repos", "budget", "mirror"}
        unknown = set(body) - allowed
        if unknown:
            raise TeamError(400, "bad_request", f"champ non modifiable ici : {min(unknown)} "
                                                "(règles : /rules, secrets : /secrets)")
        p, me = self._gate(actor, pid, "owner" if "mirror" in body else "maintainer")
        if "name" in body:
            p.name = m.clean_name(body["name"])
        if "description" in body:
            p.description = m.clean_description(body["description"])
        if "repos" in body:
            p.repos = m.clean_repos(body["repos"])
        if "budget" in body:
            p.budget = m.clean_budget(body["budget"])
        if "mirror" in body:
            if not isinstance(body["mirror"], bool):
                raise TeamError(400, "bad_request", "mirror : true | false")
            p.mirror = body["mirror"]
        p.updated_at = self.clock()
        self.store.save_project(actor, p)
        return project_json(p, me.role)

    def delete(self, actor: Any, pid: str) -> dict[str, Any]:
        p, _ = self._gate(actor, pid, "owner")
        self.store.delete_project(actor, p)
        return {"deleted": True}

    # ── membres ──
    def members(self, actor: Any, pid: str) -> dict[str, Any]:
        self._gate(actor, pid)
        return {"members": [member_json(x, actor.account_id) for x in self.store.members(actor, pid)]}

    def set_role(self, actor: Any, pid: str, account_id: str, body: dict[str, Any]) -> dict[str, Any]:
        p, _ = self._gate(actor, pid, "owner")
        role = m.clean_role(body.get("role"))
        target = next((x for x in self.store.members(actor, pid) if x.account_id == account_id), None)
        if target is None:
            raise TeamError(404, "not_found", "membre inconnu")
        if target.role == "owner":
            raise TeamError(409, "owner_fixed", "le rôle du propriétaire ne change pas")
        self.store.set_role(actor, p, account_id, role)
        target.role = role
        return member_json(target, actor.account_id)

    def remove(self, actor: Any, pid: str, account_id: str) -> dict[str, Any]:
        """Le propriétaire retire un membre ; un membre se retire lui-même (quitter). Le propriétaire ne quitte pas
        son projet : il le supprime."""
        if account_id == actor.account_id:
            p, me = self._gate(actor, pid)
            if me.role == "owner":
                raise TeamError(409, "owner_fixed", "le propriétaire ne quitte pas son projet : il le supprime")
        else:
            p, _ = self._gate(actor, pid, "owner")
        target = next((x for x in self.store.members(actor, pid) if x.account_id == account_id), None)
        if target is None:
            raise TeamError(404, "not_found", "membre inconnu")
        if target.role == "owner":
            raise TeamError(409, "owner_fixed", "le propriétaire ne se retire pas")
        self.store.remove_member(actor, p, account_id)
        return {"removed": True}

    # ── invitations ──
    def invite(self, actor: Any, pid: str, body: dict[str, Any]) -> dict[str, Any]:
        p, _ = self._gate(actor, pid, "owner")
        email, role = m.clean_email(body.get("email")), m.clean_role(body.get("role") or "member")
        members = self.store.members(actor, pid)
        if email == (actor.email or "").lower() or any(x.email == email for x in members):
            raise TeamError(409, "already_member", "déjà membre du projet")
        pending = [i for i in self.store.invites_of(actor, pid) if i.state == "pending" and i.expires_at > self.clock()]
        if any(i.email == email for i in pending):
            raise TeamError(409, "already_invited", "invitation déjà en attente pour cette adresse")
        if len(pending) >= m.MAX_PENDING_INVITES or len(members) + len(pending) >= m.MAX_MEMBERS:
            raise TeamError(409, "too_many", f"{m.MAX_MEMBERS} membres au plus, invitations comprises")
        acc = self.tenancy.account_by_email(email) if self.tenancy is not None else None
        if acc is not None and any(x.account_id == acc.account_id for x in members):
            raise TeamError(409, "already_member", "déjà membre du projet")
        now = self.clock()
        inv = Invite(id=m.new_invite_id(), project_id=p.id, tenant_id=p.tenant_id, project_name=p.name, email=email,
                     role=role, invited_by=actor.account_id, invitee_account=acc.account_id if acc else "",
                     created_at=now, expires_at=now + m.INVITE_TTL_S)
        self.store.create_invite(actor, inv)
        return invite_json(inv)

    def invites(self, actor: Any, pid: str) -> dict[str, Any]:
        self._gate(actor, pid, "owner")
        return {"invitations": [invite_json(i) for i in self.store.invites_of(actor, pid)]}

    def revoke_invite(self, actor: Any, pid: str, invite_id: str) -> dict[str, Any]:
        p, _ = self._gate(actor, pid, "owner")
        inv = self.store.invite(actor, invite_id) if m.is_invite_id(invite_id) else None
        if inv is None or inv.project_id != p.id:
            raise TeamError(404, "not_found", "invitation inconnue")
        if inv.state != "pending":
            raise TeamError(409, "not_pending", f"invitation déjà {inv.state}")
        self.store.set_invite_state(actor, inv, "revoked", self.clock())
        return {"revoked": True}

    def my_invites(self, actor: Any) -> dict[str, Any]:
        now = self.clock()
        return {"invitations": [my_invite_json(i) for i in self.store.invites_for(actor) if i.expires_at > now]}

    def _my_pending(self, actor: Any, invite_id: str) -> Invite:
        inv = self.store.invite(actor, invite_id) if m.is_invite_id(invite_id) else None
        addressed = inv is not None and (inv.invitee_account == actor.account_id
                                         or (inv.email and inv.email == (actor.email or "").lower()))
        if not addressed or inv.state != "pending" or inv.expires_at <= self.clock():  # type: ignore[union-attr]
            raise TeamError(404, "not_found", "invitation inconnue ou expirée")
        return inv  # type: ignore[return-value]

    def accept(self, actor: Any, invite_id: str) -> dict[str, Any]:
        inv = self._my_pending(actor, invite_id)
        if self.store.member(actor, inv.project_id) is not None:
            raise TeamError(409, "already_member", "déjà membre du projet")
        now = self.clock()
        mem = Member(project_id=inv.project_id, tenant_id=inv.tenant_id, account_id=actor.account_id, role=inv.role,
                     email=actor.email, added_by=inv.invited_by, added_at=now)
        self.store.accept(actor, inv, mem, now)
        return self.get(actor, inv.project_id)

    def decline(self, actor: Any, invite_id: str) -> dict[str, Any]:
        inv = self._my_pending(actor, invite_id)
        self.store.set_invite_state(actor, inv, "declined", self.clock())
        return {"declined": True}

    # ── catalogue de skills ──
    def skills(self, actor: Any, pid: str) -> dict[str, Any]:
        self._gate(actor, pid)
        return {"skills": [skill_json(s) for s in self.store.skills(actor, pid)]}

    def put_skill(self, actor: Any, pid: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
        p, _ = self._gate(actor, pid, "maintainer")
        d = m.clean_skill(name, body)
        existing = self.store.skills(actor, pid)
        if len(existing) >= m.MAX_SKILLS and all(s.name != d["name"] for s in existing):
            raise TeamError(409, "too_many", f"{m.MAX_SKILLS} skills au plus")
        s = Skill(project_id=p.id, tenant_id=p.tenant_id, added_by=actor.account_id, added_at=self.clock(), **d)
        self.store.put_skill(actor, s)
        return skill_json(s)

    def delete_skill(self, actor: Any, pid: str, name: str) -> dict[str, Any]:
        p, _ = self._gate(actor, pid, "maintainer")
        if not self.store.delete_skill(actor, p, name):
            raise TeamError(404, "not_found", "skill inconnue")
        return {"removed": True}

    # ── règles de permission ──
    def rules(self, actor: Any, pid: str) -> dict[str, Any]:
        p, _ = self._gate(actor, pid)
        return {"rules": {"ask": [], "deny": [], **p.rules}}

    def put_rules(self, actor: Any, pid: str, body: dict[str, Any]) -> dict[str, Any]:
        p, _ = self._gate(actor, pid, "maintainer")
        p.rules, p.updated_at = m.clean_rules(body), self.clock()
        self.store.save_project(actor, p)
        return {"rules": p.rules}

    # ── noms de secrets ──
    def secrets(self, actor: Any, pid: str) -> dict[str, Any]:
        p, _ = self._gate(actor, pid)
        return {"secrets": [dict(s) for s in p.secret_names]}

    def put_secret(self, actor: Any, pid: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
        d = m.clean_secret_name(name, body)  # une valeur est refusée AVANT même de regarder les droits
        p, _ = self._gate(actor, pid, "maintainer")
        rest = [s for s in p.secret_names if s["name"] != d["name"]]
        if len(rest) >= m.MAX_SECRET_NAMES:
            raise TeamError(409, "too_many", f"{m.MAX_SECRET_NAMES} noms de secrets au plus")
        p.secret_names = sorted([*rest, d], key=lambda s: s["name"])
        p.updated_at = self.clock()
        self.store.save_project(actor, p)
        return d

    def delete_secret(self, actor: Any, pid: str, name: str) -> dict[str, Any]:
        p, _ = self._gate(actor, pid, "maintainer")
        rest = [s for s in p.secret_names if s["name"] != name]
        if len(rest) == len(p.secret_names):
            raise TeamError(404, "not_found", "nom de secret inconnu")
        p.secret_names, p.updated_at = rest, self.clock()
        self.store.save_project(actor, p)
        return {"removed": True}

    # ── agents ──
    def check_agent_project(self, actor: Any, pid: str) -> str:
        """Rattacher un agent à un projet : membre au moins (un viewer n'écrit rien)."""
        self._gate(actor, pid, "member")
        return pid

    def project_agents(self, actor: Any, pid: str) -> list[Any]:
        """Les agents DE L'ACTEUR rattachés au projet : un agent a une maison, dans le locataire de son créateur
        (§6.7) ; ceux des autres membres ne sortent pas de leur locataire en v1."""
        self._gate(actor, pid)
        if self.agents is None:
            return []
        return [a for a in self.agents.list_agents() if a.project == pid and a.created_by == actor.account_id]

    # ── notes de projet (miroir) ──
    def _mirror_on(self, actor: Any, p: Project) -> None:
        if not p.mirror:
            raise TeamError(409, "mirror_off", "projet non mis en miroir : ses notes restent sur les machines")

    def notes(self, actor: Any, pid: str, since: str | None, *, limit: int = 200) -> dict[str, Any]:
        p, _ = self._gate(actor, pid)
        self._mirror_on(actor, p)
        try:
            seq = int(since or 0)
        except ValueError:
            raise TeamError(400, "bad_cursor", "curseur illisible") from None
        rows = self.store.notes_after(actor, p, seq, max(1, min(limit, 500)))
        return {"notes": [note_json(n) for n in rows], "cursor": str(rows[-1].seq if rows else seq)}

    def quarantined(self, actor: Any, pid: str) -> dict[str, Any]:
        p, _ = self._gate(actor, pid, "maintainer")
        return {"quarantine": [{**q, "updated_at": q["updated_at"], "quarantined_at": _iso(q["quarantined_at"])}
                               for q in self.store.quarantined(actor, p)]}

    def push_notes(self, actor: Any, pid: str, machine_id: str, items: Any) -> dict[str, int]:
        p, _ = self._gate(actor, pid, "member")
        self._mirror_on(actor, p)
        if not isinstance(items, list):
            raise TeamError(400, "bad_request", "notes : une liste")
        if len(items) > m.MAX_NOTES_PER_PUSH:
            raise TeamError(413, "too_many", f"{m.MAX_NOTES_PER_PUSH} notes par envoi au plus")
        cleaned = [m.clean_note(d) for d in items]  # tout ou rien : une note illisible refuse l'envoi
        accepted = skipped = quarantined = 0
        now = self.clock()
        for d in cleaned:
            title, text = d["title"], d["text"]
            if self.scrub is not None:
                title, text = self.scrub(title), self.scrub(text)
            sha = _sha(d["kind"], title, text, d["deleted"])
            n = Note(project_id=p.id, tenant_id=p.tenant_id, name=d["name"], kind=d["kind"], title=title, text=text,
                     sha=sha, updated_at=min(d["updated_at"], now + 300), author_account=actor.account_id,
                     author_machine=machine_id, origin=d["origin"], derived_from=d["derived_from"], links=d["links"],
                     deleted=d["deleted"])
            cur = self.store.note(actor, p, n.name)
            if cur is not None and cur.sha == sha:
                skipped += 1
                continue
            if cur is None:
                if n.deleted:
                    skipped += 1
                    continue
                if self.store.count_notes(actor, p) >= m.MAX_NOTES:
                    raise TeamError(409, "too_many", f"{m.MAX_NOTES} notes au plus par projet")
                self.store.put_note(actor, n)
                accepted += 1
            elif d["base_sha"] == cur.sha:  # l'auteur a modifié la version qu'il avait : pas de conflit
                self.store.put_note(actor, n)
                accepted += 1
            elif n.updated_at > cur.updated_at:  # conflit : la plus récente gagne, l'autre attend une relecture
                self.store.quarantine(actor, cur, "remplacée par une version plus récente", now)
                self.store.put_note(actor, n)
                accepted += 1
                quarantined += 1
            else:
                self.store.quarantine(actor, n, "plus ancienne que la version du projet", now)
                quarantined += 1
        return {"accepted": accepted, "skipped": skipped, "quarantined": quarantined}

    # ── vue d'une machine liée (Lokkal) ──
    def machine_projects(self, actor: Any) -> dict[str, Any]:
        """Ce que la machine d'un membre applique : dépôts (→ projet mémoire local), règles qui restreignent, catalogue,
        noms de secrets à poser chez soi, et si les notes passent par le miroir (projet en miroir ET compte abonné)."""
        allowed = self.mirror_allowed is not None and self.mirror_allowed(actor.account_id)
        out = []
        for mem in self.store.memberships(actor):
            p = self.store.project(actor, mem.project_id)
            if p is None or p.tenant_id != mem.tenant_id:
                continue
            d = project_json(p, mem.role)
            d["skills"] = [skill_json(s) for s in self.store.skills(actor, p.id)]
            d["mirror"] = bool(p.mirror and allowed)
            d["can_push_notes"] = d["mirror"] and m.RANK[mem.role] >= m.RANK["member"]
            out.append(d)
        return {"projects": out}


__all__ = ["Team", "invite_json", "member_json", "note_json", "project_json", "skill_json"]
