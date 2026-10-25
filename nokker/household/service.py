"""Service du foyer (lot X23, docs/LOKKAL.md §7.29). Tous les droits sont vérifiés ici, à chaque appel.

- **Foyer = projet d'équipe `household`** : créé par le payeur (owner), qui invite par adresse de compte ; l'invité
  entre avec le rôle `member` ET une ligne `household_members` (même transaction, nokker/household/store.py). Un foyer
  par compte, 6 membres au plus (payeur compris) : comptés ici (invitations en attente comprises), refusés en base.
- **Chacun garde son locataire** : le foyer ne lit rien des fils, connecteurs, secrets ni de la mémoire de profil
  d'un membre. La vue (`view`) donne des COMPTES de tours (le ledger), jamais un contenu ; le payeur voit tous les
  membres, un membre sa propre ligne et le total du foyer.
- **Le payeur seul** pose les plafonds par membre, déclare un profil mineur, retire ou renouvelle une attestation,
  règle la liste blanche des profils mineurs, retire un membre, supprime le foyer.
- **Profils mineurs** (`household_minors`) : `teen` (13-17), `child` (< 13). Le payeur atteste être le représentant
  légal (texte versionné par son empreinte) ; l'attestation est horodatée et révocable (révocation = profil
  suspendu). Un profil mineur ne garde jamais de consentement d'entraînement (retiré ici, refusé en base) et ne quitte
  pas le foyer seul.
- **Palier du payeur** (facturation branchée, `attach_billing`) : le quota commun et les sièges sont ceux du palier ACTIF
  du payeur quand il est dans `HOUSEHOLD_PLANS` ; sinon (Solo, résilié, statut hors `ACTIVE_STATUSES` ; `past_due`
  garde le palier) le foyer est `suspended` : chaque membre revient à ses propres plafonds de compte, rien n'est effacé
  (membres, notes, attestations), invitations bloquées (402 `household_suspended`), message clair au payeur dans la
  vue. Facturation éteinte ou illisible : `household_settings` (`Tier`, nokker/household/model.py).
- **Mémoire commune** : les notes de projet du foyer (`team_notes`, mêmes règles que les notes d'équipe : caviardage,
  la dernière reçue gagne, l'autre en quarantaine) ; le rappel dans les tours est dans nokker/household/memory.py.

Erreurs : `TeamError(status, code, message)`, servies par nokker/household/http.py.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nokker.billing.plans import HOUSEHOLD_PLANS
from nokker.household import model as hm
from nokker.household.model import Consent, HouseholdMember, Policy, Settings, Tier
from nokker.team import model as tm
from nokker.team.model import Invite, Member, Project, TeamError
from nokker.team.service import Team, household_access, note_json

logger = logging.getLogger("nokker.household")


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=UTC).isoformat()


def no_household() -> TeamError:
    return TeamError(404, "not_found", "aucun foyer")


def payer_only() -> TeamError:
    return TeamError(403, "forbidden", "réservé au payeur du foyer")


def suspended() -> TeamError:
    return TeamError(402, "household_suspended", "foyer suspendu : l'abonnement du payeur ne comprend plus de foyer "
                                                 "(Famille ou Large) ; aucune invitation tant qu'il n'est pas repris")


# Vue du payeur d'un foyer suspendu (`notice`) : ce qui se passe et comment le réactiver, FR puis EN.
SUSPENDED_NOTICE = (
    "Ton foyer est suspendu : ton abonnement actuel ne comprend pas de foyer (palier Famille ou Large). Chaque membre "
    "utilise ses propres plafonds de compte, rien n'est effacé (membres, notes, réglages), et aucune invitation ne part "
    "tant que le foyer est suspendu. Reprends un abonnement Famille ou Large depuis « Mon compte » pour le réactiver.\n\n"
    "Your household is suspended: your current subscription does not include a household (Family or Large plan). Each "
    "member uses their own account limits, nothing is deleted (members, notes, settings), and no invitation goes out "
    "while the household is suspended. Subscribe to Family or Large again from “My account” to reactivate it.")


@dataclass(frozen=True)
class MinorState:
    """Ce que le noyau applique à un tour d'un profil mineur (nokker/household/guard.py)."""

    profile: str
    active: bool  # attestation en cours, profil non suspendu, drapeau allumé
    guardian: str  # compte du représentant (le payeur) : lui seul décide les cartes fortes
    household_id: str
    policy: Policy


@dataclass
class Household:
    team: Team
    settings: Settings = field(default_factory=Settings)
    quota: Any = None  # nokker/household/quota.py::HouseholdQuota (compte des tours) ; None = non décompté
    consents: Any = None  # nokker/onboarding/consents.py (data_consents) : le retrait du consentement d'entraînement
    # Locataire d'un membre dont le registre, le préfixe ou les règles changent (profil, attestation, liste blanche,
    # entrée, sortie) : le serveur y branche `TenantRegistry.forget`.
    on_change: Callable[[str], None] | None = None
    # Facturation (train) : locataire du payeur → palier actif ; None = facturation éteinte, aucun contrôle de plus.
    payer_plan: Callable[[str], str | None] | None = None
    # Palier → ses plafonds (`turns_per_day`, `turns_per_week`, `household_members`) ; posé avec `payer_plan`.
    plan_caps: Callable[[str], dict[str, Any] | None] | None = None
    clock: Callable[[], float] = field(default=time.time)

    @property
    def store(self) -> Any:
        return self.team.store

    # ── palier du payeur ──
    def attach_billing(self, billing: Any) -> bool:
        """Branche la facturation (nokker/billing/service.py::Billing) : palier actif du payeur et plafonds du palier,
        relus à chaque appel (cache de `Billing`, vidé par le webhook). Aucun palier de foyer en vente (pas de
        `billing_prices` pour `HOUSEHOLD_PLANS`) : rien n'est branché, `household_settings` s'applique — sinon aucun
        payeur ne pourrait avoir de foyer. Rend vrai si la facturation est branchée."""
        if not any(p in (getattr(billing, "prices", None) or {}) for p in HOUSEHOLD_PLANS):
            logger.info("foyer : aucun palier de foyer en vente (%s sans price_id) : réglages household_settings",
                        "/".join(sorted(HOUSEHOLD_PLANS)))
            return False
        self.payer_plan = lambda tenant_id: billing.active_plan(tenant_id)
        self.plan_caps = lambda plan: dict(billing.plans[plan].caps) if plan in billing.plans else None
        return True

    def tier(self, payer_tenant: str) -> Tier:
        """Le palier qui s'applique au foyer du locataire `payer_tenant` (celui du payeur). Facturation éteinte :
        `household_settings`. Facturation illisible : `household_settings` aussi (jamais de blocage sur une panne,
        comme les plafonds de compte)."""
        if self.payer_plan is None:
            return Tier("active", self.settings, self.settings.plan)
        try:
            plan = self.payer_plan(payer_tenant)
            caps = self.plan_caps(plan) if plan in HOUSEHOLD_PLANS and self.plan_caps is not None else None
        except Exception:
            logger.warning("foyer : palier du payeur %s illisible, réglages household_settings", payer_tenant,
                           exc_info=True)
            return Tier("active", self.settings, self.settings.plan)
        if plan not in HOUSEHOLD_PLANS:
            return Tier("suspended", self.settings, plan)
        return Tier("active", self.settings.for_plan(plan, caps), plan)

    # ── accès ──
    def _mine(self, actor: Any) -> tuple[Project, Member, HouseholdMember]:
        row = self.store.hh_membership(actor, actor.account_id)
        if row is None:
            raise no_household()
        with household_access():
            try:
                p, me = self.team._gate(actor, row.household_id, "viewer")
            except TeamError as e:
                if e.status == 404:
                    raise no_household() from None
                raise
        if p.kind != hm.KIND:
            raise no_household()
        return p, me, row

    def _payer(self, actor: Any) -> tuple[Project, HouseholdMember]:
        p, _, row = self._mine(actor)
        if not row.payer or p.owner_account != actor.account_id:
            raise payer_only()
        return p, row

    def _target(self, actor: Any, p: Project, account_id: str) -> HouseholdMember:
        row = next((r for r in self.store.hh_members(actor, p.id) if r.account_id == account_id), None)
        if row is None:
            raise TeamError(404, "not_found", "membre inconnu dans ce foyer")
        return row

    def _changed(self, *accounts: str) -> None:
        if self.on_change is None:
            return
        tenancy = self.team.tenancy
        for acc_id in accounts:
            acc = tenancy.account(acc_id) if tenancy is not None else None
            if acc is not None:
                try:
                    self.on_change(acc.tenant_id)
                except Exception:
                    logger.warning("foyer : cache du locataire %s non relu", acc.tenant_id, exc_info=True)

    # ── foyer ──
    def create(self, actor: Any, body: dict[str, Any]) -> dict[str, Any]:
        if self.store.hh_membership(actor, actor.account_id) is not None:
            raise TeamError(409, "household_exists", "ce compte fait déjà partie d'un foyer (un foyer par compte)")
        if self.payer_plan is not None and not self.tier(actor.tenant_id).active:
            raise TeamError(402, "plan_required",
                            f"le foyer demande un abonnement {' ou '.join(sorted(HOUSEHOLD_PLANS))}")
        if self.store.owned_count(actor) >= tm.MAX_OWNED_PROJECTS:
            raise TeamError(409, "too_many", f"{tm.MAX_OWNED_PROJECTS} projets au plus par compte")
        now = self.clock()
        p = Project(id=tm.new_project_id(), tenant_id=actor.tenant_id,
                    name=tm.clean_name(body.get("name") or "Foyer"), owner_account=actor.account_id,
                    rules={"ask": [], "deny": []}, mirror=True, created_at=now, updated_at=now, kind=hm.KIND)
        owner = Member(project_id=p.id, tenant_id=p.tenant_id, account_id=actor.account_id, role="owner",
                       email=actor.email, added_by=actor.account_id, added_at=now)
        payer = HouseholdMember(household_id=p.id, tenant_id=p.tenant_id, account_id=actor.account_id,
                                email=actor.email, payer=True, added_at=now)
        self.store.create_household(actor, p, owner, payer)
        return self.view(actor)

    def delete(self, actor: Any) -> dict[str, Any]:
        p, _ = self._payer(actor)
        others = [r.account_id for r in self.store.hh_members(actor, p.id) if not r.payer]
        self.store.delete_project(actor, p)
        self._changed(actor.account_id, *others)
        return {"deleted": True}

    def _line(self, r: HouseholdMember, me: str, usage: dict[str, Any] | None,
              settings: Settings | None = None) -> dict[str, Any]:
        cap, source = r.cap(settings or self.settings)
        used = (usage or {}).get("by_account", {}).get(r.account_id, {"today": 0, "week": 0})
        return {"account_id": r.account_id, "email": r.email, "payer": r.payer, "profile": r.profile,
                "status": r.status, "joined_at": _iso(r.added_at), "you": r.account_id == me,
                "turns": {"today": used["today"], "week": used["week"]},
                "cap": {"turns_per_day": cap, "source": source}}

    def view(self, actor: Any) -> dict[str, Any]:
        """`GET /v1/household` : membres, tours par membre (jour, semaine), plafonds — jamais de contenu."""
        p, _, mine = self._mine(actor)
        rows = self.store.hh_members(actor, p.id)
        usage = self.quota.usage(p, actor.account_id) if self.quota is not None else None
        tier = self.tier(p.tenant_id)
        s = tier.settings
        lines = [self._line(r, actor.account_id, usage, s) for r in rows
                 if mine.payer or r.account_id == actor.account_id]
        today = (usage or {}).get("today", {})
        week = (usage or {}).get("week", {})
        out: dict[str, Any] = {
            "id": p.id, "name": p.name, "created_at": _iso(p.created_at), "plan": tier.plan, "state": tier.state,
            "mode": s.mode, "max_members": s.max_members, "size": len(rows), "counted": usage is not None,
            "you": {"account_id": actor.account_id, "payer": mine.payer, "profile": mine.profile},
            # suspendu : plus de quota commun (chaque membre a ses plafonds de compte), les tours passés restent lus
            "quota": {"tz": "Europe/Zurich",
                      "today": {"used": today.get("used", 0), "cap": s.turns_per_day if tier.active else None,
                                "resets_at": today.get("resets_at")},
                      "week": {"used": week.get("used", 0), "cap": s.turns_per_week if tier.active else None,
                               "resets_at": week.get("resets_at")}},
            "members": lines,
        }
        if mine.payer and not tier.active:
            out["notice"] = SUSPENDED_NOTICE
        if mine.payer:
            now = self.clock()
            out["invitations"] = [self._invite_json(i) for i in self.store.invites_of(actor, p.id)
                                  if i.state == "pending" and i.expires_at > now]
            out["policy"] = Policy.of(p.household_policy).to_json()
            out["minors"] = {"enabled": s.minors}
        return out

    # ── invitations ──
    @staticmethod
    def _invite_json(i: Invite) -> dict[str, Any]:
        return {"id": i.id, "email": i.email, "profile": i.profile, "state": i.state,
                "created_at": _iso(i.created_at), "expires_at": _iso(i.expires_at), "decided_at": _iso(i.decided_at)}

    def invite(self, actor: Any, body: dict[str, Any], *, ip: str | None = None) -> dict[str, Any]:
        p, _ = self._payer(actor)
        tier = self.tier(p.tenant_id)
        if not tier.active:
            raise suspended()
        email = tm.clean_email(body.get("email"))
        profile = hm.clean_profile(body.get("profile"), minors=self.settings.minors)
        attestation = hm.clean_attestation(body.get("attestation")) if profile in hm.MINOR_PROFILES else None
        rows = self.store.hh_members(actor, p.id)
        now = self.clock()
        pending = [i for i in self.store.invites_of(actor, p.id) if i.state == "pending" and i.expires_at > now]
        if email == (actor.email or "").lower() or any(r.email == email for r in rows):
            raise TeamError(409, "already_member", "déjà membre du foyer")
        if any(i.email == email for i in pending):
            raise TeamError(409, "already_invited", "invitation déjà en attente pour cette adresse")
        if len(rows) + len(pending) >= tier.settings.max_members:
            raise TeamError(409, "household_full",
                            f"{tier.settings.max_members} membres au plus, payeur et invitations en attente compris")
        acc = self.team.tenancy.account_by_email(email) if self.team.tenancy is not None else None
        inv = Invite(id=tm.new_invite_id(), project_id=p.id, tenant_id=p.tenant_id, project_name=p.name, email=email,
                     role="member", invited_by=actor.account_id, invitee_account=acc.account_id if acc else "",
                     created_at=now, expires_at=now + tm.INVITE_TTL_S, kind=hm.KIND, profile=profile)
        self.store.create_invite(actor, inv)
        if attestation is not None:
            version, lang = attestation
            self.store.add_consent(actor, Consent(id=0, household_id=p.id, guardian_account=actor.account_id,
                                                  minor_email=email, invite_id=inv.id, profile=profile,
                                                  version_sha=version, lang=lang, granted_at=now, ip=ip))
        return self._invite_json(inv)

    def invitations(self, actor: Any) -> dict[str, Any]:
        p, _ = self._payer(actor)
        return {"invitations": [self._invite_json(i) for i in self.store.invites_of(actor, p.id)]}

    def revoke_invite(self, actor: Any, invite_id: str) -> dict[str, Any]:
        p, _ = self._payer(actor)
        inv = self.store.invite(actor, invite_id) if tm.is_invite_id(invite_id) else None
        if inv is None or inv.project_id != p.id:
            raise TeamError(404, "not_found", "invitation inconnue")
        if inv.state != "pending":
            raise TeamError(409, "not_pending", f"invitation déjà {inv.state}")
        self.store.set_invite_state(actor, inv, "revoked", self.clock())
        if inv.profile in hm.MINOR_PROFILES:
            self.store.revoke_consents(actor, p.id, inv.email, actor.account_id, self.clock())
        return {"revoked": True}

    def received(self, actor: Any) -> dict[str, Any]:
        now = self.clock()
        out = [{"id": i.id, "household": {"id": i.project_id, "name": i.project_name}, "profile": i.profile,
                "created_at": _iso(i.created_at), "expires_at": _iso(i.expires_at)}
               for i in self.store.invites_for(actor) if i.kind == hm.KIND and i.expires_at > now]
        return {"invitations": out}

    def accept(self, actor: Any, invite_id: str) -> dict[str, Any]:
        with household_access():
            inv = self.team._my_pending(actor, invite_id)
        if inv.kind != hm.KIND:
            raise TeamError(404, "not_found", "invitation inconnue ou expirée")
        if inv.profile in hm.MINOR_PROFILES and not self.settings.minors:
            raise TeamError(400, "minors_disabled", "les profils mineurs ne sont pas encore ouverts")
        if self.store.hh_membership(actor, actor.account_id) is not None:
            raise TeamError(409, "already_in_household", "ce compte fait déjà partie d'un foyer (un foyer par compte)")
        if not self.tier(inv.tenant_id).active:  # locataire de l'invitation = celui du foyer, donc du payeur
            raise suspended()
        now = self.clock()
        mem = Member(project_id=inv.project_id, tenant_id=inv.tenant_id, account_id=actor.account_id, role="member",
                     email=actor.email, added_by=inv.invited_by, added_at=now)
        self.store.accept(actor, inv, mem, now)
        if inv.profile in hm.MINOR_PROFILES:
            self._no_training(actor.account_id)
        self._changed(actor.account_id)
        return self.view(actor)

    def decline(self, actor: Any, invite_id: str) -> dict[str, Any]:
        with household_access():
            inv = self.team._my_pending(actor, invite_id)
        if inv.kind != hm.KIND:
            raise TeamError(404, "not_found", "invitation inconnue ou expirée")
        self.store.set_invite_state(actor, inv, "declined", self.clock())
        return {"declined": True}

    # ── membres ──
    def remove(self, actor: Any, account_id: str) -> dict[str, Any]:
        """Le payeur retire un membre ; un membre adulte quitte le foyer ; un profil mineur ne le quitte pas seul (son
        représentant le retire). Le payeur ne quitte pas : il supprime le foyer."""
        p, _, mine = self._mine(actor)
        if account_id == actor.account_id:
            if mine.payer:
                raise TeamError(409, "payer_fixed", "le payeur ne quitte pas le foyer : il le supprime")
            if mine.minor:
                raise TeamError(403, "minor_cannot_leave", "un profil mineur quitte le foyer par son représentant")
            target = mine
        else:
            if not mine.payer:
                raise payer_only()
            target = self._target(actor, p, account_id)
            if target.payer:
                raise TeamError(409, "payer_fixed", "le payeur ne se retire pas")
        self.store.remove_member(actor, p, account_id)
        if target.minor and mine.payer:
            self.store.revoke_consents(actor, p.id, target.email, actor.account_id, self.clock())
        self._changed(account_id)
        return {"removed": True}

    def set_cap(self, actor: Any, account_id: str, body: Any) -> dict[str, Any]:
        p, _ = self._payer(actor)
        cap = hm.clean_cap(body)
        row = self._target(actor, p, account_id)
        row.cap_day, row.cap_custom = cap, True
        self.store.save_hh(actor, row)
        return self._line(row, actor.account_id, None)

    def reset_cap(self, actor: Any, account_id: str) -> dict[str, Any]:
        p, _ = self._payer(actor)
        row = self._target(actor, p, account_id)
        row.cap_day, row.cap_custom = None, False
        self.store.save_hh(actor, row)
        return self._line(row, actor.account_id, None)

    # ── profils mineurs ──
    def _no_training(self, account_id: str) -> None:
        """Un profil mineur ne garde jamais de consentement d'entraînement : retrait journalisé dans SON locataire
        (une ligne `granted: false`, comme un retrait depuis « Mon compte »). La base refuse ensuite tout accord."""
        if self.consents is None:
            return
        from nokker.tenancy.context import tenant_scope

        acc = self.team.tenancy.account(account_id) if self.team.tenancy is not None else None
        if acc is None:
            return
        with tenant_scope(acc.tenant_id):
            if self.consents.current(account_id, "training"):
                self.consents.record(account_id, "training", False, "account", None, None)

    def _attest(self, actor: Any, p: Project, row: HouseholdMember, profile: str, body: Any,
                ip: str | None) -> None:
        version, lang = hm.clean_attestation((body or {}).get("attestation") if isinstance(body, dict) else None)
        now = self.clock()
        self.store.add_consent(actor, Consent(id=0, household_id=p.id, guardian_account=actor.account_id,
                                              minor_email=row.email, profile=profile, version_sha=version, lang=lang,
                                              granted_at=now, ip=ip))

    def _minor_row(self, actor: Any, p: Project, row: HouseholdMember, profile: str) -> dict[str, Any]:
        return {"account_id": row.account_id, "profile": profile, "household_id": p.id, "household_tenant": p.tenant_id,
                "guardian_account": actor.account_id, "declared_at": self.clock()}

    def set_profile(self, actor: Any, account_id: str, body: Any, *, ip: str | None = None) -> dict[str, Any]:
        """Le payeur déclare un membre `teen` ou `child` (attestation exigée), ou le repasse `adult` (majorité) :
        l'attestation en cours est alors close et le compte n'est plus un profil mineur."""
        p, _ = self._payer(actor)
        if not isinstance(body, dict):
            raise TeamError(400, "bad_request", "corps attendu : {profile, attestation?}")
        profile = hm.clean_profile(body.get("profile"), minors=self.settings.minors or body.get("profile") == "adult")
        row = self._target(actor, p, account_id)
        if row.payer:
            raise TeamError(409, "payer_fixed", "le payeur est le représentant : jamais un profil mineur")
        now = self.clock()
        if profile in hm.MINOR_PROFILES:
            self._attest(actor, p, row, profile, body, ip)
            self.store.put_minor(actor, self._minor_row(actor, p, row, profile))
            self._no_training(account_id)
            row.status = "active"
        else:
            if row.minor:
                self.store.revoke_consents(actor, p.id, row.email, actor.account_id, now)
            self.store.delete_minor(actor, account_id)
            row.status = "active"
        row.profile = profile
        self.store.save_hh(actor, row)
        self._changed(account_id)
        return self._line(row, actor.account_id, None)

    def revoke_consent(self, actor: Any, account_id: str) -> dict[str, Any]:
        """Retrait de l'attestation par le représentant : le profil est SUSPENDU (aucun tour), rien n'est effacé ici
        (l'effacement des données du mineur passe par `nokker erase`)."""
        p, _ = self._payer(actor)
        row = self._target(actor, p, account_id)
        if not row.minor:
            raise TeamError(409, "not_minor", "ce membre n'a pas de profil mineur")
        self.store.revoke_consents(actor, p.id, row.email, actor.account_id, self.clock())
        row.status = "suspended"
        self.store.save_hh(actor, row)
        self._changed(account_id)
        return self._line(row, actor.account_id, None)

    def renew_consent(self, actor: Any, account_id: str, body: Any, *, ip: str | None = None) -> dict[str, Any]:
        p, _ = self._payer(actor)
        if not self.settings.minors:
            raise TeamError(400, "minors_disabled", "les profils mineurs ne sont pas encore ouverts")
        row = self._target(actor, p, account_id)
        if not row.minor:
            raise TeamError(409, "not_minor", "ce membre n'a pas de profil mineur")
        self._attest(actor, p, row, row.profile, body, ip)
        self.store.put_minor(actor, self._minor_row(actor, p, row, row.profile))
        row.status = "active"
        self.store.save_hh(actor, row)
        self._changed(account_id)
        return self._line(row, actor.account_id, None)

    def consents_of(self, actor: Any) -> dict[str, Any]:
        """Journal des attestations (payeur) : qui, quel profil, quelle version, quand, retirée ou non."""
        p, _ = self._payer(actor)
        return {"consents": [{"id": c.id, "minor_email": c.minor_email, "profile": c.profile,
                              "version": c.version_sha, "lang": c.lang, "granted_at": _iso(c.granted_at),
                              "revoked_at": _iso(c.revoked_at), "active": c.active}
                             for c in self.store.consents(actor, p.id)]}

    @staticmethod
    def consent_document(lang: str) -> dict[str, Any]:
        t = hm.consent_text(lang)
        return {"version": t.sha256, "lang": t.lang, "text": t.text}

    def policy(self, actor: Any) -> dict[str, Any]:
        p, _ = self._payer(actor)
        return Policy.of(p.household_policy).to_json()

    def put_policy(self, actor: Any, body: Any) -> dict[str, Any]:
        p, _ = self._payer(actor)
        p.household_policy, p.updated_at = hm.clean_policy(body).to_json(), self.clock()
        self.store.save_project(actor, p)
        self._changed(*(r.account_id for r in self.store.hh_members(actor, p.id) if r.minor))
        return p.household_policy

    # ── mémoire commune (notes du foyer, mêmes règles que les notes d'équipe) ──
    def notes(self, actor: Any, since: str | None) -> dict[str, Any]:
        p, _, _ = self._mine(actor)
        with household_access():
            return self.team.notes(actor, p.id, since)

    def put_note(self, actor: Any, name: str, body: Any) -> dict[str, Any]:
        """Une note du foyer écrite depuis l'app (rule | feedback | fact). Sa portée est le foyer : une note de portée
        `user` (mémoire de profil) est refusée. `base_sha` = la version lue ; sinon la dernière reçue gagne et l'autre
        part en quarantaine, relue par le payeur."""
        p, _, _ = self._mine(actor)
        if not isinstance(body, dict):
            raise TeamError(400, "bad_note", "note : un objet")
        item = {**body, "name": name, "locality": "both"}
        with household_access():
            out = self.team.push_notes(actor, p.id, "", [item])
            got = self.store.note(actor, p, tm.clean_note(item)["name"])
        return {"note": note_json(got) if got else None, **out}

    def forget_note(self, actor: Any, name: str) -> dict[str, Any]:
        p, _, _ = self._mine(actor)
        cur = self.store.note(actor, p, name) if name else None
        if cur is None or cur.deleted:
            raise TeamError(404, "not_found", "note inconnue")
        with household_access():
            out = self.team.push_notes(actor, p.id, "", [{"name": name, "kind": cur.kind, "deleted": True,
                                                         "base_sha": cur.sha, "locality": "both"}])
        return {"forgotten": out["accepted"] == 1}

    def quarantined(self, actor: Any) -> dict[str, Any]:
        p, _ = self._payer(actor)
        with household_access():
            return self.team.quarantined(actor, p.id)

    def restore_note(self, actor: Any, quarantine_id: Any) -> dict[str, Any]:
        p, _ = self._payer(actor)
        with household_access():
            return self.team.restore_note(actor, p.id, quarantine_id)

    # ── lectures du noyau (tour d'un membre ; pas de route) ──
    def membership(self, account_id: str | None) -> tuple[Any, Project, HouseholdMember] | None:
        """(compte, foyer, ligne du membre) du compte, ou None. Lu dans le locataire courant (celui du tour)."""
        tenancy = self.team.tenancy
        acc = tenancy.account(account_id) if tenancy is not None and account_id else None
        if acc is None or getattr(acc, "status", "active") != "active":
            return None
        row = self.store.hh_membership(acc, account_id)
        if row is None:
            return None
        p = self.store.project(acc, row.household_id)
        if p is None or p.kind != hm.KIND or p.tenant_id != row.tenant_id:
            return None
        return acc, p, row

    def minor_state(self, account_id: str | None) -> MinorState | None:
        """None : pas un profil mineur. Sinon son état : `active` exige le drapeau, l'adhésion au foyer qui l'a
        déclaré, un profil non suspendu. Un compte mineur sorti de son foyer reste mineur, suspendu."""
        if not account_id:
            return None
        row = self.store.minor(account_id)
        if row is None:
            return None
        found = self.membership(account_id)
        active = False
        policy = Policy()
        if found is not None:
            _, p, mrow = found
            active = (self.settings.minors and mrow.household_id == row["household_id"] and mrow.minor
                      and mrow.status == "active")
            policy = Policy.of(p.household_policy)
        return MinorState(profile=row["profile"], active=active, guardian=row["guardian_account"],
                          household_id=row["household_id"], policy=policy)

    def is_minor(self, account_id: str | None) -> bool:
        return bool(account_id) and self.store.minor(account_id) is not None

    def guardian_for_tenant(self, tenant_id: str) -> str | None:
        """Compte du représentant d'un profil mineur, par le locataire du mineur (un compte = un locataire)."""
        tenancy = self.team.tenancy
        acc = tenancy.account_for_tenant(tenant_id) if tenancy is not None else None
        st = self.minor_state(acc.account_id) if acc is not None else None
        return st.guardian if st is not None else None

    def minors_of(self, actor: Any) -> list[HouseholdMember]:
        """Profils mineurs du foyer dont `actor` est le payeur (cartes fortes à décider)."""
        p, _ = self._payer(actor)
        return [r for r in self.store.hh_members(actor, p.id) if r.minor]


__all__ = ["SUSPENDED_NOTICE", "Household", "MinorState", "no_household", "payer_only", "suspended"]
