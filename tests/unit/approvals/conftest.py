"""Helpers partagés — génère une paire de clés ECDSA P-256 et signe au format IEEE P1363 (WebCrypto),
exactement comme le ferait un appareil enrôlé (Mini App Telegram, passkey, app native)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import quote_plus

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


def gen_keypair() -> tuple[ec.EllipticCurvePrivateKey, str]:
    """Clé publique encodée en base64url SANS padding — même forme que
    `exportPublicKeySpkiBase64Url` côté Mini App (`nokker/api/miniapp/approve-core.js`, branche
    `p1/miniapp`), que `load_p256_public_key_spki` accepte en priorité (avec repli base64 standard)."""
    priv = ec.generate_private_key(ec.SECP256R1())
    spki_der = priv.public_key().public_bytes(serialization.Encoding.DER,
                                              serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, base64.urlsafe_b64encode(spki_der).decode("ascii").rstrip("=")


def sign_p1363(priv: ec.EllipticCurvePrivateKey, message: bytes) -> str:
    der_sig = priv.sign(message, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@pytest.fixture
def device_keypair():
    return gen_keypair()


def telegram_init_data(bot_token: str, user_id: int, auth_date: int | None = None) -> str:
    """Construit un `initData` Mini App Telegram valide (même format HMAC-SHA256 que la vraie Mini App,
    docs/APPROVALS.md §3) — sert à simuler la preuve « signature+telegram » exigée au niveau `strong`
    (`nokker/approvals/base.py::_verify_proof`). `auth_date` par défaut = maintenant."""
    if auth_date is None:
        auth_date = int(time.time())
    user_json = json.dumps({"id": user_id, "first_name": "Alice"}, separators=(",", ":"))
    fields = {"auth_date": str(auth_date), "query_id": "abc", "user": user_json}
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    parts = [f"{k}={quote_plus(v)}" for k, v in fields.items()]
    parts.append(f"hash={digest}")
    return "&".join(parts)
