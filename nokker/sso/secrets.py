"""Secrets des clients OIDC (drapeau `sso`) : la base ne garde que le NOM (`sso_connections.secret_name`), la valeur
vit au coffre du noyau de nokker.ai, portée `system`, rangée par locataire (`sso.<tn_…>.<nom>`) : deux équipes qui
choisissent le même nom ne se voient jamais. Écriture seule depuis l'API (`PUT …/client-secret`) ; la valeur n'est lue
qu'au moment de l'échange du code, jamais rendue, jamais journalisée."""
from __future__ import annotations

import threading
from typing import Any, Protocol


class SsoSecrets(Protocol):
    def get(self, tenant: str, name: str) -> str | None: ...
    def set(self, tenant: str, name: str, value: str) -> None: ...
    def has(self, tenant: str, name: str) -> bool: ...
    def delete(self, tenant: str, name: str) -> bool: ...


def _key(tenant: str, name: str) -> str:
    return f"sso.{tenant}.{name}"


class MemorySsoSecrets:
    def __init__(self) -> None:
        self._v: dict[str, str] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # jamais les valeurs
        return f"MemorySsoSecrets({len(self._v)} noms)"

    def get(self, tenant: str, name: str) -> str | None:
        with self._lock:
            return self._v.get(_key(tenant, name))

    def set(self, tenant: str, name: str, value: str) -> None:
        with self._lock:
            self._v[_key(tenant, name)] = value

    def has(self, tenant: str, name: str) -> bool:
        return self.get(tenant, name) is not None

    def delete(self, tenant: str, name: str) -> bool:
        with self._lock:
            return self._v.pop(_key(tenant, name), None) is not None


class VaultSsoSecrets:
    """Sur un `SecretsProvider` de portée `system` (fichier chiffré ou OpenBao, nokker/secrets/)."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider

    def __repr__(self) -> str:
        return f"VaultSsoSecrets({getattr(self.provider, 'kind', '?')})"

    def get(self, tenant: str, name: str) -> str | None:
        return self.provider.get(_key(tenant, name))

    def set(self, tenant: str, name: str, value: str) -> None:
        self.provider.set(_key(tenant, name), value)

    def has(self, tenant: str, name: str) -> bool:
        return self.get(tenant, name) is not None

    def delete(self, tenant: str, name: str) -> bool:
        return bool(self.provider.delete(_key(tenant, name)))


__all__ = ["MemorySsoSecrets", "SsoSecrets", "VaultSsoSecrets"]
