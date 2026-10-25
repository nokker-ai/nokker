"""Vérification d'un ID token OIDC (JWS compact), stricte et sans bibliothèque JWT : on ne fait confiance qu'à ce
qu'on vérifie ici (OIDC Core §3.1.3.7, RFC 8725).

- Algorithmes : **RS256 et ES256 seulement**. `none`, HS*, PS*, tout le reste : refusés (`bad_alg`), avant même de
  chercher une clé. L'algorithme doit correspondre au type de la clé choisie (RSA pour RS256, EC P-256 pour ES256) :
  pas de confusion d'algorithme.
- Clé : prise dans le JWKS du fournisseur (découverte), par `kid` ; les en-têtes `jwk`, `jku`, `x5u`, `x5c` ne sont
  JAMAIS utilisés. `crit` présent → refusé. RSA de moins de 2048 bits → refusée.
- Claims : `iss` exactement l'issuer configuré ; `aud` contient le client_id ; plusieurs audiences → `azp` exigé et
  égal au client_id (et `azp` présent est toujours le client_id) ; `exp` exigé, `iat` exigé et ni dans le futur ni
  vieux de plus de 10 minutes, `nbf` respecté, avec une tolérance de 60 s ; `nonce` égal à celui de la connexion
  (comparaison à temps constant) ; `sub` non vide.

Une erreur porte un `code` court (journal d'audit) ; jamais le jeton ni un morceau du jeton.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
from collections.abc import Callable
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

ALGS = ("RS256", "ES256")
LEEWAY_S = 60
MAX_IAT_AGE_S = 600
MAX_TOKEN_BYTES = 16 * 1024
MIN_RSA_BITS = 2048


class TokenInvalid(Exception):
    """`code` : malformed, bad_alg, bad_header, unknown_key, weak_key, bad_signature, bad_iss, bad_aud, bad_azp,
    expired, not_yet_valid, bad_iat, bad_nonce, bad_sub."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def b64u_decode(s: str) -> bytes:
    if not isinstance(s, str) or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                                     for c in s):
        raise TokenInvalid("malformed")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (binascii.Error, ValueError):
        raise TokenInvalid("malformed") from None


def _json(raw: bytes) -> dict[str, Any]:
    try:
        d = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise TokenInvalid("malformed") from None
    if not isinstance(d, dict):
        raise TokenInvalid("malformed")
    return d


def _b64_int(v: Any) -> int:
    if not isinstance(v, str) or not v:
        raise TokenInvalid("unknown_key")
    return int.from_bytes(b64u_decode(v), "big")


def _public_key(jwk: dict[str, Any], alg: str) -> Any:
    kty = jwk.get("kty")
    if alg == "RS256":
        if kty != "RSA":
            raise TokenInvalid("bad_signature")
        key = rsa.RSAPublicNumbers(_b64_int(jwk.get("e")), _b64_int(jwk.get("n"))).public_key()
        if key.key_size < MIN_RSA_BITS:
            raise TokenInvalid("weak_key")
        return key
    if kty != "EC" or jwk.get("crv") != "P-256":
        raise TokenInvalid("bad_signature")
    x, y = b64u_decode(str(jwk.get("x") or "")), b64u_decode(str(jwk.get("y") or ""))
    if len(x) != 32 or len(y) != 32:
        raise TokenInvalid("unknown_key")
    try:
        return ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"),
                                             ec.SECP256R1()).public_key()
    except ValueError:
        raise TokenInvalid("unknown_key") from None


def _pick_key(candidates: list[dict[str, Any]], kid: str | None, alg: str) -> dict[str, Any]:
    usable = [k for k in candidates if isinstance(k, dict) and k.get("use") in (None, "sig")
              and k.get("alg") in (None, alg)]
    if kid is not None:
        usable = [k for k in candidates if isinstance(k, dict) and k.get("kid") == kid and k.get("use") in (None, "sig")]
        if len(usable) != 1:
            raise TokenInvalid("unknown_key")
        if usable[0].get("alg") not in (None, alg):
            raise TokenInvalid("bad_signature")
        return usable[0]
    kty = "RSA" if alg == "RS256" else "EC"
    usable = [k for k in usable if k.get("kty") == kty]
    if len(usable) != 1:  # sans kid, on n'essaie pas plusieurs clés : une seule possible, ou rien
        raise TokenInvalid("unknown_key")
    return usable[0]


def _check_signature(key: Any, alg: str, signing_input: bytes, sig: bytes) -> None:
    try:
        if alg == "RS256":
            key.verify(sig, signing_input, padding.PKCS1v15(), hashes.SHA256())
        else:
            if len(sig) != 64:
                raise TokenInvalid("bad_signature")
            der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
            key.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise TokenInvalid("bad_signature") from None


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def verify_id_token(token: str, *, keys: Callable[[str | None], list[dict[str, Any]]], issuer: str, client_id: str,
                    nonce: str, now: float, leeway: float = LEEWAY_S, max_iat_age: float = MAX_IAT_AGE_S
                    ) -> dict[str, Any]:
    """Rend les claims d'un ID token VÉRIFIÉ, ou lève `TokenInvalid`. `keys(kid)` : les JWK du fournisseur (le
    cache de nokker/sso/idp.py recharge le JWKS une fois si le `kid` est inconnu : rotation)."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_BYTES:
        raise TokenInvalid("malformed")
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenInvalid("malformed")
    header = _json(b64u_decode(parts[0]))
    alg = header.get("alg")
    if alg not in ALGS:
        raise TokenInvalid("bad_alg")
    if "crit" in header:
        raise TokenInvalid("bad_header")
    kid = header.get("kid")
    if kid is not None and not isinstance(kid, str):
        raise TokenInvalid("bad_header")
    if not parts[2]:
        raise TokenInvalid("bad_signature")
    sig = b64u_decode(parts[2])
    payload_raw = b64u_decode(parts[1])
    key = _public_key(_pick_key(list(keys(kid) or []), kid, alg), alg)
    _check_signature(key, alg, f"{parts[0]}.{parts[1]}".encode("ascii"), sig)
    c = _json(payload_raw)

    if c.get("iss") != issuer:
        raise TokenInvalid("bad_iss")
    aud = c.get("aud")
    auds = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
    if client_id not in auds or not all(isinstance(a, str) for a in auds):
        raise TokenInvalid("bad_aud")
    azp = c.get("azp")
    if (len(auds) > 1 and azp != client_id) or (azp is not None and azp != client_id):
        raise TokenInvalid("bad_azp")
    exp = _num(c.get("exp"))
    if exp is None or exp <= now - leeway:
        raise TokenInvalid("expired")
    nbf = c.get("nbf")
    if nbf is not None and (_num(nbf) is None or _num(nbf) > now + leeway):  # type: ignore[operator]
        raise TokenInvalid("not_yet_valid")
    iat = _num(c.get("iat"))
    if iat is None or iat > now + leeway or iat < now - max_iat_age:
        raise TokenInvalid("bad_iat")
    got = c.get("nonce")
    if not isinstance(got, str) or not nonce or not hmac.compare_digest(got.encode(), nonce.encode()):
        raise TokenInvalid("bad_nonce")
    sub = c.get("sub")
    if not isinstance(sub, str) or not sub or len(sub) > 255:
        raise TokenInvalid("bad_sub")
    return c


__all__ = ["ALGS", "LEEWAY_S", "MAX_IAT_AGE_S", "TokenInvalid", "verify_id_token"]
