"""Sites connectés — identifiants au coffre (docs/CONNECTED-SITES.md §5.5).

Le mot de passe d'un connecteur est rangé au coffre de Lokkal (`SecretsProvider`, `file` ou `openbao`) sous
`sites.<id>.password`, portée `user`. Il n'en sort que par `use_password`, appelé par l'opération nommée `login` d'une
recette quand la session a EXPIRÉ (ou à la première connexion) : chaque lecture est journalisée avec son motif, la
valeur est passée à un rappel (la frappe dans la page) et n'est jamais rendue à l'appelant. Toute valeur rangée ou lue
est ajoutée aux secrets connus du caviardage (`KnownSecrets`). Un code de vérification n'est JAMAIS rangé ici.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nokker.sites.model import Connector

REASON_EXPIRED = "session expirée"
REASON_FIRST = "première connexion"
REASONS = (REASON_EXPIRED, REASON_FIRST)


class CredentialsError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def secret_name(conn: Connector | str) -> str:
    cid = conn if isinstance(conn, str) else conn.id
    return f"sites.{cid}.password"


class Credentials:
    def __init__(self, vault: Any, store: Any, known: Any = None, on_known: Callable[[str, str], None] | None = None):
        self.vault = vault  # SecretsProvider (portée user)
        self.store = store
        self.known = known  # KnownSecrets du noyau (caviardage), facultatif
        self.on_known = on_known  # rappel supplémentaire (ex. scrubber d'un autre processus)

    def remember(self, name: str, value: str) -> None:
        """La valeur rejoint le caviardage (sans lecture du coffre)."""
        if self.known is not None:
            self.known.add(name, value)
        if self.on_known is not None:
            self.on_known(name, value)

    def has(self, conn: Connector) -> bool:
        return any(i.name == secret_name(conn) for i in self.vault.list())

    def updated_at(self, conn: Connector) -> str | None:
        for i in self.vault.list():
            if i.name == secret_name(conn):
                return i.updated_at.isoformat() if i.updated_at else None
        return None

    def save(self, conn: Connector, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise CredentialsError("bad_value", "mot de passe vide")
        self.vault.set(secret_name(conn), value)
        self.remember(secret_name(conn), value)

    def delete(self, conn: Connector) -> bool:
        return bool(self.vault.delete(secret_name(conn)))

    def use_password(self, conn: Connector, reason: str, use: Callable[[str], Any]) -> Any:
        """Lit le mot de passe pour UNE connexion et le passe à `use` (frappe dans la page). Journalisé. Rend ce que
        rend `use` (jamais la valeur)."""
        if reason not in REASONS:
            raise CredentialsError("bad_reason", "lecture d'identifiant refusée : motif non admis")
        value = self.vault.get(secret_name(conn))
        if not value:
            raise CredentialsError("missing", "aucun mot de passe enregistré")
        self.store.log_credential_read(conn, reason)
        self.remember(secret_name(conn), value)
        result = use(value)
        if result == value:  # garde : un rappel ne rend jamais la valeur
            raise CredentialsError("leak", "le rappel a rendu la valeur")
        return result

    # Pas de chargement au démarrage ici : le noyau ajoute déjà toutes les valeurs du coffre au caviardage
    # (docs/LOKKAL.md §7.10). Lire le coffre hors d'une connexion serait une lecture non journalisée.


__all__ = ["Credentials", "CredentialsError", "REASON_EXPIRED", "REASON_FIRST", "secret_name"]
