"""Vérification du jeton Cloudflare Access de la console opérateur (espace-admin §2.1).

La console ne fait jamais confiance à un en-tête e-mail : elle vérifie elle-même `Cf-Access-Jwt-Assertion` (RS256,
clés publiques de l'organisation sur `https://<org>/cdn-cgi/access/certs`, audience = AUD de l'application, émetteur,
expiration). L'e-mail du jeton vérifié donne ensuite le rôle dans `operators` (côté appelant).
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib import request as urllib_request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

logger = logging.getLogger("nokker.admin.access")

CLOCK_SKEW_S = 60


class AccessDenied(Exception):
    """Jeton absent, illisible, mal signé, expiré, ou pour une autre application."""


def _b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _rsa_key(jwk: dict[str, Any]) -> rsa.RSAPublicKey:
    n = int.from_bytes(_b64(jwk["n"]), "big")
    e = int.from_bytes(_b64(jwk["e"]), "big")
    return rsa.RSAPublicNumbers(e, n).public_key()


def fetch_certs(url: str, timeout_s: float = 5.0) -> dict[str, Any]:
    with urllib_request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 — URL fixée par la config
        return json.loads(resp.read())


@dataclass
class AccessVerifier:
    """`team_domain` : ex. `<équipe>.cloudflareaccess.com`. `audience` : AUD de l'application Access (variable
    d'environnement). Clés gardées 1 h ; un `kid` inconnu relit les clés au plus une fois par minute (rotation)."""

    team_domain: str
    audience: str
    fetch: Callable[[str], dict[str, Any]] = fetch_certs
    now: Callable[[], float] = time.time
    keys_ttl_s: float = 3600.0
    _keys: dict[str, rsa.RSAPublicKey] = field(default_factory=dict, repr=False)
    _keys_at: float = field(default=0.0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"

    def _load_keys(self, force: bool = False) -> dict[str, rsa.RSAPublicKey]:
        with self._lock:
            age = self.now() - self._keys_at
            if self._keys and not (force and age > 60) and age < self.keys_ttl_s:
                return self._keys
            try:
                data = self.fetch(f"{self.issuer}/cdn-cgi/access/certs")
                keys = {k["kid"]: _rsa_key(k) for k in data.get("keys", []) if k.get("kty") == "RSA" and "kid" in k}
            except Exception:  # noqa: BLE001 — on garde les anciennes clés plutôt que de tout refuser
                logger.warning("certificats Access illisibles (%s)", self.team_domain, exc_info=True)
                return self._keys
            if keys:
                self._keys, self._keys_at = keys, self.now()
            return self._keys

    def verify(self, token: str) -> dict[str, Any]:
        """Claims du jeton s'il est valide pour cette application ; `AccessDenied` sinon."""
        if not self.audience:
            raise AccessDenied("audience Access non configurée")
        parts = (token or "").split(".")
        if len(parts) != 3:
            raise AccessDenied("jeton absent ou illisible")
        try:
            header = json.loads(_b64(parts[0]))
            claims = json.loads(_b64(parts[1]))
            signature = _b64(parts[2])
        except (ValueError, json.JSONDecodeError) as e:
            raise AccessDenied("jeton illisible") from e
        if header.get("alg") != "RS256":
            raise AccessDenied("algorithme refusé")
        kid = header.get("kid", "")
        key = self._load_keys().get(kid) or self._load_keys(force=True).get(kid)
        if key is None:
            raise AccessDenied("clé de signature inconnue")
        try:
            key.verify(signature, f"{parts[0]}.{parts[1]}".encode(), padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as e:
            raise AccessDenied("signature invalide") from e
        aud = claims.get("aud")
        if self.audience not in (aud if isinstance(aud, list) else [aud]):
            raise AccessDenied("autre application")
        if claims.get("iss") != self.issuer:
            raise AccessDenied("autre émetteur")
        now = self.now()
        if float(claims.get("exp", 0)) < now - CLOCK_SKEW_S:
            raise AccessDenied("jeton expiré")
        if float(claims.get("nbf", 0)) > now + CLOCK_SKEW_S:
            raise AccessDenied("jeton pas encore valide")
        if not str(claims.get("email", "")).strip():
            raise AccessDenied("jeton sans e-mail (jeton de service)")
        return claims


__all__ = ["AccessDenied", "AccessVerifier", "fetch_certs"]
