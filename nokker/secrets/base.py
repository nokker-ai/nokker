"""Contrat du coffre local (lot X4, docs/en/secrets.md, docs/LOKKAL.md §7.10).

Un `SecretsProvider` range des valeurs sous un NOM. Quatre opérations, et une seule qui rend une valeur :
  - `get(nom)`            → la valeur, ou None si le nom n'existe pas ;
  - `set(nom, valeur)`    → `SecretInfo` (nom + date de mise à jour) ;
  - `delete(nom)`         → True si le nom existait ;
  - `list()`              → `[SecretInfo]`, triés par nom : JAMAIS de valeur.

Règles communes à tous les fournisseurs :
  - un nom est court et sans espace (`NAME_RE`) ; il sert de poignée au caviardage (`[secret:NOM]`) et de
    référence dans les configs (`secret:NOM`, nokker/secrets/refs.py) ;
  - une valeur est une chaîne non vide, bornée (`MAX_VALUE_BYTES`, de quoi tenir une clé ssh) ;
  - fournisseur indisponible (clé absente, coffre scellé, réseau) → `SecretsError`, jamais de repli sur un autre
    fournisseur ni sur un fichier en clair ;
  - aucun message d'erreur, aucun `repr`, aucun journal ne contient une valeur.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

# Nom d'un secret de l'utilisateur : lettre ou chiffre d'abord, puis lettres, chiffres, « _ », « . », « - ».
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
# Entrées du noyau (portée « system ») : en plus « : » (`byok:<nom>`).
SYSTEM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
MAX_VALUE_BYTES = 64 * 1024
SCOPES = ("user", "system")


class SecretsError(RuntimeError):
    """Coffre inutilisable ou demande refusée. `code` : `unavailable` (clé absente, coffre scellé, injoignable),
    `bad_name`, `bad_value`, `config`, `missing` (référence vers un nom absent), `denied` (droits refusés)."""

    def __init__(self, message: str, code: str = "unavailable") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SecretInfo:
    """Ce qu'on peut montrer d'un secret : son nom et sa date de mise à jour (UTC), jamais sa valeur."""

    name: str
    updated_at: datetime | None

    def as_dict(self) -> dict[str, str | None]:
        return {"name": self.name, "updated_at": self.updated_at.isoformat() if self.updated_at else None}


@runtime_checkable
class SecretsProvider(Protocol):
    """Un fournisseur de coffre, pour UNE portée (`user` : les secrets de l'utilisateur ; `system` : clés du noyau,
    comme la clé du Router ou le jeton de machine). `kind` : `file` | `openbao`."""

    kind: str
    scope: str

    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> SecretInfo: ...

    def delete(self, name: str) -> bool: ...

    def list(self) -> list[SecretInfo]: ...


def check_name(name: str, scope: str = "user") -> str:
    rx = SYSTEM_NAME_RE if scope == "system" else NAME_RE
    if not isinstance(name, str) or not rx.fullmatch(name):
        raise SecretsError("nom de secret invalide : lettres, chiffres, « _ », « . », « - » (128 au plus), "
                           "commence par une lettre ou un chiffre", "bad_name")
    return name


def check_value(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise SecretsError("valeur de secret requise (chaîne non vide)", "bad_value")
    if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        raise SecretsError(f"valeur de secret trop longue (plus de {MAX_VALUE_BYTES // 1024} Kio)", "bad_value")
    return value


__all__ = ["MAX_VALUE_BYTES", "NAME_RE", "SCOPES", "SYSTEM_NAME_RE", "SecretInfo", "SecretsError", "SecretsProvider",
           "check_name", "check_value"]
