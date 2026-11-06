"""Couche OAuth des connecteurs (docs/CONNECTORS-OAUTH.md).

- Fournisseurs décrits par la config (`oauth_providers`) sur des défauts connus (microsoft, google) : URL, scopes par
  service, `client_id`, `client_secret` = référence `secret:NOM` lue au coffre (jamais en clair). Sans client_id ou
  sans secret lisible : fournisseur éteint.
- Flux : `start` (PKCE S256, `state` aléatoire signé HMAC, flux rangé 10 min, usage unique) → `callback` (public :
  c'est le `state` qui lie au profil, au locataire et au foyer) → compte OAuth enregistré, jetons CHIFFRÉS, puis
  `on_linked` crée un connecteur 068 par service accordé. Aucun jeton côté navigateur, sauf le grant Drive du
  sélecteur Google (`picker_token`, drive.file seul, grant séparé).
- Jetons par AUDIENCE : Microsoft donne un jeton par ressource (Outlook IMAP/SMTP, Graph) ; le jeton de
  rafraîchissement sert aux deux. Rafraîchi à l'usage (moins de 60 s restantes), sous verrou ; `invalid_grant` ou
  refus persistant → compte en `reconnect` (`account_reconnect`), jamais de boucle.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from nokker.connectors.crypto import SecretBox
from nokker.connectors.errors import ConnectorError

logger = logging.getLogger("nokker.connectors.oauth")

FLOW_TTL_S = 600
# portées d'identité (et leurs alias rendus par Google) : ne donnent accès à aucune donnée
IDENTITY_ALIASES = {"openid", "email", "profile", "offline_access", "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/userinfo.profile"}
REFRESH_MARGIN_S = 60
SERVICES = ("mail", "calendar", "contacts", "files", "tasks", "pages")
REDIRECT_URI = "https://app.nokker.ai/v1/connectors/oauth/callback"
WEB_RETURN = "https://app.nokker.ai/account/connectors"

# Défauts connus ; la config remplace ou complète (client_id, client_secret, URL, scopes).
DEFAULTS: dict[str, dict[str, Any]] = {
    "microsoft": {
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "identity_scopes": ["openid", "email", "profile", "offline_access"],
        # audience → services : un jeton d'accès Microsoft ne vaut que pour UNE ressource
        "audiences": {"outlook": ["mail"], "graph": ["calendar", "contacts", "files", "tasks"]},
        "services": {
            "mail": ["https://outlook.office.com/IMAP.AccessAsUser.All", "https://outlook.office.com/SMTP.Send"],
            "calendar": ["https://graph.microsoft.com/Calendars.ReadWrite"],
            "contacts": ["https://graph.microsoft.com/Contacts.ReadWrite"],
            "files": ["https://graph.microsoft.com/Files.ReadWrite"],
            "tasks": ["https://graph.microsoft.com/Tasks.ReadWrite"],
        },
    },
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "revoke_url": "https://oauth2.googleapis.com/revoke",
        "identity_scopes": ["openid", "email"],
        "extra": {"access_type": "offline", "prompt": "consent"},
        "services": {
            "calendar": ["https://www.googleapis.com/auth/calendar"],
            "contacts": ["https://www.googleapis.com/auth/contacts"],
            "files": ["https://www.googleapis.com/auth/drive.file"],
            "tasks": ["https://www.googleapis.com/auth/tasks"],
        },
        # Google Drive : grant SÉPARÉ (le jeton du sélecteur va au navigateur : drive.file seul)
        "separate": {"files": "drive"},
    },
}


@dataclass(frozen=True)
class Provider:
    name: str
    authorize_url: str
    token_url: str
    client_id: str
    client_secret: str  # valeur lue au coffre ; jamais dans un journal
    services: dict[str, list[str]]
    identity_scopes: list[str] = field(default_factory=list)
    audiences: dict[str, list[str]] = field(default_factory=dict)
    extra: dict[str, str] = field(default_factory=dict)
    revoke_url: str = ""
    separate: dict[str, str] = field(default_factory=dict)  # service → grant séparé
    picker: dict[str, str] | None = None  # google : {developer_key, app_id}

    def audience(self, service: str) -> str:
        return next((a for a, svcs in self.audiences.items() if service in svcs), "default")

    def grant_for(self, services: list[str]) -> str:
        grants = {self.separate.get(s, "main") for s in services}
        if len(grants) != 1:
            raise ConnectorError("bad_service", "Google Drive se relie à part (un compte Google pour Drive seul)")
        return grants.pop()

    def scopes_for(self, services: list[str], audience: str | None = None) -> list[str]:
        out = list(self.identity_scopes)
        for s in services:
            if audience is None or self.audience(s) == audience:
                out += self.services.get(s, [])
        return list(dict.fromkeys(out))


def providers_from_config(raw: dict[str, Any] | None, resolve: Callable[[str], str] | None) -> dict[str, Provider]:
    """Fournisseurs ALLUMÉS : client_id présent et client_secret `secret:NOM` lisible au coffre."""
    out: dict[str, Provider] = {}
    for name, cfg in (raw or {}).items():
        base = {**DEFAULTS.get(name, {}), **(cfg or {})}
        cid, ref = str(base.get("client_id") or ""), str(base.get("client_secret") or "")
        if not cid or not base.get("authorize_url") or not base.get("token_url"):
            continue
        if not ref.startswith("secret:"):
            logger.error("oauth %s : client_secret doit être une référence secret:NOM — fournisseur éteint", name)
            continue
        try:
            secret = resolve(ref) if resolve else ""
        except Exception:  # noqa: BLE001 — coffre scellé, nom absent : éteint, journalisé sans valeur
            secret = ""
        if not secret:
            logger.warning("oauth %s : secret %s illisible — fournisseur éteint", name, ref)
            continue
        out[name] = Provider(name=name, authorize_url=base["authorize_url"], token_url=base["token_url"],
                             client_id=cid, client_secret=secret, services=dict(base.get("services") or {}),
                             identity_scopes=list(base.get("identity_scopes") or []),
                             audiences=dict(base.get("audiences") or {}), extra=dict(base.get("extra") or {}),
                             revoke_url=str(base.get("revoke_url") or ""), separate=dict(base.get("separate") or {}),
                             picker=dict(base["picker"]) if base.get("picker") else None)
    return out


# ── transport ─────────────────────────────────────────────────────────────────────────────────────────────────────

class Http(Protocol):
    def post_form(self, url: str, data: dict[str, str]) -> tuple[int, dict[str, Any]]: ...


class HttpxHttp:
    """Points d'accès fixés par la config du fournisseur (jamais par l'utilisateur)."""

    def __init__(self, timeout_s: float = 20.0, transport: Any = None) -> None:
        import httpx

        self._client = httpx.Client(timeout=timeout_s, transport=transport, follow_redirects=False)

    def post_form(self, url: str, data: dict[str, str]) -> tuple[int, dict[str, Any]]:
        r = self._client.post(url, data=data, headers={"Accept": "application/json"})
        try:
            body = r.json()
        except ValueError:
            body = {}
        return r.status_code, body if isinstance(body, dict) else {}


# ── modèle et magasin ─────────────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OAuthAccount:
    id: str
    account_id: str
    provider: str
    subject: str
    email: str = ""
    scopes: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    grant_name: str = "main"
    state: str = "ok"
    tenant_id: str = ""

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "provider": self.provider, "email": self.email, "state": self.state,
                "services": list(self.services)}


@dataclass(frozen=True)
class Flow:
    state_sha256: str
    tenant_id: str
    account_id: str
    provider: str
    services: tuple[str, ...]
    code_verifier: str
    created_at: float
    expires_at: float
    grant_name: str = "main"
    household_id: str = ""
    label: str = ""
    oauth_account: str = ""


class MemoryOAuthStore:
    """Comme la RLS : comptes lus dans le locataire courant (`tenant()`), flux lus par leur clé."""

    def __init__(self, box: SecretBox, tenant: Callable[[], str]) -> None:
        self._box, self._tenant = box, tenant
        self._accounts: dict[str, tuple[OAuthAccount, bytes]] = {}
        self._flows: dict[str, Flow] = {}
        self._lock = threading.RLock()

    def put_flow(self, f: Flow) -> None:
        with self._lock:
            self._flows[f.state_sha256] = f

    def take_flow(self, state_sha256: str) -> Flow | None:
        with self._lock:
            return self._flows.pop(state_sha256, None)

    def find(self, account_id: str, provider: str, grant: str, subject: str) -> OAuthAccount | None:
        t = self._tenant()
        return next((a for a, _ in self._accounts.values() if a.tenant_id == t and (a.account_id, a.provider,
                     a.grant_name, a.subject) == (account_id, provider, grant, subject)), None)

    def save(self, a: OAuthAccount, tokens: dict[str, Any]) -> OAuthAccount:
        a = replace(a, tenant_id=self._tenant())
        with self._lock:
            self._accounts[a.id] = (a, self._box.seal(json.dumps(tokens)))
        return a

    def get(self, oa_id: str) -> tuple[OAuthAccount, dict[str, Any]] | None:
        hit = self._accounts.get(oa_id)
        if hit is None or hit[0].tenant_id != self._tenant():
            return None
        return hit[0], json.loads(self._box.open(hit[1]))

    def of(self, account_id: str) -> list[OAuthAccount]:
        t = self._tenant()
        return [a for a, _ in self._accounts.values() if a.tenant_id == t and a.account_id == account_id]

    def set_state(self, oa_id: str, state: str) -> None:
        with self._lock:
            hit = self.get(oa_id)
            if hit:
                self._accounts[oa_id] = (replace(hit[0], state=state), self._accounts[oa_id][1])

    def delete(self, oa_id: str) -> bool:
        with self._lock:
            return self.get(oa_id) is not None and self._accounts.pop(oa_id, None) is not None

    def locked(self, oa_id: str):
        return self._lock


class PgOAuthStore:
    COLS = "id, account_id, provider, subject, email, scopes, services, grant_name, state, tenant_id"
    F_COLS = ("state_sha256, tenant_id, account_id, provider, services, code_verifier, created_at, expires_at, "
              "grant_name, household_id, label, oauth_account")

    def __init__(self, dsn: str, box: SecretBox) -> None:
        self.dsn, self._box = dsn, box

    def _conn(self):
        from psycopg.rows import dict_row

        from nokker.store.conn import connect

        return connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _acc(r: dict[str, Any]) -> OAuthAccount:
        return OAuthAccount(**{**r, "scopes": tuple(r["scopes"] or ()), "services": tuple(r["services"] or ())})

    def put_flow(self, f: Flow) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM oauth_flows WHERE expires_at < %s", (f.created_at - 3600,))
            c.execute(f"INSERT INTO oauth_flows ({self.F_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      (f.state_sha256, f.tenant_id, f.account_id, f.provider, list(f.services), f.code_verifier,
                       f.created_at, f.expires_at, f.grant_name, f.household_id, f.label, f.oauth_account))

    def take_flow(self, state_sha256: str) -> Flow | None:
        from psycopg.rows import dict_row

        from nokker.store.conn import connect_key

        with connect_key(self.dsn, state_sha256, row_factory=dict_row) as c:  # DELETE … RETURNING : un seul gagne
            r = c.execute(f"DELETE FROM oauth_flows WHERE state_sha256 = %s RETURNING {self.F_COLS}",
                          (state_sha256,)).fetchone()
        return Flow(**{**r, "services": tuple(r["services"])}) if r else None

    def find(self, account_id: str, provider: str, grant: str, subject: str) -> OAuthAccount | None:
        with self._conn() as c:
            r = c.execute(f"SELECT {self.COLS} FROM oauth_accounts WHERE account_id = %s AND provider = %s AND "
                          "grant_name = %s AND subject = %s", (account_id, provider, grant, subject)).fetchone()
        return self._acc(r) if r else None

    def save(self, a: OAuthAccount, tokens: dict[str, Any]) -> OAuthAccount:
        with self._conn() as c:
            r = c.execute(
                f"INSERT INTO oauth_accounts (id, account_id, provider, grant_name, subject, email, scopes, services, "
                f"token_enc, state, refreshed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) "
                f"ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email, scopes = EXCLUDED.scopes, "
                f"services = EXCLUDED.services, token_enc = EXCLUDED.token_enc, state = EXCLUDED.state, "
                f"refreshed_at = now() RETURNING {self.COLS}",
                (a.id, a.account_id, a.provider, a.grant_name, a.subject, a.email, list(a.scopes), list(a.services),
                 self._box.seal(json.dumps(tokens)), a.state)).fetchone()
        return self._acc(r)

    def get(self, oa_id: str) -> tuple[OAuthAccount, dict[str, Any]] | None:
        with self._conn() as c:
            r = c.execute(f"SELECT {self.COLS}, token_enc FROM oauth_accounts WHERE id = %s", (oa_id,)).fetchone()
        if r is None:
            return None
        enc = r.pop("token_enc")
        return self._acc(r), json.loads(self._box.open(enc))

    def of(self, account_id: str) -> list[OAuthAccount]:
        with self._conn() as c:
            rows = c.execute(f"SELECT {self.COLS} FROM oauth_accounts WHERE account_id = %s ORDER BY created_at",
                             (account_id,)).fetchall()
        return [self._acc(r) for r in rows]

    def set_state(self, oa_id: str, state: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE oauth_accounts SET state = %s WHERE id = %s", (state, oa_id))

    def delete(self, oa_id: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM oauth_accounts WHERE id = %s", (oa_id,)).rowcount > 0

    def locked(self, oa_id: str):
        """Verrou consultatif Postgres par compte : un seul rafraîchissement part (plusieurs pods)."""
        store = self

        class _Lock:
            def __enter__(self):
                self.c = store._conn()
                self.c.execute("SELECT pg_advisory_lock(hashtext(%s))", (f"oauth:{oa_id}",))
                return self

            def __exit__(self, *exc):
                try:
                    self.c.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"oauth:{oa_id}",))
                finally:
                    self.c.close()

        return _Lock()


# ── service ───────────────────────────────────────────────────────────────────────────────────────────────────────

def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _id_claims(id_token: str) -> dict[str, Any]:
    """Revendications d'un id_token reçu DIRECTEMENT du point d'accès des jetons (TLS, canal arrière : OIDC Core
    §3.1.3.7 permet de ne pas en vérifier la signature). Jamais un id_token venu du navigateur."""
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def new_oauth_id() -> str:
    return "oa_" + "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(10))


class OAuth:
    """`on_linked(account, oauth_account, services, label) -> list[str]` (ids des connecteurs créés) : branché sur le
    service des connecteurs. `tenant()` : locataire courant ; `scope(tenant)` : contexte du locataire (callback)."""

    def __init__(self, providers: dict[str, Provider], store: Any, state_key: bytes, *, http: Http | None = None,
                 clock: Callable[[], float] = time.time, on_linked: Callable[..., list[str]] | None = None,
                 redirect_uri: str = REDIRECT_URI, web_return: str = WEB_RETURN,
                 available: tuple[str, ...] = ("mail", "calendar")) -> None:
        if len(state_key) < 16:
            raise ValueError("clé de signature du state OAuth trop courte")
        self.providers, self.store, self._key = providers, store, state_key
        self.http = http or HttpxHttp()
        self.clock, self.on_linked = clock, on_linked
        self.redirect_uri, self.web_return = redirect_uri, web_return
        self.available = available  # natures dont le connecteur est livré (les autres arrivent par lots)

    # state signé : <aléa>.<hmac>
    def _sign(self, raw: str) -> str:
        return raw + "." + hmac.new(self._key, raw.encode(), hashlib.sha256).hexdigest()[:32]

    def _verify(self, state: str) -> str | None:
        raw, _, sig = (state or "").rpartition(".")
        if not raw or not hmac.compare_digest(self._sign(raw), state):
            return None
        return raw

    def provider(self, name: str) -> Provider:
        p = self.providers.get(name)
        if p is None:
            raise ConnectorError("provider_off", f"fournisseur non disponible : {name}")
        return p

    def presets(self) -> list[dict[str, Any]]:
        out = []
        for name in ("microsoft", "google"):
            p = self.providers.get(name)
            svcs = [s for s in (p.services if p else DEFAULTS[name]["services"]) if s in self.available]
            sep = (p.separate if p else DEFAULTS[name].get("separate", {}))
            main = [s for s in svcs if s not in sep]
            if main:
                out.append({"provider": name, "services": main, "enabled": p is not None})
            for s in sep:
                if s in svcs:
                    out.append({"provider": name, "services": [s], "enabled": p is not None, "separate": True,
                                **({"picker": p.picker} if p is not None and p.picker else {})})
        return out

    def start(self, *, tenant_id: str, account_id: str, provider: str, services: list[str], label: str = "",
              household_id: str = "", oauth_account: str = "", login_hint: str = "") -> dict[str, Any]:
        p = self.provider(provider)
        services = list(dict.fromkeys(str(s) for s in services or []))
        offered = [s for s in p.services if s in self.available]
        if not services or any(s not in offered for s in services):
            raise ConnectorError("bad_service", f"services possibles chez {provider} : {', '.join(offered)}")
        grant = p.grant_for(services)
        if oauth_account:
            hit = self.store.get(oauth_account)
            if hit is None or hit[0].account_id != account_id or hit[0].provider != provider \
                    or hit[0].grant_name != grant:
                raise ConnectorError("not_found", "compte relié inconnu")
            services = list(dict.fromkeys([*hit[0].services, *services]))
            login_hint = login_hint or hit[0].email
        raw = secrets.token_urlsafe(32)
        state = self._sign(raw)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        now = self.clock()
        self.store.put_flow(Flow(state_sha256=_sha(raw), tenant_id=tenant_id, account_id=account_id, provider=provider,
                                 services=tuple(services), code_verifier=verifier, created_at=now,
                                 expires_at=now + FLOW_TTL_S, grant_name=grant, household_id=household_id,
                                 label=label[:40], oauth_account=oauth_account))
        # consentement pour tous les services d'emblée (Microsoft : toutes les ressources ; le premier jeton vaut pour
        # la ressource du premier service, les autres viennent du jeton de rafraîchissement)
        q = {"client_id": p.client_id, "response_type": "code", "redirect_uri": self.redirect_uri,
             "scope": " ".join(p.scopes_for(services)), "state": state, "code_challenge": challenge,
             "code_challenge_method": "S256", **p.extra}
        if login_hint:
            q["login_hint"] = login_hint
        if provider == "google" and grant == "main" and oauth_account:
            q["include_granted_scopes"] = "true"  # jamais pour le grant Drive (drive.file seul)
        return {"url": f"{p.authorize_url}?{urllib.parse.urlencode(q)}",
                "expires_at": datetime.fromtimestamp(now + FLOW_TTL_S, tz=UTC).isoformat()}

    def _token(self, p: Provider, data: dict[str, str]) -> dict[str, Any]:
        body = {**data, "client_id": p.client_id, "client_secret": p.client_secret}
        try:
            status, d = self.http.post_form(p.token_url, body)
        except Exception:  # noqa: BLE001 — réseau : sans détail (le secret est dans le corps)
            raise ConnectorError("provider_error", "fournisseur injoignable") from None
        if status != 200 or not d.get("access_token"):
            err = str(d.get("error") or f"HTTP {status}")
            raise ConnectorError("invalid_grant" if err == "invalid_grant" else "provider_error", err[:60])
        return d

    def _redirect(self, **q: str) -> str:
        return f"{self.web_return}?{urllib.parse.urlencode({k: v for k, v in q.items() if v})}"

    def callback(self, params: dict[str, str]) -> str:
        """URL de retour du web (302). Ne lève pas : toute erreur devient `?oauth=error&code=…`."""
        from nokker.tenancy.context import tenant_scope

        raw = self._verify(str(params.get("state") or ""))
        flow = self.store.take_flow(_sha(raw)) if raw else None
        if flow is None:
            return self._redirect(oauth="error", code="expired")
        if flow.expires_at < self.clock():
            return self._redirect(oauth="error", code="expired", provider=flow.provider)
        if params.get("error"):
            return self._redirect(oauth="error", code="denied" if params["error"] == "access_denied" else "provider_error",
                                  provider=flow.provider)
        try:
            p = self.provider(flow.provider)
            first_aud = p.audience(flow.services[0]) if p.audiences else None
            data = {"grant_type": "authorization_code", "code": str(params.get("code") or ""),
                    "redirect_uri": self.redirect_uri, "code_verifier": flow.code_verifier}
            if first_aud is not None:
                # Microsoft : un jeton d'accès ne vaut que pour UNE ressource. L'échange du code demande EXPLICITEMENT
                # celle du premier service (jamais « la première de la liste » au choix du fournisseur) ; les autres
                # ressources viennent chacune d'un rafraîchissement à leurs scopes (`access_token`).
                data["scope"] = " ".join(p.scopes_for([s for s in flow.services if p.audience(s) == first_aud],
                                                      first_aud))
            d = self._token(p, data)
            claims = _id_claims(str(d.get("id_token") or ""))
            subject = str(claims.get("sub") or claims.get("oid") or "")
            email = str(claims.get("email") or claims.get("preferred_username") or "").lower()
            if not subject:
                raise ConnectorError("provider_error", "identité absente")
            granted = set(str(d.get("scope") or " ".join(p.scopes_for(list(flow.services)))).split())
            ok = [s for s in flow.services if p.audiences or all(sc in granted for sc in p.services.get(s, []))]
            missing = [s for s in flow.services if s not in ok]
            if not ok:
                return self._redirect(oauth="error", code="scope_missing", provider=flow.provider,
                                      missing=",".join(missing))
            now = self.clock()
            tokens = {"refresh_token": d.get("refresh_token") or "",
                      "access": {first_aud or "default": {"token": d["access_token"],
                                                          "exp": now + float(d.get("expires_in") or 3600)}}}
            with tenant_scope(flow.tenant_id):
                prior = self.store.get(flow.oauth_account) if flow.oauth_account else None
                found = prior[0] if prior else self.store.find(flow.account_id, flow.provider, flow.grant_name, subject)
                if found is not None and found.subject != subject:
                    return self._redirect(oauth="error", code="already_linked_elsewhere", provider=flow.provider)
                if found is not None and not tokens["refresh_token"]:
                    old = self.store.get(found.id)
                    tokens["refresh_token"] = old[1].get("refresh_token", "") if old else ""
                acc = OAuthAccount(id=found.id if found else new_oauth_id(), account_id=flow.account_id,
                                   provider=flow.provider, subject=subject, email=email or (found.email if found else ""),
                                   scopes=tuple(sorted(granted)), grant_name=flow.grant_name, state="ok",
                                   services=tuple(dict.fromkeys([*(found.services if found else ()), *ok])))
                acc = self.store.save(acc, tokens)
                if self.on_linked is not None:
                    self.on_linked(flow.account_id, acc, ok, flow.label)
        except ConnectorError as e:
            logger.warning("oauth %s : callback refusé (%s)", flow.provider, e.code)
            return self._redirect(oauth="error", code=e.code if e.code in ("too_many",) else "provider_error",
                                  provider=flow.provider)
        return self._redirect(oauth="done", provider=flow.provider, account=acc.id, missing=",".join(missing))

    # ── jetons ──
    def access_token(self, oa_id: str, service: str) -> str:
        """Jeton d'accès valable pour `service`, dans le locataire courant ; rafraîchi s'il le faut, sous verrou."""
        hit = self.store.get(oa_id)
        if hit is None:
            raise ConnectorError("not_configured", "compte relié retiré")
        acc, tokens = hit
        if acc.state != "ok":
            raise ConnectorError("account_reconnect", "compte à reconnecter par son propriétaire")
        p = self.provider(acc.provider)
        aud = p.audience(service) if p.audiences else "default"
        cur = (tokens.get("access") or {}).get(aud) or {}
        if cur.get("token") and float(cur.get("exp") or 0) - REFRESH_MARGIN_S > self.clock():
            return str(cur["token"])
        with self.store.locked(oa_id):
            acc, tokens = self.store.get(oa_id) or (acc, tokens)  # un autre a peut-être rafraîchi entre-temps
            cur = (tokens.get("access") or {}).get(aud) or {}
            if cur.get("token") and float(cur.get("exp") or 0) - REFRESH_MARGIN_S > self.clock():
                return str(cur["token"])
            if not tokens.get("refresh_token"):
                self.store.set_state(oa_id, "reconnect")
                raise ConnectorError("account_reconnect", "compte à reconnecter par son propriétaire")
            svcs = [s for s in acc.services if (p.audience(s) if p.audiences else "default") == aud] or [service]
            data = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
            if p.audiences:
                data["scope"] = " ".join(p.scopes_for(svcs, aud))
            try:
                d = self._token(p, data)
            except ConnectorError as e:
                if e.code == "invalid_grant":
                    self.store.set_state(oa_id, "reconnect")
                    logger.warning("oauth %s : jeton révoqué, compte %s à reconnecter", acc.provider, oa_id)
                    raise ConnectorError("account_reconnect", "compte à reconnecter par son propriétaire") from None
                raise
            access = dict(tokens.get("access") or {})
            access[aud] = {"token": d["access_token"], "exp": self.clock() + float(d.get("expires_in") or 3600)}
            tokens = {**tokens, "access": access, "refresh_token": d.get("refresh_token") or tokens["refresh_token"]}
            self.store.save(acc, tokens)
            return str(d["access_token"])

    def picker_token(self, oa_id: str) -> dict[str, Any]:
        """Jeton du sélecteur Google : SEULEMENT le grant Drive (drive.file). Jamais stocké côté web ni journalisé."""
        hit = self.store.get(oa_id)
        if hit is None:
            raise ConnectorError("not_found", "compte relié inconnu")
        acc, _ = hit
        p = self.provider(acc.provider)
        drive_only = set(acc.scopes) - IDENTITY_ALIASES <= {"https://www.googleapis.com/auth/drive.file"}
        if acc.provider != "google" or acc.grant_name != "drive" or not drive_only or not p.picker:
            raise ConnectorError("unsupported", "sélecteur réservé au compte Google Drive (drive.file seul)")
        token = self.access_token(oa_id, "files")
        cur = (self.store.get(oa_id)[1].get("access") or {}).get("default") or {}
        left = max(0, int(float(cur.get("exp") or 0) - self.clock()))
        return {"access_token": token, "expires_in": min(600, left), "developer_key": p.picker.get("developer_key", ""),
                "app_id": p.picker.get("app_id", "")}

    def revoke(self, oa_id: str) -> bool:
        """Révoque chez le fournisseur (au mieux) puis efface le compte et ses jetons."""
        hit = self.store.get(oa_id)
        if hit is None:
            return False
        acc, tokens = hit
        p = self.providers.get(acc.provider)
        tok = tokens.get("refresh_token")
        if p is not None and p.revoke_url and tok:
            try:
                self.http.post_form(p.revoke_url, {"token": tok})
            except Exception:  # noqa: BLE001 — au mieux : le compte est effacé quand même
                logger.warning("oauth %s : révocation chez le fournisseur en échec", acc.provider)
        return self.store.delete(oa_id)


def tokens_expire_soon(exp: float, now: float) -> bool:
    return exp - REFRESH_MARGIN_S <= now


__all__ = ["DEFAULTS", "FLOW_TTL_S", "Flow", "HttpxHttp", "MemoryOAuthStore", "OAuth", "OAuthAccount", "PgOAuthStore",
           "Provider", "REDIRECT_URI", "SERVICES", "WEB_RETURN", "new_oauth_id", "providers_from_config", "timedelta"]
