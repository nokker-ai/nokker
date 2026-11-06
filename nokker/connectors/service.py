"""Cartes Connecteurs (T4, docs/CONNECTORS-MULTI.md) : relier, tester, renommer, choisir le défaut, révoquer, partager.

Un connecteur n'est enregistré qu'APRÈS un test réussi, pour qu'une carte « reliée » veuille dire « a répondu au
dernier essai ». Le secret entre une fois et ne ressort jamais, ni chez le propriétaire ni chez un bénéficiaire.

Partage dans le foyer : le propriétaire demande, une carte forte lui est présentée (`GRANT_TOOL`, geste du noyau), le
partage existe à l'approbation. Descendre de niveau ou retirer : immédiat. Le bénéficiaire peut se retirer (`me`).
Quitter le foyer fait tomber les partages (`member_gone`, appelé par le foyer). Tout est journalisé
(`connector_grant_events`)."""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from nokker.connectors import TestResult, test_connection
from nokker.connectors.errors import ConnectorError
from nokker.connectors.grants import LEVELS, RANK, Grant, GrantEvent, GrantStore, check_level
from nokker.connectors.mail import PRESET_HELP, PRESETS, ZOHO_REGIONS, normalize_settings
from nokker.connectors.store import KINDS, ConnectorStore, Status, check_label, is_multi

logger = logging.getLogger("nokker.connectors")

GRANT_TOOL = "connector_grant"  # carte forte d'un partage : un geste du noyau, pas un outil du registre


def public_settings(kind: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Ce que la carte peut afficher : le compte relié et le serveur, jamais l'URL secrète d'un agenda iCal."""
    if kind == "mail":
        return {k: settings.get(k) for k in ("preset", "address", "username", "imap_host", "smtp_host", "region")
                if settings.get(k)}
    return {k: settings.get(k) for k in ("mode", "label", "username", "url") if settings.get(k)}


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def card(st: Status | None, kind: str, locality: str = "cloud") -> dict[str, Any]:
    """Carte d'une NATURE (routes d'avant 068 : le connecteur par défaut). `locality` (docs/LOKKAL.md §6.7) : « cloud »
    = posé chez nokker.ai (utilisable depuis Lokkal par relais, X20), « local » = posé dans Lokkal."""
    if st is None:
        return {"kind": kind, "state": "absent", "locality": locality}
    return {"kind": kind, "state": "ok" if st.last_ok_at else "unverified", "settings": public_settings(kind, st.settings),
            "connected_at": st.created_at.isoformat(), "last_ok_at": _iso(st.last_ok_at), "locality": locality}


def _oauth_label(kind: str, acc: Any, taken: set[str]) -> str:
    from nokker.connectors.store import LABEL_MAX

    base = (acc.email or acc.provider)[:LABEL_MAX] if kind == "mail" else \
        f"{'Google' if acc.provider == 'google' else 'Outlook'} {acc.email}"[:LABEL_MAX]
    label, n = base, 2
    while label.lower() in taken:
        label, n = f"{base[:LABEL_MAX - 4]} {n}", n + 1
    return label


def display_name(email: str) -> str:
    """Nom montré d'un membre : les comptes n'ont pas de prénom, c'est la partie locale de l'adresse."""
    return (email or "").split("@", 1)[0] or "?"


@dataclass(frozen=True)
class Member:
    account_id: str
    email: str
    tenant_id: str
    minor: bool = False

    @property
    def name(self) -> str:
        return display_name(self.email)


@dataclass(frozen=True)
class HouseholdInfo:
    household_id: str
    household_tenant: str
    members: tuple[Member, ...]

    def member(self, account_id: str) -> Member | None:
        return next((m for m in self.members if m.account_id == account_id), None)


class Households(Protocol):
    def of(self, account_id: str) -> HouseholdInfo | None: ...


@dataclass(frozen=True)
class Shared:
    """Un connecteur partagé avec moi : la ligne de partage, l'état du connecteur chez son propriétaire."""

    grant: Grant
    status: Status
    owner: Member


def fingerprint(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def grant_reason(kind: str, level: str, grantee: Member, st: Status) -> str:
    who = f"{grantee.name} ({grantee.email}{', profil mineur' if grantee.minor else ''})"
    what = f"« {st.label} »" + (f" — {st.settings.get('address')}" if kind == "mail" and st.settings.get("address") else "")
    if kind == "mail":
        how = ("en lecture seule (lire et chercher tes messages)" if level == "read" else
               "en lecture et envoi (lire tes messages, préparer des brouillons et envoyer des e-mails depuis cette "
               "boîte, en ton nom)")
        return f"donner à {who} l'accès {how} à ta boîte {what} ; tu peux le retirer à tout moment"
    how = "en lecture seule" if level == "read" else "en lecture et écriture (créer, modifier, supprimer des événements)"
    return f"donner à {who} l'accès {how} à ton agenda {what} ; tu peux le retirer à tout moment"


class Connectors:
    def __init__(self, store: ConnectorStore, tester=test_connection, resolve=None, locality: str = "cloud", *,
                 grants: GrantStore | None = None, households: Households | None = None, approvals: Any = None,
                 tenant_of: Callable[[str], str | None] | None = None, clock: Callable[[], float] = time.time,
                 on_change: Callable[[str], None] | None = None) -> None:
        # `resolve` (Lokkal, X4) : un identifiant `secret:NOM` est rangé tel quel (la référence) et lu au coffre pour
        # chaque test ; sans `resolve`, il est pris littéralement (la bêta n'a pas de coffre).
        self.store, self._test, self._resolve = store, tester, resolve
        self.oauth: Any = None  # nokker/connectors/oauth.py::OAuth, posé par le serveur quand un fournisseur est allumé
        self.locality = locality  # « cloud » chez nokker.ai, « local » dans Lokkal (docs/LOKKAL.md §6.7)
        self.grants, self.households, self.approvals = grants, households, approvals
        self._tenant_of, self.clock = tenant_of, clock
        # Locataire dont les outils changent (boîte ajoutée, renommée, retirée ; partage accordé ou retiré) : le serveur
        # y branche `TenantRegistry.forget` (description des outils relue). Les DROITS, eux, sont relus à chaque appel.
        self.on_change = on_change

    def _touch(self, *account_ids: str) -> None:
        if self.on_change is None or self._tenant_of is None:
            return
        for aid in dict.fromkeys(a for a in account_ids if a):
            try:
                t = self._tenant_of(aid)
                if t:
                    self.on_change(t)
            except Exception:  # noqa: BLE001 — un cache non vidé se relit au prochain changement
                logger.warning("connecteurs : outils de %s non relus", aid, exc_info=True)

    def _grantees(self, owner: str, connector_id: str | None = None) -> list[str]:
        if self.grants is None:
            return []
        return [g.grantee_account for g in self.grants.of_owner(owner)
                if connector_id is None or g.connector_id == connector_id]

    @property
    def multi(self) -> bool:
        """Le contrat 068 est servi (`features.connectors_multi`) : magasin multi ET partages branchés."""
        return is_multi(self.store) and self.grants is not None

    def _secret(self, secret: str) -> str:
        if self._resolve is None:
            return secret
        from nokker.secrets.base import SecretsError

        try:
            return self._resolve(secret)
        except SecretsError as e:
            raise ConnectorError("bad_settings", str(e)) from None

    def presets(self) -> dict[str, Any]:
        """`mail` : noms des préréglages (inchangé) ; `mail_help` : aide d'une ligne `{préréglage: {fr, en}}` ;
        `mail_regions` : valeurs du réglage `region` d'un préréglage ; `mail_soon` : affichés « bientôt » (OAuth2)."""
        oauth = self.oauth.presets() if self.oauth is not None else []
        ms_on = any(p["provider"] == "microsoft" and p["enabled"] for p in oauth)
        out = {"mail": sorted(k for k, v in PRESETS.items() if v.get("auth") != "oauth"),
               "calendar": ["ical", "caldav"], "mail_help": PRESET_HELP,
               "mail_regions": {"zoho": list(ZOHO_REGIONS)}, "mail_soon": [] if ms_on else ["outlook"]}
        if self.oauth is not None:
            out["oauth"] = oauth
            out["kinds"] = ["mail", "calendar"]
        return out

    def _tested(self, kind: str, settings: dict[str, Any], secret: str) -> tuple[dict[str, Any], str, TestResult]:
        if kind not in KINDS:
            raise ConnectorError("bad_settings", f"nature inconnue : {kind}")
        if kind == "mail":
            settings = normalize_settings(settings)
        secret = str(secret or "").strip()
        if (isinstance(settings, dict) and settings.get("auth") == "oauth") or secret.startswith("oauth:"):
            # un compte OAuth se relie par le flux du fournisseur (§3), jamais par la route des mots de passe
            raise ConnectorError("unsupported", "ce compte se relie par la connexion de son fournisseur (Microsoft, Google)")
        r: TestResult = self._test(kind, settings, self._secret(secret))
        if not r.ok:
            raise ConnectorError(r.code or "protocol_error", r.message)
        return settings, secret, r

    # ── routes par nature (avant 068) : le connecteur par défaut ────────────────────────────────────────────────
    def cards(self, account_id: str) -> list[dict[str, Any]]:
        by_kind: dict[str, Status] = {}
        for s in self.store.statuses(account_id):
            if s.is_default or s.kind not in by_kind:
                by_kind[s.kind] = s
        return [card(by_kind.get(k), k, self.locality) for k in KINDS]

    def connect(self, account_id: str, kind: str, settings: dict[str, Any], secret: str) -> dict[str, Any]:
        """Teste puis enregistre (remplace le connecteur par défaut). Lève `ConnectorError` si le test échoue."""
        settings, secret, r = self._tested(kind, settings, secret)
        self.store.put(account_id, kind, settings, secret)
        self.store.mark_ok(account_id, kind)
        st = next(s for s in self.store.statuses(account_id) if s.kind == kind and s.is_default)
        return {**card(st, kind, self.locality), "message": r.message}

    def test(self, account_id: str, kind: str) -> dict[str, Any]:
        cred = self.store.get(account_id, kind)
        if cred is None:
            raise ConnectorError("not_configured", "connecteur non relié")
        return self._retest(account_id, cred)

    def revoke(self, account_id: str, kind: str) -> bool:
        st = next((s for s in self.store.statuses(account_id) if s.kind == kind and s.is_default), None)
        if st is None:
            return False
        if st.id and self.multi:
            self.remove(account_id, st.id)
            return True
        return self.store.delete(account_id, kind)

    def _retest(self, account_id: str, cred: Any) -> dict[str, Any]:
        r = self._test(cred.kind, cred.settings, self._secret(cred.secret))
        if r.ok:
            if cred.id and is_multi(self.store):
                self.store.mark_ok_id(account_id, cred.id)
            else:
                self.store.mark_ok(account_id, cred.kind)
        return {"ok": r.ok, "message": r.message, "code": r.code}

    # ── plusieurs connecteurs (068) ─────────────────────────────────────────────────────────────────────────────
    def _need_multi(self) -> None:
        if not self.multi:
            raise ConnectorError("unsupported", "plusieurs connecteurs par nature : pas sur cette instance")

    def _own(self, account_id: str, connector_id: str) -> Status:
        st = next((s for s in self.store.statuses(account_id) if s.id == connector_id), None)
        if st is None:
            raise ConnectorError("not_found", "connecteur inconnu")
        return st

    def _hh(self, account_id: str) -> HouseholdInfo | None:
        return self.households.of(account_id) if self.households is not None else None

    def _card_own(self, st: Status, grants: list[Grant], hh: HouseholdInfo | None) -> dict[str, Any]:
        out = {"id": st.id, "kind": st.kind, "label": st.label, "default": st.is_default, **card(st, st.kind, self.locality),
               "access": "owner", "grants": [], **self._auth_view(st, mine=True)}
        for g in grants:
            m = hh.member(g.grantee_account) if hh else None
            e: dict[str, Any] = {"account_id": g.grantee_account, "name": m.name if m else "?",
                                 "minor": bool(m and m.minor)}
            if g.level:
                e.update(level=g.level, status="active", granted_at=_iso(g.granted_at))
                if g.pending_level:
                    e.update(pending_level=g.pending_level, approval_id=g.approval_id)
            else:
                e.update(level=g.pending_level, status="pending", approval_id=g.approval_id)
            out["grants"].append(e)
        return out

    def _card_shared(self, sh: Shared) -> dict[str, Any]:
        st, g = sh.status, sh.grant
        out = {"id": st.id, "kind": st.kind, "label": st.label, "default": False,
               "state": "ok" if st.last_ok_at else "unverified", "connected_at": _iso(st.created_at),
               "last_ok_at": _iso(st.last_ok_at), "locality": self.locality, "access": g.level,
               "owner": {"account_id": sh.owner.account_id, "name": sh.owner.name}, "granted_at": _iso(g.granted_at)}
        if st.kind == "mail" and st.settings.get("address"):
            out["settings"] = {"address": st.settings["address"]}
        view = self._auth_view(st, mine=False, owner_tenant=g.tenant_id)
        if view.get("state") == "reconnect":
            out["state"] = "reconnect"
        return {**out, **{k: v for k, v in view.items() if k != "state"}}

    def _auth_view(self, st: Status, *, mine: bool, owner_tenant: str = "") -> dict[str, Any]:
        """`auth`, `provider`, et pour MES connecteurs OAuth `oauth_account {id, email, state, connectors}` ; `state`
        `reconnect` quand le compte relié doit être reconnecté (aussi montré au bénéficiaire, sans l'adresse)."""
        oa_id = st.settings.get("oauth_account")
        if st.settings.get("auth") != "oauth" or not oa_id:
            return {"auth": "password"}
        out: dict[str, Any] = {"auth": "oauth", "provider": st.settings.get("provider")}
        if self.oauth is None:
            return out
        from contextlib import nullcontext

        from nokker.tenancy.context import tenant_scope

        with nullcontext() if mine else tenant_scope(owner_tenant):
            hit = self.oauth.store.get(oa_id)
            n = len(self._of_oauth(st.account_id, oa_id)) if mine else 0
        state = hit[0].state if hit else "reconnect"
        if state == "reconnect":
            out["state"] = "reconnect"
        if mine and hit:
            out["oauth_account"] = {"id": oa_id, "email": hit[0].email, "state": state, "connectors": n}
        return out

    def shared(self, account_id: str) -> list[Shared]:
        """Les connecteurs partagés AVEC ce compte, encore valides (même foyer, connecteur toujours là). Lu dans son
        locataire (politique `grant_grantee_read`), puis l'état du connecteur dans le locataire du propriétaire."""
        if self.grants is None:
            return []
        hh = self._hh(account_id)
        if hh is None:
            return []
        from nokker.tenancy.context import tenant_scope

        out = []
        for g in self.grants.shared_with(account_id):
            owner = hh.member(g.owner_account)
            if owner is None or g.household_id != hh.household_id or hh.member(account_id) is None:
                continue
            with tenant_scope(g.tenant_id):
                st = next((s for s in self.store.statuses(g.owner_account) if s.id == g.connector_id), None)
            if st is not None:
                out.append(Shared(g, st, owner))
        return out

    def listing(self, account: Any) -> dict[str, Any]:
        aid = account.account_id
        if not self.multi:
            return {"connectors": self.cards(aid), "presets": self.presets()}
        hh = self._hh(aid)
        by_cid: dict[str, list[Grant]] = {}
        for g in self.grants.of_owner(aid):
            by_cid.setdefault(g.connector_id, []).append(g)
        mine = [self._card_own(s, by_cid.get(s.id, []), hh) for s in self.store.statuses(aid)]
        shared = [self._card_shared(sh) for sh in self.shared(aid)]
        members = [{"account_id": m.account_id, "name": m.name, "minor": m.minor}
                   for m in (hh.members if hh else ()) if m.account_id != aid]
        return {"connectors": mine + shared, "members": members, "presets": self.presets()}

    def add(self, account: Any, kind: str, label: str, settings: dict[str, Any], secret: str) -> dict[str, Any]:
        self._need_multi()
        if label:
            label = check_label(label)
        if sum(1 for s in self.store.statuses(account.account_id) if s.kind == kind) >= 10:
            raise ConnectorError("too_many", "au plus 10 connecteurs par nature")
        settings, secret, r = self._tested(kind, settings, secret)
        st = self.store.add(account.account_id, kind, label, settings, secret)
        self.store.mark_ok_id(account.account_id, st.id)
        self._touch(account.account_id)
        st = self._own(account.account_id, st.id)
        return {**self._card_own(st, [], self._hh(account.account_id)), "message": r.message}

    def patch(self, account: Any, connector_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._need_multi()
        aid = account.account_id
        if connector_id in {sh.status.id for sh in self.shared(aid)}:
            raise ConnectorError("not_owner", "ce connecteur est partagé avec toi : seul son propriétaire le modifie")
        self._own(aid, connector_id)
        label = body.get("label")
        if label is not None and not isinstance(label, str):
            raise ConnectorError("bad_label", "libellé invalide")
        if body.get("default") not in (None, True):
            raise ConnectorError("bad_settings", "default : seulement true (choisir un autre connecteur par défaut)")
        self.store.update(aid, connector_id, label=label, make_default=body.get("default") is True)
        self._touch(aid, *self._grantees(aid, connector_id))
        grants = [g for g in self.grants.of_owner(aid) if g.connector_id == connector_id]
        return self._card_own(self._own(aid, connector_id), grants, self._hh(aid))

    def test_id(self, account: Any, connector_id: str) -> dict[str, Any]:
        self._need_multi()
        self._own(account.account_id, connector_id)
        cred = self.store.by_id(account.account_id, connector_id)
        return self._retest(account.account_id, cred)

    def remove(self, account_id: str, connector_id: str) -> bool:
        self._need_multi()
        if connector_id in {sh.status.id for sh in self.shared(account_id)}:
            raise ConnectorError("not_owner", "ce connecteur est partagé avec toi : retire ton accès (grants/me)")
        self._own(account_id, connector_id)
        gone = self._grantees(account_id, connector_id)
        for g in self.grants.delete_connector(connector_id):
            self._cancel_card(g.approval_id, "connecteur supprimé")
            if g.level:
                self._log(g, account_id, "dropped", "connecteur supprimé")
        st = self._own(account_id, connector_id)
        ok = self.store.remove(account_id, connector_id) is not None
        oa_id = st.settings.get("oauth_account")
        if ok and oa_id and self.oauth is not None and not self._of_oauth(account_id, oa_id):
            self.oauth.revoke(oa_id)  # dernier service de ce compte relié : jeton révoqué et effacé
        self._touch(account_id, *gone)
        return ok

    # ── OAuth (docs/CONNECTORS-OAUTH.md) ─────────────────────────────────────────────────────────────────────────
    def _oauth(self) -> Any:
        if self.oauth is None or not self.multi:
            raise ConnectorError("provider_off", "connexion OAuth éteinte sur cette instance")
        return self.oauth

    def _of_oauth(self, account_id: str, oa_id: str) -> list[Status]:
        return [s for s in self.store.statuses(account_id) if s.settings.get("oauth_account") == oa_id]

    def oauth_start(self, account: Any, provider: str, body: dict[str, Any]) -> dict[str, Any]:
        oauth = self._oauth()
        services = [str(x) for x in (body.get("services") or []) if isinstance(x, str)]
        oa = str(body.get("oauth_account") or "")
        have = {k: sum(1 for s in self.store.statuses(account.account_id) if s.kind == k) for k in services}
        mine = {s.kind for s in self._of_oauth(account.account_id, oa)} if oa else set()
        if any(have.get(k, 0) >= 10 and k not in mine for k in services):
            raise ConnectorError("too_many", "au plus 10 connecteurs par nature")
        label = str(body.get("label") or "")
        if label:
            label = check_label(label)
        hh = self._hh(account.account_id)
        return oauth.start(tenant_id=self._current_tenant(), account_id=account.account_id, provider=provider,
                           services=services, label=label, household_id=hh.household_id if hh else "",
                           oauth_account=oa)

    def oauth_reconnect(self, account: Any, oa_id: str) -> dict[str, Any]:
        oauth = self._oauth()
        hit = oauth.store.get(oa_id)
        if hit is None or hit[0].account_id != account.account_id:
            raise ConnectorError("not_found", "compte relié inconnu")
        acc = hit[0]
        return oauth.start(tenant_id=self._current_tenant(), account_id=account.account_id, provider=acc.provider,
                           services=list(acc.services), oauth_account=oa_id, login_hint=acc.email)

    def oauth_delete(self, account: Any, oa_id: str) -> bool:
        oauth = self._oauth()
        hit = oauth.store.get(oa_id)
        if hit is None or hit[0].account_id != account.account_id:
            raise ConnectorError("not_found", "compte relié inconnu")
        for st in self._of_oauth(account.account_id, oa_id):
            self.remove(account.account_id, st.id)  # le dernier retiré révoque déjà le compte relié
        if oauth.store.get(oa_id) is not None:  # compte sans connecteur (services tous refusés) : révoqué ici
            oauth.revoke(oa_id)
        return True

    def on_oauth_linked(self, account_id: str, acc: Any, services: list[str], label: str = "") -> list[str]:
        """Callback OAuth (locataire du profil) : un connecteur 068 par service accordé, s'il n'existe pas déjà pour ce
        compte relié. Le « secret » rangé est la référence `oauth:<id>`, jamais un jeton."""
        have = {s.kind for s in self._of_oauth(account_id, acc.id)}
        made = []
        for kind in services:
            if kind in have:
                continue
            base = {"auth": "oauth", "provider": acc.provider, "oauth_account": acc.id}
            if kind == "mail":
                if acc.provider != "microsoft":
                    continue
                settings = normalize_settings({**base, "preset": "outlook", "address": acc.email})
            elif kind == "calendar":
                settings = {**base, "mode": "google" if acc.provider == "google" else "graph", "email": acc.email}
            else:
                continue  # natures suivantes (contacts, fichiers, tâches) : même chemin, livrées à part
            taken = {s.label.lower() for s in self.store.statuses(account_id) if s.kind == kind}
            want = label if label and label.lower() not in taken else ""
            st = self.store.add(account_id, kind, want or _oauth_label(kind, acc, taken), settings, f"oauth:{acc.id}")
            self.store.mark_ok_id(account_id, st.id)  # le fournisseur vient de délivrer le jeton : compte joignable
            made.append(st.id)
        self._touch(account_id, *self._grantees(account_id))
        return made

    # ── partages ────────────────────────────────────────────────────────────────────────────────────────────────
    def _log(self, g: Grant, actor: str, event: str, reason: str = "", level: str | None = None) -> None:
        try:
            self.grants.log(GrantEvent(g.connector_id, g.owner_account, g.grantee_account, actor, event,
                                       level or g.level, reason))
        except Exception:  # noqa: BLE001 — le journal ne bloque pas un retrait (l'accès est déjà fermé)
            logger.exception("journal des partages : écriture en échec")

    def _card_pending(self, approval_id: str | None) -> bool:
        if not approval_id or self.approvals is None:
            return False
        a = self.approvals.get(approval_id)
        return a is not None and getattr(a, "status", "") == "pending"

    def _cancel_card(self, approval_id: str | None, why: str) -> None:
        if self._card_pending(approval_id):
            try:
                self.approvals.mark_failed(approval_id, why)
            except Exception:  # noqa: BLE001 — décidée entre-temps : rien à retirer
                logger.warning("carte de partage %s non retirée", approval_id, exc_info=True)

    def grant(self, account: Any, connector_id: str, grantee_id: str, level: str) -> tuple[int, dict[str, Any]]:
        """(statut HTTP, corps). Monter ou accorder : carte forte (202 pending). Même niveau : 200 unchanged.
        Descendre : immédiat (200 updated). Une demande déjà en attente pour ce niveau : la même carte (200 pending)."""
        self._need_multi()
        aid = account.account_id
        if connector_id in {sh.status.id for sh in self.shared(aid)}:
            raise ConnectorError("not_owner", "seul le propriétaire d'un connecteur le partage")
        st = self._own(aid, connector_id)
        level = check_level(st.kind, str(level or ""))
        hh = self._hh(aid)
        grantee = hh.member(grantee_id) if hh else None
        if hh is None or grantee is None or grantee_id == aid:
            raise ConnectorError("not_household_member", "partage possible seulement avec un autre membre de ton foyer")
        g = self.grants.get(connector_id, grantee_id)
        if g is not None and g.pending_level == level and self._card_pending(g.approval_id):
            return 200, {"status": "pending", "approval_id": g.approval_id}
        if g is not None and g.level == level:
            if g.pending_level:
                self._cancel_card(g.approval_id, "demande remplacée")
                self.grants.put(replace(g, pending_level=None, approval_id=None))
            return 200, {"status": "unchanged"}
        if g is not None and g.level and RANK[level] < RANK[g.level]:  # descendre : immédiat, sans carte
            self._cancel_card(g.approval_id, "demande remplacée")
            self.grants.put(replace(g, level=level, pending_level=None, approval_id=None))
            self._log(g, aid, "granted", "niveau abaissé", level=level)
            self._touch(aid, grantee_id)
            return 200, {"status": "updated"}
        if self.approvals is None:
            raise ConnectorError("unsupported", "approbations fortes absentes sur cette instance : pas de partage")
        if g is not None:
            self._cancel_card(g.approval_id, "demande remplacée")
        approval_id = self._request_card(aid, st, grantee, level)
        base = g or Grant(tenant_id=self._current_tenant(), connector_id=connector_id, owner_account=aid,
                          grantee_account=grantee_id, household_id=hh.household_id, household_tenant=hh.household_tenant,
                          kind=st.kind)
        self.grants.put(replace(base, pending_level=level, approval_id=approval_id, household_id=hh.household_id,
                                household_tenant=hh.household_tenant))
        return 202, {"status": "pending", "approval_id": approval_id}

    @staticmethod
    def _current_tenant() -> str:
        from nokker.tenancy.context import current_tenant

        return current_tenant()

    def _request_card(self, owner: str, st: Status, grantee: Member, level: str) -> str:
        from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
        from nokker.tenancy.context import scoped_thread_id

        fp = fingerprint(owner, st.id, grantee.account_id, level)
        turn = Turn(thread_id=scoped_thread_id(self._current_tenant(), f"connector-grant-{owner}"), index=0,
                    input=InputCheck(text=""), channel="kernel", sender=owner)
        call = ToolCall(id=f"grant-{fp[:12]}-{secrets.token_hex(3)}", name=GRANT_TOOL,
                        arguments={"owner": owner, "connector_id": st.id, "grantee": grantee.account_id,
                                   "grantee_email": grantee.email, "level": level, "fingerprint": fp})
        verdict = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="connector:grant",
                          reason=grant_reason(st.kind, level, grantee, st), subject=call.id, strength="strong")
        return self.approvals.request(turn, call, verdict, None).id

    def on_grant_decision(self, approval: Any) -> str:
        """Décision de la carte forte (exécuteur d'approbation, locataire du fil de la carte = celui du propriétaire)."""
        args = approval.arguments or {}
        owner, cid, grantee_id = str(args.get("owner") or ""), str(args.get("connector_id") or ""), str(args.get("grantee") or "")
        g = self.grants.get(cid, grantee_id)
        if g is None or g.approval_id != approval.id or g.owner_account != owner:
            return "demande de partage périmée : rien n'a changé"
        cleared = replace(g, pending_level=None, approval_id=None)
        if approval.status != "approved":
            if g.level:
                self.grants.put(cleared)
            else:
                self.grants.delete(cid, grantee_id)
            return "partage refusé"
        hh = self._hh(owner)
        st = next((s for s in self.store.statuses(owner) if s.id == cid), None)
        level = str(args.get("level") or "")
        if (hh is None or hh.member(grantee_id) is None or hh.household_id != g.household_id or st is None
                or level != g.pending_level
                or fingerprint(owner, cid, grantee_id, level) != args.get("fingerprint")):
            if g.level:
                self.grants.put(cleared)
            else:
                self.grants.delete(cid, grantee_id)
            raise ConnectorError("stale", "foyer, connecteur ou niveau changé depuis la demande : rien n'est partagé")
        now = datetime.fromtimestamp(self.clock(), tz=UTC)
        self.grants.put(replace(cleared, level=level, granted_at=now))
        self._log(g, getattr(approval, "decided_by", None) or owner, "granted", level=level)
        self._touch(owner, grantee_id)
        name = hh.member(grantee_id).name
        return f"{name} a maintenant l'accès {'lecture seule' if level == 'read' else 'complet'} à « {st.label} »"

    def revoke_grant(self, account: Any, connector_id: str, who: str) -> bool:
        """`who` = un compte (le propriétaire retire) ou `me` (le bénéficiaire se retire)."""
        self._need_multi()
        aid = account.account_id
        if who == "me" or who == aid:
            sh = next((s for s in self.shared(aid) if s.status.id == connector_id), None)
            if sh is None:
                raise ConnectorError("not_found", "aucun accès à ce connecteur")
            from nokker.tenancy.context import tenant_scope

            with tenant_scope(sh.grant.tenant_id):
                g = self.grants.delete(connector_id, aid)
                if g is not None:
                    self._log(g, aid, "revoked", "retiré par le bénéficiaire")
            self._touch(aid, sh.grant.owner_account)
            return g is not None
        if connector_id in {sh.status.id for sh in self.shared(aid)}:
            raise ConnectorError("not_owner", "seul le propriétaire retire l'accès d'un autre membre")
        self._own(aid, connector_id)
        g = self.grants.delete(connector_id, who)
        if g is None:
            raise ConnectorError("not_found", "aucun partage avec ce membre")
        self._cancel_card(g.approval_id, "partage retiré")
        if g.level:
            self._log(g, aid, "revoked", "retiré par le propriétaire")
        self._touch(aid, who)
        return True

    def member_gone(self, household_id: str, account_id: str, remaining: list[Member]) -> int:
        """Un membre quitte le foyer (ou en est retiré, ou le foyer est supprimé) : ses partages donnés et reçus dans
        ce foyer tombent, journalisés `dropped`. La politique les fermait déjà ; ici les lignes partent."""
        if self.grants is None:
            return 0
        from nokker.tenancy.context import tenant_scope

        n = 0
        leaver_tenant = self._tenant_of(account_id) if self._tenant_of else None
        if leaver_tenant:
            with tenant_scope(leaver_tenant):
                for g in self.grants.of_owner(account_id):
                    if g.household_id == household_id and self.grants.delete(g.connector_id, g.grantee_account):
                        self._cancel_card(g.approval_id, "sortie du foyer")
                        if g.level:
                            self._log(g, account_id, "dropped", "sortie du foyer")
                        n += 1
        for m in remaining:
            if m.account_id == account_id or not m.tenant_id:
                continue
            with tenant_scope(m.tenant_id):
                for g in self.grants.of_owner(m.account_id):
                    if g.grantee_account == account_id and g.household_id == household_id \
                            and self.grants.delete(g.connector_id, account_id):
                        self._cancel_card(g.approval_id, "sortie du foyer")
                        if g.level:
                            self._log(g, account_id, "dropped", "sortie du foyer")
                        n += 1
        self._touch(account_id, *(m.account_id for m in remaining))
        return n


__all__ = ["GRANT_TOOL", "Connectors", "HouseholdInfo", "Households", "LEVELS", "Member", "Shared", "card",
           "display_name", "grant_reason", "public_settings"]
