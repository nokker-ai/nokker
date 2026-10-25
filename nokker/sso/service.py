"""SSO OIDC des équipes (drapeau `sso`, docs/LOKKAL.md §7.24) : configuration par le propriétaire, connexion
Authorization Code + PKCE, liaison au compte, groupes → rôles, « SSO obligatoire », journal.

**Connexion (publique, pas encore de session).**
- `start` : le navigateur donne la connexion (`sc_…`, ou son e-mail dont le domaine PROUVÉ désigne la connexion) et la
  clé publique de l'appareil qu'il vient de générer (comme pour le code e-mail). Le noyau tire `state`, `nonce`,
  `code_verifier` (PKCE S256), garde l'empreinte du `state` avec le reste, 10 minutes, et rend l'URL du fournisseur.
- `callback` : le navigateur revient avec `state` et `code`, et SIGNE `"<state>.oidc"` avec la clé de l'appareil :
  la connexion est liée au navigateur qui l'a lancée (un `state` volé ou injecté ne sert à personne d'autre). Le
  `state` est consommé AVANT tout le reste (usage unique, même en cas d'échec). Puis échange du code (secret du
  client lu au coffre à ce moment-là), vérification stricte de l'ID token (nokker/sso/idtoken.py), e-mail vérifié par
  le fournisseur, domaine prouvé de CETTE connexion.
- Compte : l'identité (issuer, sub) d'abord ; sinon le compte nokker.ai du même e-mail (tous nos comptes ont prouvé
  leur adresse à la création : lien d'invitation, code, ou SSO sur un domaine prouvé) ; sinon un compte neuf, dans son
  propre locataire. Un compte déjà lié à un AUTRE sujet du même fournisseur est refusé (409 `account_conflict`).
- Appareil : enrôlé avec la clé donnée au `start`, confirmé « sso:<connexion> » (connexion seulement, ne décide
  jamais une carte, comme le code e-mail) ; le navigateur ouvre ensuite sa session par le défi signé habituel
  (`POST /v1/sessions`).
- Rôles : pour chaque projet lié à la connexion, le rôle le plus haut que donnent les groupes ; une adhésion tenue par
  le SSO suit les groupes à CHAQUE connexion (groupe retiré = rôle retiré) ; une adhésion venue d'une invitation n'est
  touchée que si un groupe lui donne un rôle. Le propriétaire n'est jamais touché, et `owner` ne s'obtient jamais.

**SSO obligatoire** (`required` sur le lien) : un membre (pas le propriétaire, qui garde la main pour réparer la
configuration) n'accède aux ressources du projet que par une session née d'une connexion SSO de cette connexion,
vieille de moins de `max_age_s` ; une machine liée ou un tour d'agent, sans session web, ont besoin d'une connexion
SSO du compte aussi récente.

**Journal** (`sso_audit`, locataire de la connexion) : connexions réussies et refusées (avec la raison), adhésions et
rôles posés ou retirés, changements de configuration. Jamais de jeton, de code, de `state`, de `nonce`, de secret.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets as pysecrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from nokker.sso import model as m
from nokker.sso.idp import Idp, IdpError
from nokker.sso.idtoken import TokenInvalid, verify_id_token
from nokker.sso.model import Binding, Connection, Domain, Flow, Identity, SsoError
from nokker.sso.secrets import SsoSecrets
from nokker.sso.store import SsoStore
from nokker.team.model import MAX_MEMBERS, Member, Project, TeamError, not_found
from nokker.tenancy.context import tenant_scope

logger = logging.getLogger("nokker.sso")

SCOPES = "openid email profile"
MAX_GROUPS = 500


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=UTC).isoformat()


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@dataclass(frozen=True)
class _System:
    """Acteur des écritures du SSO dans le locataire du projet (celui de la connexion et du propriétaire) : aucun
    compte, donc aucune politique d'équipe ne s'ouvre ; seule `tenant_isolation` de ce locataire s'applique."""

    tenant_id: str
    account_id: str = ""
    email: str = ""


def _groups(claims: dict[str, Any], name: str) -> list[str]:
    v = claims.get(name)
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [g for g in v if isinstance(g, str) and 0 < len(g) <= 256][:MAX_GROUPS]


@dataclass
class Sso:
    store: SsoStore
    team: Any  # nokker.team.service.Team
    tenancy: Any
    approvals: Any  # BaseApprovals : enrôlement et confirmation de l'appareil
    secrets: SsoSecrets
    idp: Idp
    redirect_uri: str
    resolve_txt: Callable[[str], list[str]] | None = None  # enregistrements TXT d'un nom ; None = preuve impossible
    clock: Callable[[], float] = field(default=time.time)
    # Profil mineur d'un foyer (lot X23, `Household.is_minor`) : jamais de connexion SSO (403 `minor_account`), le
    # compte d'un mineur reste rattaché à son représentant. None (drapeau éteint) : aucun contrôle.
    is_minor: Callable[[str], bool] | None = None

    # ─────────────────────────── journal ───────────────────────────

    def _audit(self, tenant: str, cid: str, event: str, *, actor: str = "", email: str = "", project_id: str = "",
               detail: dict[str, Any] | None = None) -> None:
        row = {"connection_id": cid, "at": self.clock(), "event": event, "actor": actor, "email": email,
               "project_id": project_id, "detail": detail or {}}
        try:
            with tenant_scope(tenant):
                self.store.add_audit(row)
        except Exception:
            logger.exception("journal SSO en échec (%s, %s)", cid, event)

    def audit(self, actor: Any, cid: str, query: dict[str, Any]) -> dict[str, Any]:
        c = self._own(actor, cid)
        try:
            limit = max(1, min(int(query.get("limit") or 100), 500))
        except (TypeError, ValueError):
            raise SsoError(400, "bad_request", "limit : un entier") from None
        rows = self.store.audit_of(c.id, limit)
        return {"events": [{"at": _iso(r["at"]), "event": r["event"], "account": r["actor"], "email": r["email"],
                            "project_id": r["project_id"], "detail": r["detail"]} for r in rows]}

    # ─────────────────────────── configuration (propriétaire) ───────────────────────────

    def _own(self, actor: Any, cid: str) -> Connection:
        c = self.store.connection(cid) if m.is_connection_id(cid) else None
        if c is None or c.owner_account != actor.account_id or c.tenant_id != actor.tenant_id:
            raise not_found()
        return c

    def _connection_json(self, c: Connection) -> dict[str, Any]:
        return {"id": c.id, "name": c.name, "issuer": c.issuer, "client_id": c.client_id,
                "client_secret": {"name": c.secret_name, "set": self.secrets.has(c.tenant_id, c.secret_name)},
                "groups_claim": c.groups_claim, "token_auth": c.token_auth, "redirect_uri": self.redirect_uri,
                "domains": [self._domain_json(d) for d in self.store.domains(c.id)],
                "projects": [b.project_id for b in self.store.bindings_of(c.id)],
                "created_at": _iso(c.created_at), "updated_at": _iso(c.updated_at)}

    @staticmethod
    def _domain_json(d: Domain) -> dict[str, Any]:
        return {"domain": d.domain, "verified": d.verified_at is not None, "verified_at": _iso(d.verified_at),
                "txt": {"name": d.txt_name, "value": d.txt_value}}

    def list_connections(self, actor: Any) -> dict[str, Any]:
        return {"connections": [self._connection_json(c) for c in self.store.connections_of(actor.account_id)
                                if c.tenant_id == actor.tenant_id]}

    def create_connection(self, actor: Any, body: dict[str, Any]) -> dict[str, Any]:
        f = m.connection_fields(body, partial=False)
        if len(self.store.connections_of(actor.account_id)) >= m.MAX_CONNECTIONS:
            raise SsoError(409, "too_many", f"{m.MAX_CONNECTIONS} connexions SSO au plus par compte")
        now = self.clock()
        c = Connection(id=m.new_connection_id(), tenant_id=actor.tenant_id, owner_account=actor.account_id,
                       name=f["name"], issuer=f["issuer"], client_id=f["client_id"], secret_name=f["client_secret_name"],
                       groups_claim=f.get("groups_claim", "groups"),
                       token_auth=f.get("token_auth", "client_secret_basic"), created_at=now, updated_at=now)
        self.store.create_connection(c)
        self._audit(c.tenant_id, c.id, "connection_created", actor=actor.account_id,
                    detail={"issuer": c.issuer, "client_id": c.client_id})
        return self._connection_json(c)

    def get_connection(self, actor: Any, cid: str) -> dict[str, Any]:
        return self._connection_json(self._own(actor, cid))

    def patch_connection(self, actor: Any, cid: str, body: dict[str, Any]) -> dict[str, Any]:
        c = self._own(actor, cid)
        f = m.connection_fields(body, partial=True)
        for k, v in f.items():
            setattr(c, "secret_name" if k == "client_secret_name" else k, v)
        c.updated_at = self.clock()
        self.store.save_connection(c)
        self._audit(c.tenant_id, c.id, "connection_changed", actor=actor.account_id, detail={"fields": sorted(f)})
        return self._connection_json(c)

    def delete_connection(self, actor: Any, cid: str) -> dict[str, Any]:
        c = self._own(actor, cid)
        self.store.delete_connection(c)
        self.secrets.delete(c.tenant_id, c.secret_name)
        self._audit(c.tenant_id, c.id, "connection_deleted", actor=actor.account_id)
        return {"deleted": True}

    def set_client_secret(self, actor: Any, cid: str, body: dict[str, Any]) -> dict[str, Any]:
        """Écriture seule : la valeur va au coffre, sous le NOM de la connexion ; elle ne revient jamais."""
        c = self._own(actor, cid)
        value = body.get("value")
        if not isinstance(value, str) or not value or len(value) > 4096 or value != value.strip():
            raise SsoError(400, "bad_value", "value : le secret du client, 4096 caractères au plus, sans espace autour")
        self.secrets.set(c.tenant_id, c.secret_name, value)
        self._audit(c.tenant_id, c.id, "client_secret_set", actor=actor.account_id, detail={"name": c.secret_name})
        return {"client_secret": {"name": c.secret_name, "set": True}}

    def add_domain(self, actor: Any, cid: str, body: dict[str, Any]) -> dict[str, Any]:
        c = self._own(actor, cid)
        domain = m.clean_domain(body.get("domain"))
        existing = self.store.domains(c.id)
        hit = next((d for d in existing if d.domain == domain), None)
        if hit is not None:
            return self._domain_json(hit)
        if len(existing) >= m.MAX_DOMAINS:
            raise SsoError(409, "too_many", f"{m.MAX_DOMAINS} domaines au plus")
        d = Domain(connection_id=c.id, tenant_id=c.tenant_id, domain=domain, token=pysecrets.token_hex(16),
                   created_at=self.clock())
        self.store.put_domain(d)
        self._audit(c.tenant_id, c.id, "domain_added", actor=actor.account_id, detail={"domain": domain})
        return self._domain_json(d)

    def verify_domain(self, actor: Any, cid: str, domain: str) -> dict[str, Any]:
        """Preuve DNS : `_nokker-sso.<domaine>` porte `nokker-sso=<jeton>`. Sans elle, le domaine n'ouvre rien : un
        fournisseur ne peut pas affirmer des adresses d'un domaine qu'il ne tient pas."""
        c = self._own(actor, cid)
        d = next((x for x in self.store.domains(c.id) if x.domain == str(domain or "").lower()), None)
        if d is None:
            raise SsoError(404, "not_found", "domaine inconnu")
        if d.verified_at is not None:
            return self._domain_json(d)
        if self.resolve_txt is None:
            raise SsoError(503, "dns_unavailable", "vérification DNS non configurée sur cette instance")
        try:
            records = [str(r).strip().strip('"') for r in (self.resolve_txt(d.txt_name) or [])]
        except Exception:  # noqa: BLE001 — résolveur injoignable : on ne conclut rien
            raise SsoError(503, "dns_unavailable", "résolution DNS impossible pour le moment") from None
        if d.txt_value not in records:
            raise SsoError(409, "not_verified", f"enregistrement TXT {d.txt_name} absent ou différent")
        now = self.clock()
        if not self.store.claim_domain(d, now):
            raise SsoError(409, "domain_claimed", "ce domaine est déjà prouvé par une autre connexion SSO")
        d.verified_at = now
        self._audit(c.tenant_id, c.id, "domain_verified", actor=actor.account_id, detail={"domain": d.domain})
        return self._domain_json(d)

    def delete_domain(self, actor: Any, cid: str, domain: str) -> dict[str, Any]:
        c = self._own(actor, cid)
        if not self.store.delete_domain(c, str(domain or "").lower()):
            raise SsoError(404, "not_found", "domaine inconnu")
        self._audit(c.tenant_id, c.id, "domain_removed", actor=actor.account_id, detail={"domain": domain})
        return {"removed": True}

    # ── lien projet ↔ connexion (propriétaire du projet ET de la connexion) ──

    @staticmethod
    def _binding_json(b: Binding) -> dict[str, Any]:
        return {"project_id": b.project_id, "connection": b.connection_id, "mappings": [dict(x) for x in b.mappings],
                "required": b.required, "max_age_s": b.max_age_s, "updated_at": _iso(b.updated_at)}

    def get_binding(self, actor: Any, pid: str) -> dict[str, Any]:
        self.team._gate(actor, pid, "owner")
        b = self.store.binding(actor, pid)
        if b is None:
            raise SsoError(404, "not_found", "pas de SSO sur ce projet")
        return self._binding_json(b)

    def put_binding(self, actor: Any, pid: str, body: dict[str, Any]) -> dict[str, Any]:
        p, _ = self.team._gate(actor, pid, "owner")
        unknown = set(body) - {"connection", "mappings", "required", "max_age_s"}
        if unknown:
            raise SsoError(400, "bad_request", f"champ inconnu : {min(unknown)}")
        c = self._own(actor, str(body.get("connection") or ""))
        if c.tenant_id != p.tenant_id:
            raise not_found()
        required = body.get("required", False)
        if not isinstance(required, bool):
            raise SsoError(400, "bad_request", "required : true | false")
        b = Binding(project_id=p.id, tenant_id=p.tenant_id, connection_id=c.id,
                    mappings=m.clean_mappings(body.get("mappings") or []), required=required,
                    max_age_s=m.clean_max_age(body.get("max_age_s")), updated_by=actor.account_id,
                    updated_at=self.clock())
        self.store.put_binding(b)
        self._audit(c.tenant_id, c.id, "binding_set", actor=actor.account_id, project_id=p.id,
                    detail={"mappings": b.mappings, "required": b.required, "max_age_s": b.max_age_s})
        return self._binding_json(b)

    def delete_binding(self, actor: Any, pid: str) -> dict[str, Any]:
        p, _ = self.team._gate(actor, pid, "owner")
        b = self.store.binding(actor, pid)
        if b is None or not self.store.delete_binding(p.id):
            raise SsoError(404, "not_found", "pas de SSO sur ce projet")
        self._audit(p.tenant_id, b.connection_id, "binding_removed", actor=actor.account_id, project_id=p.id)
        return {"removed": True}

    # ─────────────────────────── connexion (publique) ───────────────────────────

    def _resolve(self, body: dict[str, Any]) -> tuple[str, str]:
        cid, email = str(body.get("connection") or ""), str(body.get("email") or "").strip().lower()
        if m.is_connection_id(cid):
            tenant = self.store.route(cid)
            if tenant is not None:
                return cid, tenant
        elif email and not cid:
            hit = self.store.claim_for(m.email_domain(email))
            if hit is not None:
                return hit
        raise SsoError(404, "unknown_connection", "connexion SSO inconnue")

    def start(self, body: dict[str, Any], ip: str | None) -> dict[str, Any]:
        from nokker.approvals.crypto import load_p256_public_key_spki

        cid, tenant = self._resolve(body)
        spki = str(body.get("public_key_spki") or "")
        try:
            load_p256_public_key_spki(spki)
        except Exception:  # noqa: BLE001 — clé illisible : refusée avant d'ouvrir quoi que ce soit
            raise SsoError(400, "invalid_key", "public_key_spki : clé P-256 SPKI") from None
        with tenant_scope(tenant):
            c = self.store.connection(cid)
            if c is None:
                raise SsoError(404, "unknown_connection", "connexion SSO inconnue")
            if not self.secrets.has(c.tenant_id, c.secret_name):
                raise SsoError(503, "not_configured", "connexion SSO incomplète (secret du client absent)")
        try:
            disc = self.idp.discovery(c.issuer)
        except IdpError:
            self._audit(tenant, cid, "idp_unavailable", detail={"step": "discovery"})
            raise SsoError(502, "idp_unavailable", "fournisseur d'identité injoignable") from None
        state, nonce = pysecrets.token_urlsafe(32), pysecrets.token_urlsafe(32)
        verifier = pysecrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        now = self.clock()
        lang = "en" if body.get("lang") == "en" else "fr"
        self.store.put_flow(Flow(state_sha256=_sha(state), connection_id=cid, tenant_id=tenant, nonce=nonce,
                                 code_verifier=verifier, public_key_spki=spki,
                                 device_name=str(body.get("name") or "")[:80], lang=lang, created_at=now,
                                 expires_at=now + m.FLOW_TTL_S))
        q = {"response_type": "code", "client_id": c.client_id, "redirect_uri": self.redirect_uri, "scope": SCOPES,
             "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
        if body.get("email"):
            q["login_hint"] = str(body["email"]).strip().lower()[:254]
        sep = "&" if "?" in disc["authorization_endpoint"] else "?"
        return {"authorize_url": disc["authorization_endpoint"] + sep + urlencode(q), "state": state,
                "connection": cid, "expires_at": _iso(now + m.FLOW_TTL_S)}

    def callback(self, body: dict[str, Any], ip: str | None) -> dict[str, Any]:
        from nokker.approvals.crypto import SignatureError, verify_p1363_signature

        state = str(body.get("state") or "")
        flow = self.store.take_flow(_sha(state)) if 0 < len(state) <= 200 else None  # consommé d'abord : usage unique
        now = self.clock()
        if flow is None or flow.expires_at < now:
            raise SsoError(401, "invalid_state", "connexion SSO inconnue, expirée ou déjà utilisée")
        tenant, cid = flow.tenant_id, flow.connection_id
        try:
            verify_p1363_signature(flow.public_key_spki, f"{state}.oidc".encode(), str(body.get("signature") or ""))
        except (SignatureError, ValueError):
            self._audit(tenant, cid, "login_refused", detail={"reason": "binding_invalid"})
            raise SsoError(401, "binding_invalid", "la connexion n'a pas été lancée par ce navigateur") from None
        if body.get("error"):
            self._audit(tenant, cid, "login_refused", detail={"reason": "idp_error",
                                                              "error": str(body["error"])[:64]})
            raise SsoError(401, "idp_error", "le fournisseur d'identité a refusé la connexion")
        with tenant_scope(tenant):
            c = self.store.connection(cid)
        if c is None:
            raise SsoError(401, "invalid_state", "connexion SSO inconnue, expirée ou déjà utilisée")
        secret = self.secrets.get(c.tenant_id, c.secret_name)
        if not secret:
            self._audit(tenant, cid, "login_refused", detail={"reason": "not_configured"})
            raise SsoError(503, "not_configured", "connexion SSO incomplète (secret du client absent)")
        code = str(body.get("code") or "")
        if not code or len(code) > 2048:
            raise SsoError(400, "bad_request", "code requis")
        try:
            disc = self.idp.discovery(c.issuer)
            tokens = self.idp.exchange(disc, code=code, verifier=flow.code_verifier, redirect_uri=self.redirect_uri,
                                       client_id=c.client_id, client_secret=secret, token_auth=c.token_auth)
            claims = verify_id_token(tokens["id_token"], keys=lambda kid: self.idp.keys(disc, kid), issuer=c.issuer,
                                     client_id=c.client_id, nonce=flow.nonce, now=now)
        except IdpError as e:
            self._audit(tenant, cid, "login_refused", detail={"reason": f"idp:{e.code}"})
            raise SsoError(401, "idp_error", "échange avec le fournisseur d'identité en échec") from None
        except TokenInvalid as e:
            self._audit(tenant, cid, "login_refused", detail={"reason": e.code})
            raise SsoError(401, "token_invalid", "jeton d'identité refusé") from None
        email = str(claims.get("email") or "").strip().lower()
        sub = claims["sub"]
        if not email or "@" not in email or claims.get("email_verified") not in (True, "true"):
            self._audit(tenant, cid, "login_refused", email=email, detail={"reason": "email_unverified", "sub": sub})
            raise SsoError(403, "email_unverified", "le fournisseur ne garantit pas cette adresse e-mail")
        if self.store.claim_for(m.email_domain(email)) != (c.id, c.tenant_id):
            self._audit(tenant, cid, "login_refused", email=email, detail={"reason": "domain_refused", "sub": sub})
            raise SsoError(403, "domain_refused", "adresse hors des domaines de cette connexion")
        account = self._link(c, sub, email, flow.lang)
        with tenant_scope(account.tenant_id):
            device = self.approvals.enroll_device(account.account_id, flow.device_name or "SSO", flow.public_key_spki)
            try:
                self.approvals.confirm_device_by_sso(device.id, c.id)
                self.tenancy.bind_identity(account.account_id, "device", device.id)
            except Exception:
                self.approvals.delete_device(device.id)
                raise
        projects = self._sync(c, account, _groups(claims, c.groups_claim))
        self._audit(tenant, cid, "login_ok", actor=account.account_id, email=email,
                    detail={"sub": sub, "projects": projects})
        return {"device_id": device.id, "user": account.account_id, "projects": projects}

    # ── compte ──

    def _refuse_minor(self, c: Connection, acc: Any, email: str) -> None:
        if self.is_minor is not None and self.is_minor(acc.account_id):
            self._audit(c.tenant_id, c.id, "login_refused", email=email, detail={"reason": "minor_account"})
            raise SsoError(403, "minor_account", "pas de connexion SSO pour un profil mineur")

    def _link(self, c: Connection, sub: str, email: str, lang: str) -> Any:
        from nokker.tenancy.store import IdentityTaken

        now = self.clock()
        ident = self.store.identity(c.issuer, sub)
        if ident is not None:
            acc = self.tenancy.account(ident.account_id)
            if acc is None or getattr(acc, "status", "active") != "active":
                self._audit(c.tenant_id, c.id, "login_refused", email=email, detail={"reason": "account_inactive"})
                raise SsoError(403, "account_inactive", "compte suspendu ou effacé")
            self._refuse_minor(c, acc, email)
            ident.connection_id, ident.email, ident.last_login_at = c.id, email, now
            self.store.put_identity(ident)
            return acc
        acc = self.tenancy.account_by_email(email)
        if acc is not None:
            self._refuse_minor(c, acc, email)
            if any(i.issuer == c.issuer and i.sub != sub for i in self.store.identities_of(acc.account_id)):
                self._audit(c.tenant_id, c.id, "login_refused", email=email, detail={"reason": "account_conflict",
                                                                                     "sub": sub})
                raise SsoError(409, "account_conflict", "ce compte est déjà lié à une autre identité de ce fournisseur")
            event = "account_linked"
        else:
            try:
                acc = self.tenancy.create_account(email, lang)
            except IdentityTaken:  # adresse prise par un compte inactif (ou une course) : on ne lie rien
                self._audit(c.tenant_id, c.id, "login_refused", email=email, detail={"reason": "account_inactive"})
                raise SsoError(403, "account_inactive", "compte suspendu ou effacé") from None
            event = "account_created"
        self.store.put_identity(Identity(issuer=c.issuer, sub=sub, account_id=acc.account_id, connection_id=c.id,
                                         email=email, created_at=now, last_login_at=now))
        self._audit(c.tenant_id, c.id, event, actor=acc.account_id, email=email, detail={"sub": sub})
        return acc

    # ── groupes → rôles ──

    def _sync(self, c: Connection, acc: Any, groups: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        sys_actor = _System(tenant_id=c.tenant_id)
        st = self.team.store
        with tenant_scope(c.tenant_id):
            for b in self.store.bindings_of(c.id):
                p: Project | None = st.project(sys_actor, b.project_id)
                if p is None or p.tenant_id != c.tenant_id:
                    continue
                role = b.role_for(groups)
                members = st.members(sys_actor, p.id)
                cur = next((x for x in members if x.account_id == acc.account_id), None)
                if cur is not None and cur.role == "owner":  # le propriétaire n'est jamais touché par le SSO
                    out.append({"id": p.id, "role": "owner"})
                    continue
                managed = cur is not None and self.store.is_managed(p.id, acc.account_id)
                now = self.clock()
                if role is None:
                    if cur is not None and managed:
                        st.remove_member(sys_actor, p, acc.account_id)
                        self.store.unset_managed(p.id, acc.account_id)
                        self._audit(c.tenant_id, c.id, "role_removed", actor=acc.account_id, email=acc.email,
                                    project_id=p.id, detail={"from": cur.role})
                        cur = None
                    out.append({"id": p.id, "role": cur.role if cur is not None else None})
                    continue
                if cur is None:
                    if len(members) >= MAX_MEMBERS:
                        self._audit(c.tenant_id, c.id, "member_limit", actor=acc.account_id, email=acc.email,
                                    project_id=p.id)
                        out.append({"id": p.id, "role": None})
                        continue
                    st.add_member(sys_actor, Member(project_id=p.id, tenant_id=p.tenant_id, account_id=acc.account_id,
                                                    role=role, email=acc.email, added_by=f"sso:{c.id}", added_at=now))
                    self._audit(c.tenant_id, c.id, "member_added", actor=acc.account_id, email=acc.email,
                                project_id=p.id, detail={"role": role})
                elif cur.role != role:
                    st.set_role(sys_actor, p, acc.account_id, role)
                    self._audit(c.tenant_id, c.id, "role_changed", actor=acc.account_id, email=acc.email,
                                project_id=p.id, detail={"from": cur.role, "to": role})
                if not managed:
                    self.store.set_managed(p.id, p.tenant_id, acc.account_id, c.id, now)
                out.append({"id": p.id, "role": role})
        return out

    # ─────────────────────────── SSO obligatoire ───────────────────────────

    def gate_reason(self, actor: Any, project: Project, member: Member) -> dict[str, str] | None:
        """Branché sur `Team.sso_gate` : None si l'accès est permis, sinon {"code", "connection"}."""
        if member.role == "owner":
            return None  # le propriétaire garde la main (réparer une configuration cassée)
        b = self.store.binding(actor, project.id)
        if b is None or not b.required:
            return None
        now = self.clock()
        via = getattr(actor, "auth_via", None)
        if via is not None:  # requête web : la session elle-même doit venir de CETTE connexion SSO, et être récente
            if via == f"sso:{b.connection_id}" and now - float(getattr(actor, "auth_at", 0.0)) <= b.max_age_s:
                return None
            return {"code": "sso_required", "connection": b.connection_id}
        last = self.store.last_login(b.connection_id, actor.account_id)  # machine liée, tour d'agent
        if last is not None and now - last <= b.max_age_s:
            return None
        return {"code": "sso_required", "connection": b.connection_id}


__all__ = ["SCOPES", "Sso", "TeamError"]
