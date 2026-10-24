"""Chiffrement au repos des identifiants de connecteurs : Fernet, clé NOKKER_CONNECTOR_KEY (hors base, hors dépôt)."""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from nokker.connectors.errors import ConnectorError

ENV_KEY = "NOKKER_CONNECTOR_KEY"


class SecretBox:
    """`SecretBox.from_env()` lève `key_missing` si la clé manque : pas de repli en clair."""

    def __init__(self, key: str | bytes) -> None:
        try:
            self._f = Fernet(key)
        except (ValueError, TypeError):
            raise ConnectorError("key_missing", f"{ENV_KEY} invalide (clé Fernet base64 de 32 octets attendue)") from None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> SecretBox:
        key = (env if env is not None else os.environ).get(ENV_KEY, "")
        if not key:
            raise ConnectorError("key_missing", f"{ENV_KEY} absente")
        return cls(key)

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()

    def seal(self, secret: str) -> bytes:
        return self._f.encrypt(secret.encode())

    def open(self, token: bytes) -> str:
        try:
            return self._f.decrypt(bytes(token)).decode()
        except InvalidToken:
            raise ConnectorError("key_missing", "secret illisible : clé de chiffrement différente de celle d'écriture") from None
