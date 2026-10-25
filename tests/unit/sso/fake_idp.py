"""Faux fournisseur OIDC, en mémoire : clés générées ici, découverte, JWKS et point de jeton servis par un faux client
HTTP. Aucun réseau. Les jetons sont signés comme le ferait un vrai fournisseur (RS256 / ES256, JWS compact)."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

ISSUER = "https://idp.example.com"
CLIENT_ID = "nokker-client"
CLIENT_SECRET = "s3cr3t-value-never-shown"

_KEYS: dict[str, Any] = {}


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _int(n: int) -> str:
    return b64u(n.to_bytes((n.bit_length() + 7) // 8, "big"))


def keys() -> dict[str, Any]:
    """Clés du module (générées une fois : RSA 2048 est lent)."""
    if not _KEYS:
        _KEYS["rsa"] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _KEYS["rsa2"] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _KEYS["ec"] = ec.generate_private_key(ec.SECP256R1())
    return _KEYS


def rsa_jwk(key: Any, kid: str) -> dict[str, Any]:
    n = key.public_key().public_numbers()
    return {"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256", "n": _int(n.n), "e": _int(n.e)}


def ec_jwk(key: Any, kid: str) -> dict[str, Any]:
    n = key.public_key().public_numbers()
    return {"kty": "EC", "kid": kid, "use": "sig", "alg": "ES256", "crv": "P-256",
            "x": b64u(n.x.to_bytes(32, "big")), "y": b64u(n.y.to_bytes(32, "big"))}


def sign(claims: dict[str, Any], *, alg: str = "RS256", kid: str | None = "r1", key: Any = None,
         header: dict[str, Any] | None = None) -> str:
    h: dict[str, Any] = {"alg": alg, "typ": "JWT"}
    if kid is not None:
        h["kid"] = kid
    h.update(header or {})
    signing = f"{b64u(json.dumps(h).encode())}.{b64u(json.dumps(claims).encode())}".encode()
    if alg == "none":
        return signing.decode() + "."
    if alg == "RS256":
        sig = (key or keys()["rsa"]).sign(signing, padding.PKCS1v15(), hashes.SHA256())
    elif alg == "ES256":
        r, s = decode_dss_signature((key or keys()["ec"]).sign(signing, ec.ECDSA(hashes.SHA256())))
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    elif alg == "HS256":
        import hmac

        sig = hmac.new(b"secret", signing, hashlib.sha256).digest()
    else:
        raise ValueError(alg)
    return f"{signing.decode()}.{b64u(sig)}"


def claims(nonce: str = "n-1", **over: Any) -> dict[str, Any]:
    now = int(over.pop("now", time.time()))
    c = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "u-1001", "email": "alice@example.com", "email_verified": True,
         "iat": now, "exp": now + 300, "nonce": nonce, "groups": ["eng"]}
    c.update(over)
    return {k: v for k, v in c.items() if v is not ...}


class FakeIdp:
    """Un fournisseur : `http` est le faux client (get / post_form) que le service reçoit en injection."""

    def __init__(self, issuer: str = ISSUER) -> None:
        self.issuer = issuer
        self.jwks_keys = [rsa_jwk(keys()["rsa"], "r1"), ec_jwk(keys()["ec"], "e1")]
        self.gets: list[str] = []
        self.token_posts: list[dict[str, Any]] = []
        self.next_claims: dict[str, Any] = {}  # surcharges du prochain jeton
        self.next_token: str | None = None  # jeton imposé tel quel (alg none, signature fausse…)
        self.sign_alg, self.sign_kid, self.sign_key = "RS256", "r1", None
        self.discovery_over: dict[str, Any] = {}
        self.codes: dict[str, dict[str, str]] = {}  # code → {nonce, challenge}
        self.http = self

    # ── côté navigateur : l'utilisateur s'est authentifié, le fournisseur rend un code ──
    def authorize(self, authorize_url: str, code: str = "code-1") -> dict[str, str]:
        q = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        self.codes[code] = {"nonce": q["nonce"], "challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"]}
        return {"code": code, "state": q["state"], **q}

    # ── faux client HTTP ──
    def get(self, url: str) -> tuple[int, bytes]:
        self.gets.append(url)
        if url == self.issuer.rstrip("/") + "/.well-known/openid-configuration":
            d = {"issuer": self.issuer, "authorization_endpoint": self.issuer + "/authorize",
                 "token_endpoint": self.issuer + "/token", "jwks_uri": self.issuer + "/jwks",
                 "id_token_signing_alg_values_supported": ["RS256", "ES256"]}
            d.update(self.discovery_over)
            return 200, json.dumps(d).encode()
        if url == self.issuer + "/jwks":
            return 200, json.dumps({"keys": self.jwks_keys}).encode()
        return 404, b"{}"

    def post_form(self, url: str, data: dict[str, str], headers: dict[str, str]) -> tuple[int, bytes]:
        self.token_posts.append({"url": url, "data": dict(data), "headers": dict(headers)})
        if url != self.issuer + "/token":
            return 404, b"{}"
        auth = headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if auth != expected and data.get("client_secret") != CLIENT_SECRET:
            return 401, b'{"error": "invalid_client"}'
        grant = self.codes.pop(data.get("code", ""), None)
        if grant is None:
            return 400, b'{"error": "invalid_grant"}'
        verifier = data.get("code_verifier", "")
        if b64u(hashlib.sha256(verifier.encode()).digest()) != grant["challenge"]:
            return 400, b'{"error": "invalid_grant", "error_description": "pkce"}'
        if data.get("redirect_uri") != grant["redirect_uri"]:
            return 400, b'{"error": "invalid_grant"}'
        token = self.next_token or sign(claims(grant["nonce"], **self.next_claims), alg=self.sign_alg,
                                        kid=self.sign_kid, key=self.sign_key)
        return 200, json.dumps({"access_token": "at-xyz", "token_type": "Bearer", "id_token": token}).encode()
