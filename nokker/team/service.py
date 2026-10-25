"""Service de l'équipe (lot X17, docs/LOKKAL.md §7.15) : TOUS les droits sont vérifiés ici, à chaque appel, par
`_gate(acteur, projet, rôle minimal)`.

- Non-membre (ou projet inconnu, ou id mal formé) → 404 `not_found`, toujours la même réponse : on n'apprend pas
  qu'un projet existe.
- Membre au rôle insuffisant → 403 `forbidden` (il sait déjà que le projet existe) ; un viewer n'écrit jamais rien.
- Les secrets ne se partagent jamais en valeur : un projet liste des NOMS (`secret_names`), chaque membre pose sa
  valeur dans son coffre (X4) ; une valeur dans une requête est refusée (400 `secret_value_refused`).
- Les règles de permission ne font que restreindre (pas de `allow`, pas de mode `auto`, model.clean_rules) ; la
  machine les applique comme celles d'un `.nokker/settings.json` de dépôt (qa/permissions.py::restrict_with).
- Notes de projet (miroir, §6.3) : seulement si le propriétaire a mis le projet en miroir ; conflit = l'écriture
  reçue la dernière par nokker.ai gagne (horloge du serveur), l'autre version part en quarantaine pour relecture ;
  une version oubliée y est gardée aussi (rien n'est effacé).

Le service ne connaît pas HTTP (nokker/team/http.py) ; il lève `TeamError(status, code, message)`.
"""
from __future__ import annotations

import contextlib
import contextvars
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nokker.team import model as m
from nokker.team.model import Invite, Member, Note, Project, Skill, TeamError, not_found
from nokker.team.store import TeamStore

# Foyer (lot X23, nokker/household/) : un projet `household` n'existe que pour les routes /v1/household* ; ailleurs
# (/v1/projects*, vue d'une machine liée) il répond comme un projet inconnu. Le service du foyer ouvre ce passage
# pour la durée de ses appels (`household_access`).
_HOUSEHOLD_OK: contextvars.ContextVar[bool] = contextvars.ContextVar("nokker_household_ok", default=False)


@contextlib.contextmanager
def household_access() -> Iterator[None]:
    token = _HOUSEHOLD_OK.set(True)
    try:
        yield
    finally:
        _HOUSEHOLD_OK.reset(token)


def _visible(p: Project) -> bool:
    return p.kind != "household" or _HOUSEHOLD_OK.get()


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=UTC).isoformat()


def project_json(p: Project, role: str) -> dict[str, Any]:
    return {"id": p.id, "name": p.name, "description": p.description, "role": role,
            "owner_account": p.owner_account,
            "repos": [{"key": k, "memory_project": m.memory_project(k)} for k in p.repos],
            "secret_names": [dict(s) for s in p.secret_names], "budget": p.budget,
            "rules": {"ask": [], "deny": [], **p.rules}, "mirror": p.mirror,
            "created_at": _iso(p.created_at), "updated_at": _iso(p.updated_at)}


def public_trigger(trigger: dict[str, Any] | None) -> dict[str, Any]:
    """Déclencheur d'un agent montré aux AUTRES membres : le genre et l'horaire ; pour un événement, la famille
    seulement (`mail`, `webhook` : un filtre de courrier ou un id de webhook ne regarde que son propriétaire), le motif
    entier pour une alerte (`alert:Disk*`, c'est l'organisation de l'équipe)."""
    t = dict(trigger or {"kind": "manual"})
    kind = str(t.get("kind") or "manual")
    out: dict[str, Any] = {"kind": kind}
    if kind == "schedule":
        out.update({k: t[k] for k in ("cron", "tz") if t.get(k)})
    elif kind == "once" and t.get("at"):
        out["at"] = t["at"]
    elif kind == "event":
        ev = str(t.get("event") or "")
        family = ev.split(":", 1)[0]
        out["event"] = ev if family == "alert" else family
    return out


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


@dataclass
class Team:
    store: TeamStore
    tenancy: Any = None  # account(id), account_by_email(email) — tables hors RLS (nokker/tenancy)
    agents: Any = None  # AgentsHub : agents rattachés à un projet (GET /v1/projects/{id}/agents)
    mirror_allowed: Callable[[str], bool] | None = None  # compte → miroir permis (abonnés) ; None = miroir éteint
    scrub: Callable[[str], str] | None = None  # caviardage des invariants sur le texte des notes reçues
    # SSO obligatoire (drapeau `sso`, nokker/sso/service.py::Sso.gate_reason) : (acteur, projet, adhésion) → None si
    # l'accès est permis, sinon {"code", "connection"}. None (défaut, drapeau éteint) : aucun contrôle de plus.
    sso_gate: Callable[[Any, Project, Member], dict[str, str] | None] | None = None
    # Budget du projet décompté sur le ledger (nokker/team/budget.py::ProjectBudget) ; None = rangé, non décompté.
    budget: Any = None
    clock: Callable[[], float] = field(default=time.time)

    # ── accès ──
    def _gate(self, actor: Any, project_id: str, need: str = "viewer") -> tuple[Project, Member]:
        if not m.is_project_id(project_id):
            raise not_found()
        me = self.store.member(actor, project_id)
        p = self.store.project(actor, project_id) if me is not None else None
        if me is None or p is None or p.tenant_id != me.tenant_id or not _visible(p):
            raise not_found()
        if self.sso_gate is not None and (refused := self.sso_gate(actor, p, me)) is not None:
            raise TeamError(403, "sso_required", f"connexion SSO requise pour ce projet ({refused['connection']})",
                            connection=refused["connection"])
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
            if p is not None and p.tenant_id == mem.tenant_id and _visible(p):
                refused = self.sso_gate(actor, p, mem) if self.sso_gate is not None else None
                if refused is not None:  # SSO obligatoire : le nom et le chemin de connexion, rien du contenu
                    out.append({"id": p.id, "name": p.name, "role": mem.role, "created_at": _iso(p.created_at),
                                "sso": {"required": True, "connection": refused["connection"]}})
                else:
                    out.append(project_json(p, mem.role))
        out.sort(key=lambda x: x["created_at"] or "")
        return {"projects": out, "invitations": sum(1 for i in self.store.invites_for(actor)
                                                    if i.kind != "household" or _HOUSEHOLD_OK.get())}

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
        budget_changed = "budget" in body and m.clean_budget(body["budget"]) != p.budget
        if "budget" in body:
            p.budget = m.clean_budget(body["budget"])
        if "mirror" in body:
            if not isinstance(body["mirror"], bool):
                raise TeamError(400, "bad_request", "mirror : true | false")
            p.mirror = body["mirror"]
        p.updated_at = self.clock()
        self.store.save_project(actor, p)
        if budget_changed:
            self._journal_budget(actor, p)
        return project_json(p, me.role)

    # ── budget (décompté sur le ledger : nokker/team/budget.py) ──
    def _journal_budget(self, actor: Any, p: Project) -> None:
        if p.budget is None:
            self.store.audit(actor, p, "budget_removed", {}, self.clock())
        else:
            self.store.audit(actor, p, "budget_set", dict(p.budget), self.clock())

    def put_budget(self, actor: Any, pid: str, body: dict[str, Any]) -> dict[str, Any]:
        """Plafond du jour et/ou du mois (owner ou maintainer), journalisé."""
        p, _ = self._gate(actor, pid, "maintainer")
        p.budget, p.updated_at = m.clean_budget(body), self.clock()
        self.store.save_project(actor, p)
        self._journal_budget(actor, p)
        return {"budget": p.budget}

    def delete_budget(self, actor: Any, pid: str) -> dict[str, Any]:
        p, _ = self._gate(actor, pid, "maintainer")
        if p.budget is not None:
            p.budget, p.updated_at = None, self.clock()
            self.store.save_project(actor, p)
            self._journal_budget(actor, p)
        return {"budget": None}

    def budget_view(self, actor: Any, pid: str) -> dict[str, Any]:
        if self.budget is None:
            p, _ = self._gate(actor, pid)
            return {"project_id": p.id, "budget": p.budget, "counted": False}
        return self.budget.view(actor, pid)

    # ── journal du projet ──
    def audit(self, actor: Any, pid: str, *, limit: int = 100) -> dict[str, Any]:
        p, _ = self._gate(actor, pid, "maintainer")
        rows = self.store.audit_events(actor, p, max(1, min(int(limit), 500)))
        return {"events": [{"id": r["id"], "at": _iso(r["at"]), "actor_account": r["actor_account"],
                            "event": r["event"], "detail": r["detail"]} for r in rows]}

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
        return {"invitations": [my_invite_json(i) for i in self.store.invites_for(actor)
                                if i.expires_at > now and (i.kind != "household" or _HOUSEHOLD_OK.get())]}

    def _my_pending(self, actor: Any, invite_id: str) -> Invite:
        inv = self.store.invite(actor, invite_id) if m.is_invite_id(invite_id) else None
        addressed = inv is not None and (inv.invitee_account == actor.account_id
                                         or (inv.email and inv.email == (actor.email or "").lower()))
        if not addressed or inv.state != "pending" or inv.expires_at <= self.clock() \
                or (inv.kind == "household" and not _HOUSEHOLD_OK.get()):  # type: ignore[union-attr]
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

    def _agents_of_members(self, actor: Any, pid: str) -> tuple[Project, Member, list[tuple[Any, Member, str]]]:
        """Agents rattachés au projet par ses membres ACTUELS : (agent, son propriétaire, locataire de l'agent). Un
        agent garde UNE maison, le locataire de son créateur (§6.7) : on le lit là, dans le scope de CE locataire, et
        seulement s'il porte ce projet et appartient à ce membre. Rien d'autre de ce locataire n'est lu."""
        from nokker.tenancy.context import current_tenant_or_none, tenant_scope

        p, me = self._gate(actor, pid)
        if self.agents is None:
            return p, me, []
        members = {x.account_id: x for x in self.store.members(actor, pid)}
        by_tenant: dict[str, set[str]] = {}
        for acc_id in members:
            acc = self.tenancy.account(acc_id) if self.tenancy is not None else None
            if acc is None and acc_id == actor.account_id:
                acc = actor
            if acc is None or getattr(acc, "status", "active") != "active":
                continue
            by_tenant.setdefault(acc.tenant_id, set()).add(acc_id)
        rows: list[tuple[Any, Member, str]] = []
        seen: set[str] = set()
        for tenant, accounts in sorted(by_tenant.items()):
            if tenant == current_tenant_or_none():
                listed = self.agents.list_agents()
            else:
                with tenant_scope(tenant):
                    listed = self.agents.list_agents()
            for a in listed:
                if a.project == pid and a.created_by in accounts and a.id not in seen:
                    seen.add(a.id)
                    rows.append((a, members[a.created_by], tenant))
        rows.sort(key=lambda r: (r[0].name.lower(), r[0].id))
        return p, me, rows

    def _agent_entry(self, a: Any, owner: Member, actor: Any, me: Member) -> dict[str, Any]:
        from nokker.agents.types import agent_to_json, last_run_json

        mine = a.created_by == actor.account_id
        can_disarm = bool(a.armed) and (mine or me.role == "owner")
        who = {"account_id": owner.account_id, "email": owner.email}
        if mine:
            return {**agent_to_json(a), "owner": who, "mine": True, "can_disarm": can_disarm}
        lr = last_run_json(a.last_run)
        return {"id": a.id, "name": a.name, "status": a.status, "trigger": public_trigger(a.trigger),
                "last_run": {"status": lr["status"], "ended_at": lr["ended_at"]} if lr else None,
                "owner": who, "mine": False, "can_disarm": can_disarm}

    def project_agents(self, actor: Any, pid: str) -> list[dict[str, Any]]:
        """Les agents rattachés au projet par tous ses membres. Les miens : leur carte complète. Ceux des autres : nom,
        état, déclencheur, dernier passage, propriétaire — jamais leurs secrets, leur mission, leurs règles, leurs
        entrées ni leurs sorties (pas de fil, pas de compte rendu)."""
        _, me, rows = self._agents_of_members(actor, pid)
        return [self._agent_entry(a, owner, actor, me) for a, owner, _ in rows]

    def disarm_agent(self, actor: Any, pid: str, agent_id: str) -> dict[str, Any]:
        """Désarmer un agent du projet : son propriétaire, ou l'owner du projet (son seul geste sur l'agent d'un autre,
        journalisé). L'agent est désarmé dans SON locataire ; son propriétaire le réarme comme d'habitude."""
        from nokker.tenancy.context import tenant_scope

        p, me, rows = self._agents_of_members(actor, pid)
        row = next((r for r in rows if r[0].id == agent_id), None)
        if row is None:
            raise TeamError(404, "not_found", "agent inconnu dans ce projet")
        a, owner, tenant = row
        mine = a.created_by == actor.account_id
        if not mine and me.role != "owner":
            raise TeamError(403, "forbidden", "seul le propriétaire de l'agent agit sur lui ; l'owner du projet peut "
                                              "seulement le désarmer")
        with tenant_scope(tenant):
            cur = self.agents.get_agent(agent_id)
            if cur.project != pid or cur.created_by != a.created_by:  # changé entre-temps
                raise TeamError(404, "not_found", "agent inconnu dans ce projet")
            done = self.agents.disarm(agent_id, actor.account_id if mine else f"{actor.account_id} (owner du projet)")
        if not mine:
            self.store.audit(actor, p, "agent_disarmed", {"agent_id": a.id, "name": a.name,
                                                          "owner_account": a.created_by}, self.clock())
        return self._agent_entry(done, owner, actor, me)

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
        """Historique des versions remplacées ou oubliées (maintainer) ; `restored_at` dit si une version a été
        remise en place depuis (elle reste listée : rien n'est effacé)."""
        p, _ = self._gate(actor, pid, "maintainer")
        out = []
        for q in self.store.quarantined(actor, p):
            out.append({"id": q["id"], "name": q["name"], "kind": q["kind"], "title": q["title"], "text": q["text"],
                        "sha": q["sha"], "updated_at": q["updated_at"], "author_account": q["author_account"],
                        "reason": q["reason"], "quarantined_at": _iso(q["quarantined_at"]),
                        "restored_at": _iso(q.get("restored_at")), "restored_by": q.get("restored_by")})
        return {"quarantine": out}

    def restore_note(self, actor: Any, pid: str, quarantine_id: Any) -> dict[str, Any]:
        """Remet une version de la quarantaine en version courante (owner ou maintainer) : la version courante part
        en quarantaine à son tour, rien n'est effacé ; les machines la tirent au passage suivant (nouveau `seq`).
        Journalisé (`note_restored`)."""
        p, _ = self._gate(actor, pid, "maintainer")
        try:
            qid = int(str(quarantine_id))
        except ValueError:
            raise TeamError(404, "not_found", "version inconnue dans la quarantaine de ce projet") from None
        now = self.clock()
        n = self.store.restore_note(actor, p, qid, actor.account_id, now)
        self.store.audit(actor, p, "note_restored", {"name": n.name, "quarantine_id": qid, "sha": n.sha}, now)
        return {"note": note_json(n)}

    def push_notes(self, actor: Any, pid: str, machine_id: str, items: Any) -> dict[str, int]:
        """Notes de projet poussées par la machine d'un membre (rôle `member` au moins : un viewer ne pousse rien, 403).
        Tout ou rien sur la forme (une note illisible refuse l'envoi, 400) ; une note qui ne se dit pas `both` est
        refusée et comptée (`refused`), jamais gardée (seconde barrière de la règle d'or, §6.7). Chaque note passe par
        `store.write_note` : décision et écriture atomiques sous le verrou du projet, `updated_at` = horloge de
        nokker.ai (celle de la machine est ignorée), conflit → la reçue la dernière gagne, l'autre en quarantaine."""
        from nokker.core.locality import may_leave

        p, _ = self._gate(actor, pid, "member")
        self._mirror_on(actor, p)
        if not isinstance(items, list):
            raise TeamError(400, "bad_request", "notes : une liste")
        if len(items) > m.MAX_NOTES_PER_PUSH:
            raise TeamError(413, "too_many", f"{m.MAX_NOTES_PER_PUSH} notes par envoi au plus")
        cleaned = [(m.clean_note(d), may_leave(d.get("locality"))) for d in items]  # tout ou rien
        out = {"accepted": 0, "skipped": 0, "quarantined": 0, "refused": 0}
        for d, leaves in cleaned:
            if not leaves:
                out["refused"] += 1  # `local` ou sans localité : rien n'est gardé
                continue
            title, text = d["title"], d["text"]
            if self.scrub is not None:
                title, text = self.scrub(title), self.scrub(text)
            now = self.clock()
            n = Note(project_id=p.id, tenant_id=p.tenant_id, name=d["name"], kind=d["kind"], title=title, text=text,
                     sha=m.note_sha(d["kind"], title, text, d["deleted"]), updated_at=now,
                     author_account=actor.account_id, author_machine=machine_id, origin=d["origin"],
                     derived_from=d["derived_from"], links=d["links"], deleted=d["deleted"])
            outcome = self.store.write_note(actor, p, n, d["base_sha"], now)
            if outcome == m.SKIP:
                out["skipped"] += 1
            else:
                out["accepted"] += 1
                out["quarantined"] += outcome == m.CONFLICT
        return out

    # ── vue d'une machine liée (Lokkal) ──
    def machine_projects(self, actor: Any) -> dict[str, Any]:
        """Ce que la machine d'un membre applique : dépôts (→ projet mémoire local), règles qui restreignent, catalogue,
        noms de secrets à poser chez soi, et si les notes passent par le miroir (projet en miroir ET compte abonné)."""
        allowed = self.mirror_allowed is not None and self.mirror_allowed(actor.account_id)
        out = []
        for mem in self.store.memberships(actor):
            p = self.store.project(actor, mem.project_id)
            if p is None or p.tenant_id != mem.tenant_id or not _visible(p):
                continue
            if self.sso_gate is not None and self.sso_gate(actor, p, mem) is not None:
                continue  # SSO obligatoire et pas de connexion SSO récente : ni règles, ni notes, ni catalogue
            d = project_json(p, mem.role)
            d["skills"] = [skill_json(s) for s in self.store.skills(actor, p.id)]
            d["mirror"] = bool(p.mirror and allowed)
            d["can_push_notes"] = d["mirror"] and m.RANK[mem.role] >= m.RANK["member"]
            out.append(d)
        return {"projects": out}


__all__ = ["Team", "household_access", "invite_json", "member_json", "note_json", "project_json", "public_trigger", "skill_json"]
