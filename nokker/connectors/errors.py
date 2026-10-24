"""Erreurs des connecteurs : un code stable (affiché par la carte Connecteurs) et un message sans secret."""
from __future__ import annotations

CODES = ("bad_settings", "host_blocked", "port_not_allowed", "unreachable", "tls_failed", "auth_failed",
         "protocol_error", "not_configured", "key_missing", "unsupported")


class ConnectorError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def scrub(text: str, *secrets: str) -> str:
    """Retire toute occurrence des secrets (mot de passe d'application, jeton) d'un texte avant qu'il sorte."""
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "***")
    return text
