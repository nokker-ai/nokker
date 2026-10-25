"""Modèle du SSO OIDC des équipes (drapeau `sso`, docs/LOKKAL.md §7.24) : connexions, domaines, liens projet ↔
connexion, connexions en cours, identités. Validation seulement : ni stockage, ni droits, ni réseau ici.

- Une **connexion** (`sc_` + 16 hex) appartient au compte qui l'a créée, dans son locataire. Elle porte l'issuer, le
  client_id, le NOM du secret du client (la valeur est au coffre du noyau, jamais en base ni dans une réponse), le
  claim des groupes.
- Un **domaine** n'ouvre la connexion qu'une fois PROUVÉ (enregistrement TXT), et n'appartient qu'à une connexion :
  sans cette preuve, n'importe quel fournisseur pourrait affirmer `alice@domaine-des-autres` et prendre son compte.
- Un **lien** rattache une connexion à un projet du même propriétaire : table groupes → rôles (jamais `owner`), et
  `required` (les membres n'accèdent aux ressources du projet que par une connexion SSO de moins de `max_age_s`).
"""
from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from nokker.team.model import INVITABLE, RANK, TeamError

MAPPABLE = INVITABLE  # maintainer | member | viewer — `owner` ne s'obtient jamais par SSO
TOKEN_AUTH = ("client_secret_basic", "client_secret_post")
DEFAULT_MAX_AGE_S = 12 * 3600
MIN_MAX_AGE_S, MAX_MAX_AGE_S = 300, 7 * 86400
FLOW_TTL_S = 600.0
MAX_CONNECTIONS = 5
MAX_DOMAINS = 20
MAX_MAPPINGS = 100
TXT_PREFIX = "_nokker-sso."
TXT_VALUE_PREFIX = "nokker-sso="

_RE_CONNECTION = re.compile(r"^sc_[0-9a-f]{16}$")
_RE_CLIENT_ID = re.compile(r"^[\x21-\x7e]{1,255}$")
_RE_SECRET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_RE_CLAIM = re.compile(r"^[A-Za-z0-9_:./-]{1,100}$")
_RE_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def SsoError(status: int, code: str, message: str = "") -> TeamError:
    return TeamError(status, code, message)


def new_connection_id() -> str:
    return "sc_" + secrets.token_hex(8)


def is_connection_id(s: str) -> bool:
    return bool(_RE_CONNECTION.match(s or ""))


@dataclass
class Connection:
    id: str
    tenant_id: str
    owner_account: str
    name: str
    issuer: str
    client_id: str
    secret_name: str
    groups_claim: str = "groups"
    token_auth: str = "client_secret_basic"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class Domain:
    connection_id: str
    tenant_id: str
    domain: str
    token: str
    verified_at: float | None = None
    created_at: float = field(default_factory=time.time)

    @property
    def txt_name(self) -> str:
        return TXT_PREFIX + self.domain

    @property
    def txt_value(self) -> str:
        return TXT_VALUE_PREFIX + self.token


@dataclass
class Binding:
    project_id: str
    tenant_id: str
    connection_id: str
    mappings: list[dict[str, str]] = field(default_factory=list)
    required: bool = False
    max_age_s: int = DEFAULT_MAX_AGE_S  # « SSO obligatoire » : une connexion SSO plus vieille ne suffit plus
    updated_by: str = ""
    updated_at: float = field(default_factory=time.time)

    def role_for(self, groups: list[str]) -> str | None:
        """Le rôle le plus haut que donnent les groupes, ou None. Jamais `owner` (refusé à la validation et en base)."""
        have = set(groups)
        roles = [m["role"] for m in self.mappings if m["group"] in have and m["role"] in MAPPABLE]
        return max(roles, key=lambda r: RANK[r]) if roles else None


@dataclass
class Flow:
    state_sha256: str
    connection_id: str
    tenant_id: str
    nonce: str
    code_verifier: str
    public_key_spki: str
    device_name: str = ""
    lang: str = "fr"
    created_at: float = 0.0
    expires_at: float = 0.0


@dataclass
class Identity:
    issuer: str
    sub: str
    account_id: str
    connection_id: str
    email: str = ""
    created_at: float = 0.0
    last_login_at: float = 0.0


@dataclass(frozen=True)
class SessionActor:
    """Le compte d'une requête web, avec la façon dont sa session a été ouverte : `auth_via` = `approved_via` de
    l'appareil de la session (« sso:sc_… », « email-code », « invitation:… »), `auth_at` = quand cet appareil a été
    confirmé (pour un appareil SSO : l'heure de la connexion SSO). Mêmes champs que `tenancy.Account` pour le reste."""

    tenant_id: str
    account_id: str
    email: str
    auth_via: str
    auth_at: float
    lang: str = "fr"
    status: str = "active"
    created_at: Any = None

    @classmethod
    def of(cls, account: Any, auth_via: str | None, auth_at: float | None) -> SessionActor:
        return cls(tenant_id=account.tenant_id, account_id=account.account_id, email=account.email,
                   auth_via=auth_via or "", auth_at=float(auth_at or 0.0), lang=getattr(account, "lang", "fr"),
                   status=getattr(account, "status", "active"), created_at=getattr(account, "created_at", None))


# ─────────────────────────────── validation ───────────────────────────────


def _str(v: Any, n: int, what: str, *, required: bool = False) -> str:
    s = "" if v is None else str(v).strip()
    if required and not s:
        raise SsoError(400, "bad_request", f"{what} requis")
    if len(s) > n:
        raise SsoError(400, "bad_request", f"{what} : {n} caractères au plus")
    return s


def clean_name(v: Any) -> str:
    return _str(v, 80, "name", required=True)


def clean_issuer(v: Any) -> str:
    """URL https de l'issuer, comparée TELLE QUELLE au claim `iss` (OIDC Core §3.1.3.7) : pas de requête, pas de
    fragment, pas d'identifiants."""
    s = _str(v, 300, "issuer", required=True)
    u = urlsplit(s)
    if u.scheme != "https" or not u.hostname or u.query or u.fragment or u.username or u.password \
            or any(c.isspace() for c in s):
        raise SsoError(400, "bad_issuer", "issuer : une URL https, sans requête ni identifiants")
    return s


def clean_client_id(v: Any) -> str:
    s = _str(v, 255, "client_id", required=True)
    if not _RE_CLIENT_ID.match(s):
        raise SsoError(400, "bad_request", "client_id : caractères visibles, sans espace")
    return s


def clean_secret_name(v: Any) -> str:
    s = _str(v, 64, "client_secret_name", required=True)
    if not _RE_SECRET_NAME.match(s):
        raise SsoError(400, "bad_name", "client_secret_name : lettres, chiffres, « _ », « . », « - » (64 au plus)")
    return s


def clean_groups_claim(v: Any) -> str:
    s = _str(v, 100, "groups_claim") or "groups"
    if not _RE_CLAIM.match(s):
        raise SsoError(400, "bad_request", "groups_claim : nom de claim illisible")
    return s


def clean_token_auth(v: Any) -> str:
    s = _str(v, 40, "token_auth") or "client_secret_basic"
    if s not in TOKEN_AUTH:
        raise SsoError(400, "bad_request", f"token_auth : {' | '.join(TOKEN_AUTH)}")
    return s


def clean_max_age(v: Any) -> int:
    if v is None:
        return DEFAULT_MAX_AGE_S
    if isinstance(v, bool) or not isinstance(v, int) or not MIN_MAX_AGE_S <= v <= MAX_MAX_AGE_S:
        raise SsoError(400, "bad_request", f"max_age_s : entier entre {MIN_MAX_AGE_S} et {MAX_MAX_AGE_S}")
    return v


def clean_domain(v: Any) -> str:
    s = _str(v, 253, "domain", required=True).lower().rstrip(".")
    labels = s.split(".")
    if len(labels) < 2 or not all(_RE_LABEL.match(x) for x in labels) or labels[-1].isdigit():
        raise SsoError(400, "bad_domain", "domaine illisible (ex. example.com)")
    return s


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower().rstrip(".") if "@" in email else ""


def clean_mappings(v: Any) -> list[dict[str, str]]:
    if not isinstance(v, list):
        raise SsoError(400, "bad_request", "mappings : une liste de {group, role}")
    if len(v) > MAX_MAPPINGS:
        raise SsoError(400, "too_many", f"{MAX_MAPPINGS} correspondances au plus")
    out: list[dict[str, str]] = []
    for m in v:
        if not isinstance(m, dict):
            raise SsoError(400, "bad_request", "mappings : une liste de {group, role}")
        group = _str(m.get("group"), 256, "group", required=True)
        role = str(m.get("role") or "").strip()
        if role == "owner":
            raise SsoError(400, "owner_not_mappable", "le rôle owner ne s'obtient pas par SSO")
        if role not in MAPPABLE:
            raise SsoError(400, "bad_role", f"role : {' | '.join(MAPPABLE)}")
        if {"group": group, "role": role} not in out:
            out.append({"group": group, "role": role})
    return out


def connection_fields(body: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    """Champs d'une connexion depuis un corps de requête. Une VALEUR de secret n'a pas sa place ici (comme les noms de
    secrets d'un projet) : refusée avant tout, elle se pose par la route d'écriture seule `…/client-secret`."""
    if any(k in body for k in ("client_secret", "secret", "value", "token", "password")):
        raise SsoError(400, "secret_value_refused",
                       "le secret du client se pose par PUT …/client-secret ; ici, seulement son nom")
    allowed = {"name", "issuer", "client_id", "client_secret_name", "groups_claim", "token_auth"}
    unknown = set(body) - allowed
    if unknown:
        raise SsoError(400, "bad_request", f"champ inconnu : {min(unknown)}")
    cleaners = {"name": clean_name, "issuer": clean_issuer, "client_id": clean_client_id,
                "client_secret_name": clean_secret_name, "groups_claim": clean_groups_claim,
                "token_auth": clean_token_auth}
    out: dict[str, Any] = {}
    for k, fn in cleaners.items():
        if k in body:
            out[k] = fn(body[k])
        elif not partial and k in ("name", "issuer", "client_id", "client_secret_name"):
            raise SsoError(400, "bad_request", f"{k} requis")
    return out


__all__ = [
    "DEFAULT_MAX_AGE_S",
    "FLOW_TTL_S",
    "MAPPABLE",
    "MAX_CONNECTIONS",
    "MAX_DOMAINS",
    "TXT_PREFIX",
    "TXT_VALUE_PREFIX",
    "Binding",
    "Connection",
    "Domain",
    "Flow",
    "Identity",
    "SessionActor",
    "SsoError",
    "clean_domain",
    "clean_mappings",
    "clean_max_age",
    "connection_fields",
    "email_domain",
    "is_connection_id",
    "new_connection_id",
]
