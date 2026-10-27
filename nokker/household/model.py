"""Modèle du foyer (lot X23, docs/LOKKAL.md §7.29). Validation et réglages seulement : ni stockage ni droits ici.

Un **foyer** est un projet d'équipe X17 de type `household` (`team_projects.kind`) : le payeur en est l'owner, chaque
membre y a le rôle `member` et garde SON locataire. Ce que le foyer ajoute à l'équipe :

  - au plus `max_members` membres, payeur compris (6, décision produit ; la base refuse au-delà de 6) ;
  - un foyer par compte (index unique en base) ;
  - un quota COMMUN de tours (jour et semaine, mêmes fenêtres que les plafonds de compte), compté dans le ledger, et
    un plafond par membre (défaut de la configuration, réglable par le payeur seul) ;
  - des profils `teen` (13-17 ans) et `child` (moins de 13 ans), derrière le drapeau `household_minors`, déclarés par
    le payeur qui atteste être le représentant légal (texte versionné par son empreinte, horodaté, révocable).

Les valeurs du palier (quota, plafond par membre, nombre de membres) sont des choix de configuration, pas du code :
`household_settings` et, quand la facturation est branchée, le palier ACTIF du payeur (`HOUSEHOLD_PLANS` de
nokker/billing/plans.py : ses plafonds font le quota commun et les sièges ; hors palier de foyer, le foyer est
suspendu, cf `Tier`).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path
from typing import Any

from nokker.billing.plans import DEFAULT_PLANS, plans_from_config
from nokker.team.model import TeamError

KIND = "household"
PROFILES = ("adult", "teen", "child")
MINOR_PROFILES = ("teen", "child")
STATUSES = ("active", "suspended")
HARD_MAX_MEMBERS = 6  # borne de la base (039) : la configuration peut la baisser, jamais la dépasser
GUARDIAN_RULE = "household:guardian"  # carte forte d'un profil mineur, décidée par son représentant (approvals)
LANGS = ("fr", "en")

# Quota commun et sièges : UNE seule source, les paliers de nokker/billing/plans.py (`DEFAULT_PLANS`, `famille` par
# défaut : 110 tours par jour, 550 par semaine, 6 sièges), surchargés par `billing_plans` de la configuration. Ici
# seulement ce qui n'est pas un palier : 40 tours par jour par membre par défaut, mode hard.
_FAMILLE = DEFAULT_PLANS["famille"].caps
DEFAULTS: dict[str, Any] = {"plan": "famille", "turns_per_day": _FAMILLE["turns_per_day"],
                            "turns_per_week": _FAMILLE["turns_per_week"], "member_turns_per_day": 40,
                            "max_members": min(int(_FAMILLE["household_members"]), HARD_MAX_MEMBERS), "mode": "hard"}

_CONSENT_DIR = Path(__file__).parent / "consent"
_RE_KIND = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


MINORS_MODES = ("all", "allowlist")


@dataclass(frozen=True)
class Settings:
    plan: str = DEFAULTS["plan"]
    turns_per_day: int | None = DEFAULTS["turns_per_day"]
    turns_per_week: int | None = DEFAULTS["turns_per_week"]
    member_turns_per_day: int | None = DEFAULTS["member_turns_per_day"]
    max_members: int = DEFAULTS["max_members"]
    mode: str = DEFAULTS["mode"]  # hard : refus au-delà ; soft : on compte, on prévient une fois, on ne coupe pas
    minors: bool = False  # drapeau `household_minors`
    # `all` : tout payeur peut déclarer des profils mineurs ; `allowlist` : seulement les payeurs que l'opérateur a
    # autorisés un par un (table household_minor_grants, `python -m nokker household minors allow`).
    minors_mode: str = "all"

    @classmethod
    def from_config(cls, settings: dict[str, Any] | None, *, billing_plans: dict[str, Any] | None = None,
                    minors: bool = False, minors_mode: str = "all") -> Settings:
        """`household_settings` de la configuration, puis le palier `plan` (défaut `famille`) des paliers de la
        facturation (`DEFAULT_PLANS` surchargés par `billing_plans`, la même table que `Billing.plans`) : ses
        `turns_per_day` / `turns_per_week` sont le quota commun, `household_members` les sièges (borné par
        `max_members` de `household_settings` s'il est posé)."""
        cfg = {**DEFAULTS, **(settings or {})}
        plan = str(cfg["plan"] or DEFAULTS["plan"])
        known = plans_from_config(billing_plans)
        tier = dict(known[plan].caps) if plan in known else {}
        for key in ("turns_per_day", "turns_per_week"):
            if key in tier:
                cfg[key] = tier[key]
        if "household_members" in tier and "max_members" not in (settings or {}):
            cfg["max_members"] = min(int(tier["household_members"]), HARD_MAX_MEMBERS)
        if minors_mode not in MINORS_MODES:
            raise ValueError(f"household_minors_mode inconnu : {minors_mode!r} ({' | '.join(MINORS_MODES)})")
        mode = str(cfg["mode"])
        if mode not in ("soft", "hard"):
            raise ValueError(f"household_settings.mode inconnu : {mode!r} (soft | hard)")

        def cap(key: str) -> int | None:
            v = cfg.get(key)
            if v is None:
                return None
            n = int(v)
            if n < 0:
                raise ValueError(f"household_settings.{key} : un entier positif ou null")
            return n

        max_members = int(cfg["max_members"])
        if not 1 <= max_members <= HARD_MAX_MEMBERS:
            raise ValueError(f"household_settings.max_members : entre 1 et {HARD_MAX_MEMBERS}")
        return cls(plan=plan, turns_per_day=cap("turns_per_day"), turns_per_week=cap("turns_per_week"),
                   member_turns_per_day=cap("member_turns_per_day"), max_members=max_members, mode=mode,
                   minors=bool(minors), minors_mode=minors_mode)

    def for_plan(self, plan: str, caps: dict[str, Any] | None) -> Settings:
        """Les réglages d'un foyer dont le payeur a le palier `plan` (facturation branchée) : `turns_per_day` /
        `turns_per_week` du palier font le quota commun, `household_members` les sièges (payeur compris, borné à
        `HARD_MAX_MEMBERS`). Une clé absente du palier garde la valeur de `household_settings`."""
        caps = caps or {}

        def pick(key: str, default: int | None) -> int | None:
            return default if key not in caps else (None if caps[key] is None else max(0, int(caps[key])))

        seats = caps.get("household_members")
        max_members = self.max_members if seats is None else max(1, min(int(seats), HARD_MAX_MEMBERS))
        return replace(self, plan=plan, turns_per_day=pick("turns_per_day", self.turns_per_day),
                       turns_per_week=pick("turns_per_week", self.turns_per_week), max_members=max_members)


TIER_STATES = ("active", "suspended")


@dataclass(frozen=True)
class Tier:
    """Ce qui s'applique à un foyer à cet instant (nokker/household/service.py::Household.tier). `active` : quota et
    sièges de `settings`. `suspended` : le payeur n'a plus de palier de foyer (Solo, résilié, impayé au-delà de
    `past_due`) — chaque membre revient à ses propres plafonds, rien n'est effacé, les invitations sont bloquées.
    `plan` : le palier actif du payeur (None : aucun), ou `household_settings.plan` sans facturation."""

    state: str
    settings: Settings
    plan: str | None

    @property
    def active(self) -> bool:
        return self.state == "active"


@dataclass
class HouseholdMember:
    household_id: str
    tenant_id: str  # locataire du FOYER (celui du payeur)
    account_id: str
    email: str = ""
    payer: bool = False
    profile: str = "adult"
    status: str = "active"
    cap_day: int | None = None
    cap_custom: bool = False  # posé par le payeur ; sinon le défaut de la configuration s'applique
    added_at: float = 0.0

    @property
    def minor(self) -> bool:
        return self.profile in MINOR_PROFILES

    def cap(self, settings: Settings) -> tuple[int | None, str]:
        """(plafond de tours par jour, source) : `custom` (posé par le payeur, None = aucun), `default` (configuration)
        ou `none` (le payeur, sans plafond tant qu'il n'en pose pas)."""
        if self.cap_custom:
            return self.cap_day, "custom"
        if self.payer:
            return None, "none"
        return settings.member_turns_per_day, "default"


@dataclass
class Consent:
    id: int
    household_id: str
    guardian_account: str
    minor_email: str
    profile: str
    version_sha: str
    lang: str
    granted_at: float
    invite_id: str | None = None
    revoked_at: float | None = None
    revoked_by: str | None = None
    ip: str | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@dataclass
class Policy:
    """Liste blanche du foyer pour ses profils mineurs : connecteurs (`mail`, `calendar`…) et agents (noms de packs).
    Vide par défaut : un profil mineur n'a ni connecteur ni agent tant que le payeur n'en ouvre pas."""

    minor_connectors: list[str] = field(default_factory=list)
    minor_agents: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"minor_connectors": list(self.minor_connectors), "minor_agents": list(self.minor_agents)}

    @classmethod
    def of(cls, raw: dict[str, Any] | None) -> Policy:
        raw = raw or {}
        return cls(minor_connectors=[str(x) for x in raw.get("minor_connectors") or []],
                   minor_agents=[str(x) for x in raw.get("minor_agents") or []])


# ─────────────────────────────── validation ───────────────────────────────


def clean_profile(v: Any, *, minors: bool) -> str:
    s = str(v or "adult").strip()
    if s not in PROFILES:
        raise TeamError(400, "bad_profile", f"profile : {' | '.join(PROFILES)}")
    if s in MINOR_PROFILES and not minors:
        raise TeamError(400, "minors_disabled", "les profils mineurs ne sont pas encore ouverts")
    return s


def clean_cap(body: Any) -> int | None:
    """`{turns_per_day: <entier ≥ 0> | null}` — null : aucun plafond pour ce membre (le quota du foyer reste)."""
    if not isinstance(body, dict) or "turns_per_day" not in body or set(body) - {"turns_per_day"}:
        raise TeamError(400, "bad_request", "corps attendu : {turns_per_day: <entier> | null}")
    v = body["turns_per_day"]
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 10_000:
        raise TeamError(400, "bad_request", "turns_per_day : un entier entre 0 et 10 000, ou null")
    return v


def clean_policy(body: Any) -> Policy:
    if not isinstance(body, dict) or set(body) - {"minor_connectors", "minor_agents"}:
        raise TeamError(400, "bad_request", "corps attendu : {minor_connectors?: [...], minor_agents?: [...]}")
    out: dict[str, list[str]] = {}
    for key in ("minor_connectors", "minor_agents"):
        items = body.get(key) or []
        if not isinstance(items, list) or len(items) > 50:
            raise TeamError(400, "bad_request", f"{key} : une liste (50 au plus)")
        names: list[str] = []
        for x in items:
            s = str(x or "").strip()
            if not _RE_KIND.match(s):
                raise TeamError(400, "bad_request", f"{key} : nom illisible {s[:40]!r}")
            if s not in names:
                names.append(s)
        out[key] = names
    return Policy(**out)


# ─────────────────────────────── attestation du représentant légal ───────────────────────────────


@dataclass(frozen=True)
class ConsentText:
    lang: str
    text: str
    sha256: str


@cache
def consent_text(lang: str) -> ConsentText:
    """Texte d'attestation affiché au payeur (`consent/<lang>.md`). Sa version est l'empreinte de ses octets : changer
    le texte change la version, et une attestation sur un texte périmé est refusée (409 `attestation_stale`)."""
    lang = lang if lang in LANGS else "fr"
    raw = (_CONSENT_DIR / f"{lang}.md").read_bytes()
    return ConsentText(lang=lang, text=raw.decode("utf-8"), sha256=hashlib.sha256(raw).hexdigest())


def clean_attestation(body: Any) -> tuple[str, str]:
    """`{version: <sha256 du texte affiché>, lang: fr|en, legal_guardian: true}` → (version, langue). Le payeur
    coche qu'il est le représentant légal ; sans cela, ou sur un texte périmé, rien n'est enregistré."""
    if not isinstance(body, dict):
        raise TeamError(400, "attestation_required",
                        "un profil mineur exige l'attestation du représentant légal : "
                        "{attestation: {version, lang, legal_guardian: true}}")
    if body.get("legal_guardian") is not True:
        raise TeamError(400, "attestation_required", "legal_guardian : true (le payeur atteste être le représentant "
                                                     "légal du mineur)")
    lang = str(body.get("lang") or "fr")
    if lang not in LANGS:
        raise TeamError(400, "bad_request", f"lang : {' | '.join(LANGS)}")
    version = str(body.get("version") or "")
    if version != consent_text(lang).sha256:
        raise TeamError(409, "attestation_stale", "texte d'attestation périmé : relire la version courante "
                                                  "(GET /v1/household/consent)")
    return version, lang


__all__ = ["DEFAULTS", "GUARDIAN_RULE", "HARD_MAX_MEMBERS", "KIND", "MINOR_PROFILES", "PROFILES", "TIER_STATES",
           "Consent", "ConsentText", "HouseholdMember", "Policy", "Settings", "Tier", "clean_attestation", "clean_cap",
           "clean_policy", "clean_profile", "consent_text"]
