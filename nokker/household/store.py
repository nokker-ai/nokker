"""Stockage du foyer (lot X23) : le magasin de l'équipe ÉTENDU, pas un second magasin. `MemoryHouseholdStore` et
`PgHouseholdStore` héritent de `MemoryTeamStore` / `PgTeamStore` (même contrat X17) et ajoutent, dans les MÊMES
transactions que l'équipe :

  - l'adhésion au foyer (`household_members`) écrite avec l'adhésion d'équipe (création du foyer par le payeur,
    acceptation d'une invitation `household`) : un foyer par compte et 6 membres au plus, refusés EN BASE (039) ;
  - le profil mineur d'un compte (`account_minors`, hors RLS : lu avant de connaître le locataire) ;
  - les attestations du représentant légal (`parental_consents`, locataire du foyer) ;
  - les foyers autorisés à déclarer des profils mineurs (`household_minor_grants`, 043, hors RLS : mode `allowlist`).

Les DROITS restent dans le service (nokker/household/service.py). Les erreurs de la base deviennent des refus clairs :
409 `already_in_household`, 409 `household_full`.
"""
from __future__ import annotations

import contextlib
import copy
import secrets
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from nokker.household.model import HARD_MAX_MEMBERS, MINOR_PROFILES, Consent, HouseholdMember
from nokker.team.model import Invite, Member, Project, TeamError
from nokker.team.store import Actor, MemoryTeamStore, PgTeamStore


def new_grant_code() -> str:
    """Code court montré à l'opérateur à la place de l'adresse et du compte du payeur."""
    return "mg-" + secrets.token_hex(3)


def already_in_household() -> TeamError:
    return TeamError(409, "already_in_household", "ce compte fait déjà partie d'un foyer (un foyer par compte)")


def household_full(n: int = HARD_MAX_MEMBERS) -> TeamError:
    return TeamError(409, "household_full", f"{n} membres au plus dans un foyer, payeur compris")


@dataclass
class MemoryHouseholdStore(MemoryTeamStore):
    hh_rows: dict[tuple[str, str], HouseholdMember] = field(default_factory=dict)  # (foyer, compte)
    minor_rows: dict[str, dict[str, Any]] = field(default_factory=dict)  # compte → profil mineur
    consent_rows: list[Consent] = field(default_factory=list)
    grant_rows: dict[str, dict[str, Any]] = field(default_factory=dict)  # payeur → autorisation (043)
    grant_audit_rows: list[dict[str, Any]] = field(default_factory=list)

    # ── adhésions ──
    def _hh_insert(self, row: HouseholdMember) -> None:
        if any(acc == row.account_id for (_, acc) in self.hh_rows):
            raise already_in_household()
        if sum(1 for (h, _) in self.hh_rows if h == row.household_id) >= HARD_MAX_MEMBERS:
            raise household_full()
        self.hh_rows[(row.household_id, row.account_id)] = copy.copy(row)

    def create_household(self, actor: Actor, project: Project, owner: Member, payer: HouseholdMember) -> None:
        with self._lock:
            if any(acc == payer.account_id for (_, acc) in self.hh_rows):
                raise TeamError(409, "household_exists", "ce compte fait déjà partie d'un foyer (un foyer par compte)")
            self._hh_insert(payer)
            self.projects[project.id] = copy.deepcopy(project)
            self.access[(project.id, owner.account_id)] = copy.copy(owner)

    def accept(self, actor: Actor, inv: Invite, member: Member, now: float) -> None:
        if inv.kind != "household":
            return super().accept(actor, inv, member, now)
        with self._lock:
            row = HouseholdMember(household_id=inv.project_id, tenant_id=inv.tenant_id, account_id=member.account_id,
                                  email=member.email, profile=inv.profile, added_at=now)
            self._hh_insert(row)
            if inv.profile in MINOR_PROFILES:
                self.minor_rows[member.account_id] = {"account_id": member.account_id, "profile": inv.profile,
                                                      "household_id": inv.project_id, "household_tenant": inv.tenant_id,
                                                      "guardian_account": inv.invited_by, "declared_at": now}
            self.access[(member.project_id, member.account_id)] = copy.copy(member)
            i = self.invites[inv.id]
            i.state, i.decided_at, i.invitee_account = "accepted", now, member.account_id

    def remove_member(self, actor: Actor, project: Project, account_id: str) -> bool:
        with self._lock:
            self.hh_rows.pop((project.id, account_id), None)
        return super().remove_member(actor, project, account_id)

    def delete_project(self, actor: Actor, project: Project) -> None:
        with self._lock:
            for k in [k for k in self.hh_rows if k[0] == project.id]:
                del self.hh_rows[k]
            self.consent_rows[:] = [c for c in self.consent_rows if c.household_id != project.id]
        super().delete_project(actor, project)

    def hh_members(self, actor: Actor, household_id: str) -> list[HouseholdMember]:
        with self._lock:
            rows = [copy.copy(r) for (h, _), r in self.hh_rows.items() if h == household_id]
        return sorted(rows, key=lambda r: (not r.payer, r.added_at, r.account_id))

    def hh_membership(self, actor: Actor, account_id: str) -> HouseholdMember | None:
        with self._lock:
            return next((copy.copy(r) for (_, acc), r in self.hh_rows.items() if acc == account_id), None)

    def save_hh(self, actor: Actor, row: HouseholdMember) -> None:
        with self._lock:
            if (row.household_id, row.account_id) in self.hh_rows:
                self.hh_rows[(row.household_id, row.account_id)] = copy.copy(row)

    # ── profils mineurs ──
    def minor(self, account_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self.minor_rows.get(account_id)
            return dict(r) if r else None

    def put_minor(self, actor: Actor, row: dict[str, Any]) -> None:
        with self._lock:
            self.minor_rows[row["account_id"]] = dict(row)

    def delete_minor(self, actor: Actor, account_id: str) -> None:
        with self._lock:
            self.minor_rows.pop(account_id, None)

    # ── foyers autorisés à déclarer des profils mineurs (043) ──
    def minor_grant(self, account_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self.grant_rows.get(account_id)
            return dict(r) if r else None

    def grant_list(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted((dict(r) for r in self.grant_rows.values()), key=lambda r: (r["granted_at"], r["code"]))

    def grant_allow(self, account_id: str, tenant_id: str, now: float) -> tuple[dict[str, Any], str]:
        """(ligne, état) : `new`, `restored` (autorisation retirée puis rendue) ou `already` (rien n'est écrit)."""
        with self._lock:
            r = self.grant_rows.get(account_id)
            if r is not None and r["revoked_at"] is None:
                return dict(r), "already"
            state = "new" if r is None else "restored"
            if r is None:
                codes = {x["code"] for x in self.grant_rows.values()}
                code = new_grant_code()
                while code in codes:
                    code = new_grant_code()
                r = self.grant_rows[account_id] = {"account_id": account_id, "tenant_id": tenant_id, "code": code,
                                                   "granted_at": now, "revoked_at": None}
            else:
                r["granted_at"], r["revoked_at"] = now, None
            self.grant_audit_rows.append({"at": now, "tenant_id": tenant_id, "account_id": account_id,
                                          "code": r["code"], "action": "allow", "suspended": 0})
            return dict(r), state

    def grant_revoke(self, account_id: str, now: float) -> tuple[dict[str, Any] | None, int]:
        """Retire l'autorisation et SUSPEND les profils mineurs du foyer du payeur (rien n'est supprimé).
        (ligne, profils suspendus) ; (None, 0) sans autorisation ; un retrait déjà fait ne compte rien."""
        with self._lock:
            r = self.grant_rows.get(account_id)
            if r is None:
                return None, 0
            if r["revoked_at"] is not None:
                return dict(r), 0
            r["revoked_at"] = now
            n = 0
            for hm_row in self.hh_rows.values():
                if hm_row.tenant_id == r["tenant_id"] and hm_row.minor and hm_row.status != "suspended":
                    hm_row.status, n = "suspended", n + 1
            self.grant_audit_rows.append({"at": now, "tenant_id": r["tenant_id"], "account_id": account_id,
                                          "code": r["code"], "action": "revoke", "suspended": n})
            return dict(r), n

    def grant_minors(self, account_id: str) -> int:
        """Profils mineurs déclarés dans le foyer de ce payeur."""
        with self._lock:
            r = self.grant_rows.get(account_id)
            tid = r["tenant_id"] if r else None
            return sum(1 for x in self.hh_rows.values() if x.tenant_id == tid and x.minor) if tid else 0

    # ── attestations ──
    def add_consent(self, actor: Actor, c: Consent) -> Consent:
        with self._lock:
            c = copy.copy(c)
            c.id = len(self.consent_rows) + 1
            self.consent_rows.append(c)
            return copy.copy(c)

    def consents(self, actor: Actor, household_id: str) -> list[Consent]:
        with self._lock:
            return [copy.copy(c) for c in self.consent_rows if c.household_id == household_id]

    def revoke_consents(self, actor: Actor, household_id: str, minor_email: str, by: str, now: float) -> int:
        n = 0
        with self._lock:
            for c in self.consent_rows:
                if c.household_id == household_id and c.minor_email == minor_email and c.revoked_at is None:
                    c.revoked_at, c.revoked_by, n = now, by, n + 1
        return n


# ─────────────────────────────── Postgres ───────────────────────────────

_H_COLS = "household_id, tenant_id, account_id, email, payer, profile, status, cap_day, cap_custom, added_at"
_C_COLS = ("id, household_id, guardian_account, minor_email, invite_id, profile, version_sha, lang, granted_at, "
           "revoked_at, revoked_by, ip")
_MINOR_COLS = "account_id, profile, household_id, household_tenant, guardian_account, declared_at"


def _hh(r: dict[str, Any]) -> HouseholdMember:
    return HouseholdMember(household_id=r["household_id"], tenant_id=r["tenant_id"], account_id=r["account_id"],
                           email=r["email"], payer=bool(r["payer"]), profile=r["profile"], status=r["status"],
                           cap_day=r["cap_day"], cap_custom=bool(r["cap_custom"]), added_at=r["added_at"])


def _consent(r: dict[str, Any]) -> Consent:
    return Consent(id=int(r["id"]), household_id=r["household_id"], guardian_account=r["guardian_account"],
                   minor_email=r["minor_email"], invite_id=r["invite_id"], profile=r["profile"],
                   version_sha=r["version_sha"], lang=r["lang"], granted_at=r["granted_at"],
                   revoked_at=r["revoked_at"], revoked_by=r["revoked_by"], ip=r["ip"])


@contextlib.contextmanager
def _db_refusals(*, exists_code: str = "already_in_household") -> Iterator[None]:
    """Contraintes de 039 → refus clairs (la base est la dernière ligne : le service a déjà compté)."""
    import psycopg

    try:
        yield
    except psycopg.errors.UniqueViolation as e:
        if "household_members" in str(e):
            if exists_code == "household_exists":
                raise TeamError(409, "household_exists",
                                "ce compte fait déjà partie d'un foyer (un foyer par compte)") from None
            raise already_in_household() from None
        raise
    except psycopg.errors.CheckViolation as e:
        if "household_full" in str(e):
            raise household_full() from None
        raise


class PgHouseholdStore(PgTeamStore):
    """Mêmes connexions que l'équipe (`app.tenant_id` + `app.account_id`/`app.account_email`), mêmes transactions."""

    def __init__(self, dsn: str, *, migrate_now: bool = True) -> None:
        super().__init__(dsn, migrate_now=migrate_now)  # 023, 037, 039
        if migrate_now:
            from pathlib import Path

            from nokker.store.conn import migrate

            migrate(dsn, (Path(__file__).parent.parent / "store" / "migrations"
                          / "043_household_minor_grants.sql").read_text(encoding="utf-8"))

    def _insert_hh(self, c: Any, r: HouseholdMember) -> None:
        c.execute(f"INSERT INTO household_members ({_H_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                  (r.household_id, r.tenant_id, r.account_id, r.email, r.payer, r.profile, r.status, r.cap_day,
                   r.cap_custom, r.added_at))

    def create_household(self, actor: Actor, project: Project, owner: Member, payer: HouseholdMember) -> None:
        from psycopg.types.json import Jsonb

        p = project
        with _db_refusals(exists_code="household_exists"), self._conn(actor) as c:
            self._insert_hh(c, payer)  # d'abord : un compte déjà dans un foyer n'écrit rien d'autre
            c.execute("INSERT INTO team_projects (project_id, tenant_id, name, description, owner_account, repos, "
                      "secret_names, budget, rules, mirror, created_at, updated_at, kind, household_policy) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      (p.id, p.tenant_id, p.name, p.description, p.owner_account, Jsonb(p.repos),
                       Jsonb(p.secret_names), Jsonb(p.budget), Jsonb(p.rules), p.mirror, p.created_at, p.updated_at,
                       p.kind, Jsonb(p.household_policy)))
            self._insert_member(c, owner)

    def accept(self, actor: Actor, inv: Invite, member: Member, now: float) -> None:
        if inv.kind != "household":
            return super().accept(actor, inv, member, now)
        # Ordre imposé par la RLS : adhésion d'équipe (invitation encore `pending`), puis le foyer (la politique
        # `household_join` lit le rôle ET le profil de l'invitation), puis l'invitation passe `accepted`.
        with _db_refusals(), self._conn(actor) as c:
            self._insert_member(c, member)
            self._insert_hh(c, HouseholdMember(household_id=inv.project_id, tenant_id=inv.tenant_id,
                                               account_id=member.account_id, email=member.email,
                                               profile=inv.profile, added_at=now))
            if inv.profile in MINOR_PROFILES:
                c.execute(f"INSERT INTO account_minors ({_MINOR_COLS}) VALUES (%s,%s,%s,%s,%s,%s) "
                          "ON CONFLICT (account_id) DO UPDATE SET profile = EXCLUDED.profile, "
                          "household_id = EXCLUDED.household_id, household_tenant = EXCLUDED.household_tenant, "
                          "guardian_account = EXCLUDED.guardian_account, declared_at = EXCLUDED.declared_at",
                          (member.account_id, inv.profile, inv.project_id, inv.tenant_id, inv.invited_by, now))
            c.execute("UPDATE team_invites SET state = 'accepted', decided_at = %s, invitee_account = %s "
                      "WHERE invite_id = %s", (now, member.account_id, inv.id))

    def remove_member(self, actor: Actor, project: Project, account_id: str) -> bool:
        with self._conn(actor) as c:
            c.execute("DELETE FROM household_members WHERE household_id = %s AND tenant_id = %s AND account_id = %s",
                      (project.id, project.tenant_id, account_id))
        return super().remove_member(actor, project, account_id)

    def delete_project(self, actor: Actor, project: Project) -> None:
        with self._conn(actor) as c:
            for t in ("household_members", "parental_consents"):
                c.execute(f"DELETE FROM {t} WHERE household_id = %s AND tenant_id = %s", (project.id, project.tenant_id))
        super().delete_project(actor, project)

    def hh_members(self, actor: Actor, household_id: str) -> list[HouseholdMember]:
        with self._conn(actor) as c:
            rows = c.execute(f"SELECT {_H_COLS} FROM household_members WHERE household_id = %s AND tenant_id = "
                             "(SELECT tenant_id FROM team_projects WHERE project_id = %s)",
                             (household_id, household_id)).fetchall()
        return sorted((_hh(r) for r in rows), key=lambda r: (not r.payer, r.added_at, r.account_id))

    def hh_membership(self, actor: Actor, account_id: str) -> HouseholdMember | None:
        with self._conn(actor) as c:
            r = c.execute(f"SELECT {_H_COLS} FROM household_members WHERE account_id = %s", (account_id,)).fetchone()
        return _hh(r) if r else None

    def save_hh(self, actor: Actor, row: HouseholdMember) -> None:
        with self._conn(actor) as c:
            c.execute("UPDATE household_members SET profile = %s, status = %s, cap_day = %s, cap_custom = %s "
                      "WHERE household_id = %s AND tenant_id = %s AND account_id = %s",
                      (row.profile, row.status, row.cap_day, row.cap_custom, row.household_id, row.tenant_id,
                       row.account_id))

    # ── profils mineurs (hors RLS) ──
    def minor(self, account_id: str) -> dict[str, Any] | None:
        from psycopg.rows import dict_row

        from nokker.store.conn import connect_admin

        with connect_admin(self.dsn, row_factory=dict_row) as c:
            r = c.execute(f"SELECT {_MINOR_COLS} FROM account_minors WHERE account_id = %s", (account_id,)).fetchone()
        return dict(r) if r else None

    def put_minor(self, actor: Actor, row: dict[str, Any]) -> None:
        with self._conn(actor) as c:
            c.execute(f"INSERT INTO account_minors ({_MINOR_COLS}) VALUES (%s,%s,%s,%s,%s,%s) "
                      "ON CONFLICT (account_id) DO UPDATE SET profile = EXCLUDED.profile, "
                      "household_id = EXCLUDED.household_id, household_tenant = EXCLUDED.household_tenant, "
                      "guardian_account = EXCLUDED.guardian_account, declared_at = EXCLUDED.declared_at",
                      tuple(row[k] for k in ("account_id", "profile", "household_id", "household_tenant",
                                             "guardian_account", "declared_at")))

    def delete_minor(self, actor: Actor, account_id: str) -> None:
        with self._conn(actor) as c:
            c.execute("DELETE FROM account_minors WHERE account_id = %s", (account_id,))

    # ── foyers autorisés à déclarer des profils mineurs (043, hors RLS) ──
    _G_COLS = "account_id, tenant_id, code, granted_at, revoked_at"

    def minor_grant(self, account_id: str) -> dict[str, Any] | None:
        from psycopg.rows import dict_row

        from nokker.store.conn import connect_admin

        with connect_admin(self.dsn, row_factory=dict_row) as c:
            r = c.execute(f"SELECT {self._G_COLS} FROM household_minor_grants WHERE account_id = %s",
                          (account_id,)).fetchone()
        return dict(r) if r else None

    def grant_list(self) -> list[dict[str, Any]]:
        from psycopg.rows import dict_row

        from nokker.store.conn import connect_admin

        with connect_admin(self.dsn, row_factory=dict_row) as c:
            return [dict(r) for r in c.execute(f"SELECT {self._G_COLS} FROM household_minor_grants "
                                               "ORDER BY granted_at, code").fetchall()]

    def grant_allow(self, account_id: str, tenant_id: str, now: float) -> tuple[dict[str, Any], str]:
        from psycopg.rows import dict_row

        from nokker.store.conn import connect_admin

        with connect_admin(self.dsn, row_factory=dict_row) as c:
            r = c.execute(f"SELECT {self._G_COLS} FROM household_minor_grants WHERE account_id = %s FOR UPDATE",
                          (account_id,)).fetchone()
            if r is not None and r["revoked_at"] is None:
                return dict(r), "already"
            if r is None:
                state = "new"
                r = c.execute(f"INSERT INTO household_minor_grants ({self._G_COLS}) VALUES (%s,%s,%s,%s,NULL) "
                              f"RETURNING {self._G_COLS}",
                              (account_id, tenant_id, new_grant_code(), now)).fetchone()
            else:
                state = "restored"
                r = c.execute("UPDATE household_minor_grants SET granted_at = %s, revoked_at = NULL "
                              f"WHERE account_id = %s RETURNING {self._G_COLS}", (now, account_id)).fetchone()
            c.execute("INSERT INTO household_minor_grant_audit (at, tenant_id, account_id, code, action, suspended) "
                      "VALUES (%s,%s,%s,%s,'allow',0)", (now, r["tenant_id"], account_id, r["code"]))
        return dict(r), state

    def grant_revoke(self, account_id: str, now: float) -> tuple[dict[str, Any] | None, int]:
        from psycopg.rows import dict_row

        from nokker.store.conn import connect_admin

        with connect_admin(self.dsn, row_factory=dict_row) as c:
            r = c.execute(f"SELECT {self._G_COLS} FROM household_minor_grants WHERE account_id = %s FOR UPDATE",
                          (account_id,)).fetchone()
            if r is None:
                return None, 0
            if r["revoked_at"] is not None:
                return dict(r), 0
            c.execute("UPDATE household_minor_grants SET revoked_at = %s WHERE account_id = %s", (now, account_id))
            c.execute("SELECT set_config('app.tenant_id', %s, true)", (r["tenant_id"],))  # RLS : foyer du payeur
            n = c.execute("UPDATE household_members SET status = 'suspended' WHERE tenant_id = %s "
                          "AND profile = ANY(%s) AND status <> 'suspended'",
                          (r["tenant_id"], list(MINOR_PROFILES))).rowcount
            c.execute("INSERT INTO household_minor_grant_audit (at, tenant_id, account_id, code, action, suspended) "
                      "VALUES (%s,%s,%s,%s,'revoke',%s)", (now, r["tenant_id"], account_id, r["code"], n))
        r["revoked_at"] = now
        return dict(r), n

    def grant_minors(self, account_id: str) -> int:
        from nokker.store.conn import connect_admin

        with connect_admin(self.dsn) as c:
            g = c.execute("SELECT tenant_id FROM household_minor_grants WHERE account_id = %s",
                          (account_id,)).fetchone()
            if g is None:
                return 0
            c.execute("SELECT set_config('app.tenant_id', %s, true)", (g[0],))
            return c.execute("SELECT count(*) FROM household_members WHERE tenant_id = %s AND profile = ANY(%s)",
                             (g[0], list(MINOR_PROFILES))).fetchone()[0]

    # ── attestations ──
    def add_consent(self, actor: Actor, k: Consent) -> Consent:
        with self._conn(actor) as c:
            r = c.execute("INSERT INTO parental_consents (household_id, tenant_id, guardian_account, minor_email, "
                          "invite_id, profile, version_sha, lang, attested, granted_at, ip) "
                          "VALUES (%s, (SELECT tenant_id FROM team_projects WHERE project_id = %s), %s,%s,%s,%s,%s,%s,"
                          "true,%s,%s) RETURNING id",
                          (k.household_id, k.household_id, k.guardian_account, k.minor_email, k.invite_id, k.profile,
                           k.version_sha, k.lang, k.granted_at, k.ip)).fetchone()
        out = copy.copy(k)
        out.id = int(r["id"])
        return out

    def consents(self, actor: Actor, household_id: str) -> list[Consent]:
        with self._conn(actor) as c:
            rows = c.execute(f"SELECT {_C_COLS} FROM parental_consents WHERE household_id = %s ORDER BY id",
                             (household_id,)).fetchall()
        return [_consent(r) for r in rows]

    def revoke_consents(self, actor: Actor, household_id: str, minor_email: str, by: str, now: float) -> int:
        with self._conn(actor) as c:
            return c.execute("UPDATE parental_consents SET revoked_at = %s, revoked_by = %s WHERE household_id = %s "
                             "AND minor_email = %s AND revoked_at IS NULL",
                             (now, by, household_id, minor_email)).rowcount


__all__ = ["MemoryHouseholdStore", "PgHouseholdStore", "already_in_household", "household_full", "new_grant_code"]
