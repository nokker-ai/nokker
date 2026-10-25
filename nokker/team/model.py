"""Modèle de l'équipe (lot X17, docs/LOKKAL.md §7.15) : projets, membres, invitations, catalogue de skills, règles de
permission, noms de secrets, notes de projet en miroir. Validation seulement : ni stockage ni droits ici.

Un **projet** est un périmètre DANS le locataire de son propriétaire (`tenant_id`) : dépôts déclarés (clés de projet
de la mémoire X1, `hôte/chemin`), noms de secrets (jamais de valeur), budget mensuel, règles de permission qui ne font
que restreindre, catalogue de skills, choix du miroir pour les notes de projet. Les membres sont des comptes nokker.ai,
de n'importe quel locataire, avec un rôle :

    owner > maintainer > member > viewer

  - viewer     : lit tout le projet, n'écrit rien ;
  - member     : en plus, pousse des notes de projet (miroir) et rattache SES agents au projet ;
  - maintainer : en plus, catalogue, règles, noms de secrets, dépôts, budget, nom, relecture des notes en quarantaine ;
  - owner      : en plus, membres, invitations, miroir, suppression du projet. Un seul : le créateur.
"""
from __future__ import annotations

import math
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

ROLES = ("owner", "maintainer", "member", "viewer")
RANK = {"viewer": 0, "member": 1, "maintainer": 2, "owner": 3}
INVITABLE = ("maintainer", "member", "viewer")  # owner : seulement le créateur
NOTE_KINDS = ("rule", "feedback", "fact")  # notes apprises de X1 ; les sections des fichiers ne voyagent pas
CURRENCIES = ("EUR", "CHF", "USD")
INVITE_TTL_S = 14 * 86400

MAX_OWNED_PROJECTS = 20
MAX_MEMBERS = 50
MAX_PENDING_INVITES = 50
MAX_REPOS = 50
MAX_SECRET_NAMES = 100
MAX_SKILLS = 100
MAX_RULES = 200
MAX_NOTES = 5000
MAX_NOTES_PER_PUSH = 200
MAX_NOTE_TITLE = 120  # mêmes bornes que `memory_note` (docs/LOKKAL.md §7.11.6)
MAX_NOTE_TEXT = 1500

_RE_PROJECT_ID = re.compile(r"^pj_[0-9a-f]{12}$")
_RE_INVITE_ID = re.compile(r"^iv_[0-9a-f]{16}$")
_RE_ACCOUNT = re.compile(r"^us_[0-9a-z_]{1,40}$")
_RE_SKILL = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_RE_NOTE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,159}$")
_RE_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}$")


class TeamError(Exception):
    """Refus d'une demande : `status` HTTP, `code` stable pour les clients, message en français."""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.status, self.code, self.message = status, code, message or code


def not_found() -> TeamError:
    # Un non-membre ne doit pas apprendre qu'un projet existe (docs/TENANCY.md §3) : même réponse qu'un id inconnu.
    return TeamError(404, "not_found", "projet inconnu")


def new_project_id() -> str:
    return "pj_" + secrets.token_hex(6)


def new_invite_id() -> str:
    return "iv_" + secrets.token_hex(8)


def is_project_id(s: str) -> bool:
    return bool(_RE_PROJECT_ID.match(s or ""))


def is_invite_id(s: str) -> bool:
    return bool(_RE_INVITE_ID.match(s or ""))


def is_account_id(s: str) -> bool:
    return bool(_RE_ACCOUNT.match(s or ""))


@dataclass
class Project:
    id: str
    tenant_id: str
    name: str
    owner_account: str
    description: str = ""
    repos: list[str] = field(default_factory=list)  # clés `hôte/chemin` (normalize_remote de X1)
    secret_names: list[dict[str, str]] = field(default_factory=list)  # [{name, description}] — jamais de valeur
    budget: dict[str, Any] | None = None  # {monthly, currency}
    rules: dict[str, Any] = field(default_factory=dict)  # {mode?, ask: [], deny: []}
    mirror: bool = False  # notes de projet en miroir pour les membres (§6.3, §6.7) ; défaut : en Lokkal seulement
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class Member:
    project_id: str
    tenant_id: str  # locataire du PROJET (pas celui du membre)
    account_id: str
    role: str
    email: str = ""
    added_by: str = ""
    added_at: float = field(default_factory=time.time)


@dataclass
class Invite:
    id: str
    project_id: str
    tenant_id: str
    project_name: str  # recopié : l'invité ne lit pas le projet avant d'y entrer
    email: str
    role: str
    invited_by: str
    invitee_account: str = ""  # résolu à l'invitation si le compte existe, sinon par l'e-mail à la connexion
    state: str = "pending"  # pending | accepted | declined | revoked
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    decided_at: float | None = None


@dataclass
class Skill:
    project_id: str
    tenant_id: str
    name: str
    source: str
    description: str = ""
    requires_local: bool = False  # exige une ressource locale : ne s'exécute qu'en Lokkal (§6.7)
    added_by: str = ""
    added_at: float = field(default_factory=time.time)


@dataclass
class Note:
    project_id: str
    tenant_id: str
    name: str
    kind: str
    title: str
    text: str
    sha: str
    updated_at: float
    author_account: str
    author_machine: str = ""
    origin: str = ""
    derived_from: str = ""
    links: list[str] = field(default_factory=list)
    deleted: bool = False
    seq: int = 0  # curseur du tirage, croissant à chaque écriture


# ─────────────────────────────── validation ───────────────────────────────


def _text(v: Any, n: int, what: str, *, required: bool = False) -> str:
    s = "" if v is None else str(v).strip()
    if required and not s:
        raise TeamError(400, "bad_request", f"{what} requis")
    if len(s) > n:
        raise TeamError(400, "bad_request", f"{what} : {n} caractères au plus")
    return s


def clean_name(v: Any) -> str:
    return _text(v, 80, "name", required=True)


def clean_description(v: Any) -> str:
    return _text(v, 500, "description")


def clean_email(v: Any) -> str:
    s = _text(v, 254, "email", required=True).lower()
    if not _RE_EMAIL.match(s):
        raise TeamError(400, "bad_request", "email invalide")
    return s


def clean_role(v: Any, allowed: tuple[str, ...] = INVITABLE) -> str:
    s = str(v or "").strip()
    if s not in allowed:
        raise TeamError(400, "bad_role", f"role : {' | '.join(allowed)}")
    return s


def clean_repos(v: Any) -> list[str]:
    """Dépôts du projet → clés `hôte/chemin`, la même normalisation que la mémoire de projet (X1, §7.11.2) : https et
    ssh d'un même dépôt donnent la même clé, et un jeton glissé dans l'URL n'est pas gardé."""
    from nokker.context.project_memory import normalize_remote

    if not isinstance(v, list):
        raise TeamError(400, "bad_request", "repos : une liste")
    out: list[str] = []
    for raw in v:
        s = str(raw or "").strip()
        if not s or len(s) > 300 or any(c.isspace() for c in s):
            raise TeamError(400, "bad_request", f"dépôt illisible : {s[:60]!r}")
        key = normalize_remote(s.removeprefix("remote:"))
        if "/" not in key or key.startswith("/"):
            raise TeamError(400, "bad_request", f"dépôt sans hôte/chemin : {key[:60]!r}")
        if key not in out:
            out.append(key)
    if len(out) > MAX_REPOS:
        raise TeamError(400, "too_many", f"{MAX_REPOS} dépôts au plus")
    return out


def memory_project(repo_key: str) -> str:
    """Projet CortHeXis local du dépôt (`p-<sha1(clé)>`, docs/LOKKAL.md §7.11.2) : la machine d'un membre range les
    notes de ce dépôt sous ce nom, c'est la même clé de part et d'autre."""
    from nokker.context.project_memory import project_name

    return project_name(f"remote:{repo_key}")


def clean_secret_name(name: Any, body: Any = None) -> dict[str, str]:
    """Un nom du coffre (X4, `NAME_RE`) et une description. Une VALEUR n'a pas sa place ici : un projet liste des noms,
    chaque membre pose sa valeur dans SON coffre (docs/LOKKAL.md §7.10, §7.15)."""
    from nokker.secrets.base import NAME_RE

    body = body if isinstance(body, dict) else {}
    if any(k in body for k in ("value", "secret", "token", "password")):
        raise TeamError(400, "secret_value_refused",
                        "un projet liste des NOMS de secrets ; chaque membre pose sa valeur dans son coffre")
    s = str(name or "").strip()
    if not NAME_RE.match(s):
        raise TeamError(400, "bad_name", "nom de secret : lettres, chiffres, « _ », « . », « - » (128 au plus)")
    return {"name": s, "description": _text(body.get("description"), 300, "description")}


def clean_rules(v: Any) -> dict[str, Any]:
    """Règles de permission du projet : forme des réglages `permissions` de `.nokker/settings.json`, mais RESTREINDRE
    seulement — `mode` plus strict, `ask`, `deny`. Une règle `allow` ou le mode `auto` élargiraient ce que
    l'utilisateur décide sur sa machine : refusés (docs/LOKKAL.md §7.15, nokker/qa/permissions.py)."""
    from nokker.qa.permissions import Rule

    if not isinstance(v, dict):
        raise TeamError(400, "bad_request", "rules : un objet {mode?, ask, deny}")
    if "allow" in v or "thread_allow" in v:
        raise TeamError(400, "rules_widen", "une règle de projet ne peut que restreindre : pas de « allow »")
    unknown = set(v) - {"mode", "ask", "deny"}
    if unknown:
        raise TeamError(400, "bad_request", f"clé inconnue : {min(unknown)}")
    out: dict[str, Any] = {"ask": [], "deny": []}
    mode = v.get("mode")
    if mode not in (None, ""):
        if mode == "auto":
            raise TeamError(400, "rules_widen", "mode « auto » : un projet ne peut que restreindre")
        if mode not in ("plan", "default", "accept_edits"):
            raise TeamError(400, "bad_request", "mode : plan | default | accept_edits")
        out["mode"] = mode
    for key in ("ask", "deny"):
        items = v.get(key) or []
        if not isinstance(items, list):
            raise TeamError(400, "bad_request", f"{key} : une liste")
        for r in items:
            s = str(r or "").strip()
            if not s or len(s) > 300:
                raise TeamError(400, "bad_rule", f"règle illisible : {s[:60]!r}")
            try:
                Rule.parse(s)
            except ValueError as e:
                raise TeamError(400, "bad_rule", str(e)) from None
            if s not in out[key]:
                out[key].append(s)
    if len(out["ask"]) + len(out["deny"]) > MAX_RULES:
        raise TeamError(400, "too_many", f"{MAX_RULES} règles au plus")
    return out


def clean_skill(name: Any, body: Any) -> dict[str, Any]:
    """Une entrée du catalogue : une SOURCE de skill (mêmes formes que `skill_install`, nokker/tools/skills.py), pas
    son contenu. Chaque membre l'installe chez lui, scannée et sous SA carte."""
    body = body if isinstance(body, dict) else {}
    n = str(name or "").strip()
    if not _RE_SKILL.match(n):
        raise TeamError(400, "bad_name", "nom de skill : minuscules, chiffres, « _ », « . », « - » (64 au plus)")
    source = _text(body.get("source"), 500, "source", required=True)
    parts = source.split("/")
    if source.startswith("github:"):
        ident = source[len("github:"):].strip("/")
        if len(ident.split("/")) < 2 or ".." in ident.split("/") or not re.match(r"^[A-Za-z0-9_./-]+$", ident):
            raise TeamError(400, "bad_source", "source github : github:<owner>/<repo>[/<chemin>]")
    elif source.startswith("https://"):
        if any(c.isspace() for c in source) or "@" in source.split("/", 3)[2]:
            raise TeamError(400, "bad_source", "URL https sans identifiants")
    elif not (source.startswith("official/") and len(parts) >= 2 and ".." not in parts):
        raise TeamError(400, "bad_source", "source : github:<owner>/<repo>[/<chemin>], URL https, ou official/<famille>/<nom>")
    return {"name": n, "source": source, "description": _text(body.get("description"), 300, "description"),
            "requires_local": body.get("requires_local") is True}


def clean_budget(v: Any) -> dict[str, Any] | None:
    if v is None:
        return None
    if not isinstance(v, dict):
        raise TeamError(400, "bad_request", "budget : {monthly, currency} ou null")
    try:
        monthly = float(v.get("monthly"))
    except (TypeError, ValueError):
        raise TeamError(400, "bad_request", "budget.monthly : un nombre") from None
    if not math.isfinite(monthly) or monthly < 0 or monthly > 1_000_000:
        raise TeamError(400, "bad_request", "budget.monthly : entre 0 et 1 000 000")
    currency = str(v.get("currency") or "EUR").upper()
    if currency not in CURRENCIES:
        raise TeamError(400, "bad_request", f"budget.currency : {' | '.join(CURRENCIES)}")
    return {"monthly": round(monthly, 2), "currency": currency}


def clean_note(d: Any) -> dict[str, Any]:
    """Note de projet poussée par une machine : une note APPRISE de X1 (`rule`, `feedback`, `fact`, portée projet).
    Les sections des fichiers (`src-…`) ne voyagent pas : chaque membre a le dépôt."""
    if not isinstance(d, dict):
        raise TeamError(400, "bad_note", "note : un objet")
    name = str(d.get("name") or "").strip()
    if not _RE_NOTE.match(name) or name.startswith("src-"):
        raise TeamError(400, "bad_note", f"nom de note refusé : {name[:60]!r}")
    kind = str(d.get("kind") or "")
    if kind not in NOTE_KINDS:
        raise TeamError(400, "bad_note", f"kind : {' | '.join(NOTE_KINDS)}")
    if d.get("scope") not in (None, "project"):
        raise TeamError(400, "bad_note", "seules les notes de portée projet passent par le projet")
    try:
        updated = float(d.get("updated_at"))
    except (TypeError, ValueError):
        raise TeamError(400, "bad_note", "updated_at : un horodatage") from None
    if not math.isfinite(updated) or updated <= 0:
        raise TeamError(400, "bad_note", "updated_at : un horodatage")
    links = d.get("links") or []
    if not isinstance(links, list) or len(links) > 20 or not all(_RE_NOTE.match(str(x)) for x in links):
        raise TeamError(400, "bad_note", "links : 20 noms de notes au plus")
    deleted = d.get("deleted") is True
    return {"name": name, "kind": kind, "deleted": deleted,
            "title": _text(d.get("title"), MAX_NOTE_TITLE, "title", required=not deleted),
            "text": _text(d.get("text"), MAX_NOTE_TEXT, "text", required=not deleted),
            "updated_at": updated, "base_sha": str(d.get("base_sha") or ""),
            "origin": _text(d.get("origin"), 200, "origin"),
            "derived_from": str(d.get("derived_from") or "")[:20], "links": [str(x) for x in links]}


__all__ = [
    "CURRENCIES",
    "INVITABLE",
    "INVITE_TTL_S",
    "MAX_MEMBERS",
    "MAX_NOTES",
    "MAX_NOTES_PER_PUSH",
    "MAX_OWNED_PROJECTS",
    "MAX_PENDING_INVITES",
    "MAX_SECRET_NAMES",
    "MAX_SKILLS",
    "NOTE_KINDS",
    "RANK",
    "ROLES",
    "Invite",
    "Member",
    "Note",
    "Project",
    "Skill",
    "TeamError",
    "clean_budget",
    "clean_description",
    "clean_email",
    "clean_name",
    "clean_note",
    "clean_repos",
    "clean_role",
    "clean_rules",
    "clean_secret_name",
    "clean_skill",
    "is_account_id",
    "is_invite_id",
    "is_project_id",
    "memory_project",
    "new_invite_id",
    "new_project_id",
    "not_found",
]
