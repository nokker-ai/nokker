"""Aides des tests du coffre (X4) : valeurs factices MORCELÉES (aucun motif de jeton complet dans le source) et un
fournisseur en mémoire qui suit le contrat `SecretsProvider`."""
from __future__ import annotations

from datetime import UTC, datetime

from cryptography.fernet import Fernet

from nokker.secrets.base import SecretInfo, SecretsError, check_name, check_value

# Valeurs factices : assemblées à l'exécution, jamais écrites d'un bloc.
FAKE_A = "fx" + "Q9" + "-kv7" + "Lm2p" + "Zt"
FAKE_B = "fy" + "W3" + "_pr8" + "Na5c" + "Ue"
FAKE_C = "fz" + "J6" + ".hd4" + "Bo1x" + "Ks"


def fernet_key() -> str:
    return Fernet.generate_key().decode()


class MemorySecrets:
    """Fournisseur en mémoire (contrat seulement) ; `fail` = code d'erreur à lever sur tout appel."""

    kind = "memory"

    def __init__(self, scope: str = "user") -> None:
        self.scope = scope
        self.data: dict[str, tuple[str, datetime]] = {}
        self.fail: str | None = None

    def _gate(self) -> None:
        if self.fail:
            raise SecretsError("coffre scellé" if self.fail == "sealed" else "indisponible", self.fail)

    def get(self, name):
        self._gate()
        got = self.data.get(name)
        return got[0] if got else None

    def set(self, name, value):
        self._gate()
        check_name(name, self.scope)
        check_value(value)
        now = datetime.now(UTC)
        self.data[name] = (value, now)
        return SecretInfo(name, now)

    def delete(self, name):
        self._gate()
        return self.data.pop(name, None) is not None

    def list(self):
        self._gate()
        return [SecretInfo(k, v[1]) for k, v in sorted(self.data.items())]
