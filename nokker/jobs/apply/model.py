"""Plan de remplissage d'un dépôt (docs/JOBS-PACK.md §6.5, §7.3) : ce que la carte montre, ce que l'approbation rejoue.

Un plan est IMMUABLE une fois la carte posée : son empreinte (`sha256` du JSON canonique) est dans les arguments de la
carte, donc dans son `args_sha256` ; le rejeu refuse un plan dont l'empreinte a changé.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass, field
from typing import Any

_ALPHA = "abcdefghijkmnpqrstuvwxyz23456789"


def new_plan_id() -> str:
    return "pl_" + "".join(secrets.choice(_ALPHA) for _ in range(12))


@dataclass
class PlanField:
    page: int
    id: str  # clé de la fiche (first_name…) ou id de question (q_…), ou consentement (c_…)
    label: str  # libellé du portail (texte de TIERS : clôturé pour le modèle, sur une ligne sur la carte)
    kind: str  # text | choice | check | file
    required: bool
    value: str = ""  # "" = laissé vide ; case : "true" | "false" ; fichier : rôle cv | letter
    source: str = ""  # fiche | réponse | pièce | défaut | consentement | vide
    options: list[str] = field(default_factory=list)
    sensitive: bool = False
    consent: str = ""  # privacy | retention | ai | transfer | marketing | other (cases de consentement)


@dataclass
class FilePart:
    role: str  # cv | letter
    ref: str  # ref de la pièce (em_…|cv:…, cvl_…, lt_…)
    name: str
    sha256: str
    size: int
    pages: int = 0
    new_facts: list[str] = field(default_factory=list)


@dataclass
class Question:
    id: str
    label: str
    kind: str
    options: list[str] = field(default_factory=list)
    required: bool = True
    sensitive: bool = False


@dataclass
class Plan:
    id: str
    profile: str  # em_…
    owner_account: str
    ats: str
    portal: str  # nom affiché (Workday)
    offer_ref: str
    offer_id: int
    company: str
    title: str
    location: str
    url: str
    account: str  # guest | login | create
    username: str  # identifiant du compte de portail (adresse de candidature) ; vide en invité
    connector: str = ""  # connecteur du portail (st_…)
    kind: str = "deposit"  # deposit | account (création du compte avant le relevé du formulaire)
    fields: list[PlanField] = field(default_factory=list)
    files: list[FilePart] = field(default_factory=list)
    strong: list[str] = field(default_factory=list)  # raisons d'une carte forte (§7.1)
    warnings: list[str] = field(default_factory=list)  # avertissements montrés sur la carte (§7.2)
    created_at: float = 0.0

    def canonical(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Plan":
        d = dict(d)
        d["fields"] = [PlanField(**f) for f in d.get("fields") or []]
        d["files"] = [FilePart(**f) for f in d.get("files") or []]
        return cls(**d)


__all__ = ["FilePart", "Plan", "PlanField", "Question", "new_plan_id"]
