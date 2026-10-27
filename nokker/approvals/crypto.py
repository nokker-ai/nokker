"""Primitives cryptographiques du chantier Approbations fortes (docs/APPROVALS.md §3) — P1, 18.10.2026.

Tout ici est sans état (pas d'accès au store) : signature ECDSA P-256/SHA-256 au format IEEE P1363
(WebCrypto), JSON canonique + empreinte des arguments, TOTP (RFC 6238, fenêtre configurable), et
validation `initData` de la Mini App Telegram (HMAC-SHA256, clé dérivée « WebAppData »).

Aucun import de `nokker.core`/`nokker.approvals.*` ici : ce module est une boîte à outils pure, testable
sans fakes ni backend.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from typing import Any
from urllib.parse import unquote_plus

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

# ─────────────────────────────── JSON canonique / empreintes ───────────────────────────────


def canonical_json(obj: Any) -> str:
    """Clés triées, sans espaces, UTF-8 (docs/APPROVALS.md §2) — déterministe entre deux appels avec
    le même objet Python (listes : ordre conservé, c'est au contrat amont de ne pas en dépendre)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def args_sha256(arguments: dict[str, Any]) -> str:
    """Empreinte des arguments d'un appel d'outil — JSON canonique de `arguments` SEUL (pas le nom de
    l'outil : l'`Approval` qui porte cette empreinte porte aussi `tool_name` séparément, cf.
    `core/ports.py::Approval`)."""
    return sha256_hex(canonical_json(arguments))


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


# ─────────────────────────────── base64 ───────────────────────────────


def b64url_decode(s: str) -> bytes:
    s = s.strip()
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def b64_decode(s: str) -> bytes:
    s = s.strip()
    pad = "=" * (-len(s) % 4)
    return base64.b64decode(s + pad)


# ─────────────────────────────── ECDSA P-256 / IEEE P1363 ───────────────────────────────


class SignatureError(ValueError):
    """Signature absente, malformée, ou qui ne correspond pas à la clé publique/au message."""


def load_p256_public_key_spki(public_key_spki: str):
    """`public_key_spki` : clé publique SPKI (SubjectPublicKeyInfo DER). Encodage accepté : base64url
    SANS padding — c'est ce que produit `exportPublicKeySpkiBase64Url` côté Mini App Telegram
    (`nokker/api/miniapp/approve-core.js`, branche `p1/miniapp`) — avec repli sur le base64 standard
    pour un client qui encoderait autrement (jamais un enrôlement bloqué sur un détail d'encodage)."""
    der = None
    for decoder in (b64url_decode, b64_decode):
        try:
            der = decoder(public_key_spki)
            break
        except Exception:  # noqa: BLE001 — on tente l'autre décodage avant d'abandonner
            continue
    if der is None:
        raise SignatureError("clé publique SPKI : ni base64url ni base64 standard ne décodent la valeur")
    try:
        key = serialization.load_der_public_key(der)
    except Exception as e:  # noqa: BLE001 — toute clé malformée est une erreur de preuve, pas un bug
        raise SignatureError(f"clé publique SPKI invalide : {e}") from e
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise SignatureError("clé publique attendue : EC P-256 (secp256r1)")
    return key


def verify_p1363_signature(public_key_spki: str, message: bytes, signature_b64url: str) -> None:
    """Lève `SignatureError` si la signature ne vérifie pas. `signature_b64url` : ECDSA P-256/SHA-256,
    base64url, format IEEE P1363 (WebCrypto — r || s, 64 octets bruts, PAS un DER ASN.1)."""
    key = load_p256_public_key_spki(public_key_spki)
    try:
        raw = b64url_decode(signature_b64url)
    except Exception as e:  # noqa: BLE001
        raise SignatureError(f"signature base64url invalide : {e}") from e
    if len(raw) != 64:
        raise SignatureError(f"signature IEEE P1363 attendue sur 64 octets, reçu {len(raw)}")
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:], "big")
    der_sig = encode_dss_signature(r, s)
    try:
        key.verify(der_sig, message, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as e:
        raise SignatureError("signature ECDSA invalide pour ce message/cette clé") from e


# ─────────────────────────────── TOTP (RFC 6238) ───────────────────────────────


def totp_code(secret_b32: str, for_time: float | None = None, period: int = 30, digits: int = 6) -> str:
    secret_b32 = secret_b32.strip().upper()
    key = base64.b32decode(secret_b32 + "=" * ((8 - len(secret_b32) % 8) % 8))
    step = int((for_time if for_time is not None else time.time()) // period)
    msg = struct.pack(">Q", step)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code_int).zfill(digits)


def totp_matching_step(secret_b32: str, code: str, for_time: float | None = None, window: int = 1,
                       period: int = 30) -> int | None:
    """Renvoie le NUMÉRO DE PAS (step) qui correspond au code, ou `None`. L'appelant (le backend
    `Approvals`) doit vérifier que ce step n'a pas déjà été consommé par un appel précédent — c'est ce
    qui rend un code TOTP non réutilisable (RFC 6238 ne l'impose pas lui-même)."""
    code = code.strip()
    if not code:
        return None
    t = for_time if for_time is not None else time.time()
    base_step = int(t // period)
    for w in range(-window, window + 1):
        candidate = totp_code(secret_b32, (base_step + w) * period, period=period)
        if hmac.compare_digest(candidate, code):
            return base_step + w
    return None


def new_totp_secret(length: int = 20) -> str:
    """Secret TOTP aléatoire, encodé base32 (format attendu par les apps TOTP standard)."""
    return base64.b32encode(secrets.token_bytes(length)).decode("ascii").rstrip("=")


# ─────────────────────────────── Telegram initData (Mini App) ───────────────────────────────


class TelegramInitDataError(ValueError):
    pass


def verify_telegram_init_data(init_data: str, bot_token: str, max_age_s: int = 600) -> dict[str, Any]:
    """HMAC-SHA256, clé dérivée `HMAC_SHA256(key="WebAppData", data=bot_token)` (docs/APPROVALS.md §3).
    Lève `TelegramInitDataError` si la signature ou `auth_date` ne tiennent pas ; renvoie
    `{"auth_date": int, "user": dict, "raw": dict}` sinon."""
    if not init_data:
        raise TelegramInitDataError("initData vide")
    data: dict[str, str] = {}
    for part in init_data.split("&"):
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        data[k] = unquote_plus(v)
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise TelegramInitDataError("initData sans champ hash")
    check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        raise TelegramInitDataError("signature initData invalide")
    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError as e:
        raise TelegramInitDataError("auth_date invalide") from e
    if time.time() - auth_date > max_age_s:
        raise TelegramInitDataError(f"initData expirée (> {max_age_s}s)")
    user: dict[str, Any] = {}
    if "user" in data:
        try:
            user = json.loads(data["user"])
        except json.JSONDecodeError as e:
            raise TelegramInitDataError("champ user invalide") from e
    return {"auth_date": auth_date, "user": user, "raw": data}


# ─────────────────────────────── WebAuthn (web, passkey) ───────────────────────────────
#
# ADR `nokker-apps/docs/adr/0001-webauthn-vs-ecdsa-contrat.md` (18.10.2026) — variante retenue côté
# client : le navigateur ne peut pas signer `challenge + "." + decision` littéralement (WebAuthn impose
# sa propre forme), donc le `challenge` WebAuthn passé à `navigator.credentials.get` EST
# `SHA-256(challenge_contrat + "." + decision)` (32 octets bruts) — la décision reste cryptographiquement
# liée à la signature, juste transportée différemment. La signature rendue par WebAuthn est déjà en
# DER (contrairement à WebCrypto brut, qui est en IEEE P1363) : `cryptography` la vérifie telle quelle,
# AUCUNE conversion ici (la conversion DER->P1363 mentionnée dans l'ADR est un détail du *mock* client,
# pas du contrat serveur).


class WebAuthnError(ValueError):
    """`clientDataJSON`/`authenticatorData`/signature WebAuthn invalides, ou absence du flag requis."""


def sha256_bytes(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def verify_webauthn_assertion(public_key_spki: str, client_data_json_b64url: str, authenticator_data_b64url: str,
                              signature_der_b64url: str, *, expected_challenge: bytes, expected_origin: str,
                              expected_rp_id: str, expected_type: str = "webauthn.get",
                              require_uv: bool = False) -> int:
    """Lève `WebAuthnError` si l'assertion ne vérifie pas ; rend le `signCount` de l'authentificateur (octets 33..36
    de `authenticatorData`, big-endian) que l'appelant compare au dernier vu pour cet appareil. `expected_challenge` : 32 octets attendus
    EXACTEMENT dans `clientDataJSON.challenge` une fois décodé (ADR : `sha256(challenge + "." +
    decision)` pour une décision, `sha256(challenge)` pour une connexion — calculé par l'appelant,
    jamais ici). `require_uv` : `True` pour une approbation `strong` (docs/APPROVALS.md §1).
    `expected_rp_id` (obligatoire, aucun chemin ne vérifie « parfois ») : `rpIdHash` (32 premiers octets de
    `authenticatorData`) doit valoir `sha256(expected_rp_id)`."""
    try:
        client_data_json = b64url_decode(client_data_json_b64url)
        authenticator_data = b64url_decode(authenticator_data_b64url)
        signature_der = b64url_decode(signature_der_b64url)
    except Exception as e:  # noqa: BLE001
        raise WebAuthnError(f"décodage base64url échoué : {e}") from e

    try:
        client_data = json.loads(client_data_json)
    except json.JSONDecodeError as e:
        raise WebAuthnError(f"clientDataJSON illisible : {e}") from e

    if client_data.get("type") != expected_type:
        raise WebAuthnError(f"type clientDataJSON inattendu : {client_data.get('type')!r}")
    if client_data.get("origin") != expected_origin:
        raise WebAuthnError(f"origine clientDataJSON inattendue : {client_data.get('origin')!r}")
    try:
        challenge_bytes = b64url_decode(str(client_data.get("challenge", "")))
    except Exception as e:  # noqa: BLE001
        raise WebAuthnError(f"challenge clientDataJSON illisible : {e}") from e
    if not hmac.compare_digest(challenge_bytes, expected_challenge):
        raise WebAuthnError("challenge clientDataJSON ne correspond pas à l'empreinte attendue")

    if len(authenticator_data) < 37:
        raise WebAuthnError("authenticatorData trop court (< 37 octets)")
    if not expected_rp_id or not hmac.compare_digest(
            authenticator_data[:32], hashlib.sha256(expected_rp_id.encode("utf-8")).digest()):
        raise WebAuthnError(f"rpIdHash de authenticatorData ne correspond pas à {expected_rp_id!r}")
    flags = authenticator_data[32]
    user_present = bool(flags & 0x01)
    user_verified = bool(flags & 0x04)
    if not user_present:
        raise WebAuthnError("flag UP (user present) absent de authenticatorData")
    if require_uv and not user_verified:
        raise WebAuthnError("flag UV (user verified) requis mais absent — niveau « strong »")

    message = authenticator_data + sha256_bytes(client_data_json)
    key = load_p256_public_key_spki(public_key_spki)
    try:
        key.verify(signature_der, message, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as e:
        raise WebAuthnError("signature WebAuthn invalide pour ce message/cette clé") from e
    except ValueError as e:  # DER malformé — cryptography lève ValueError, pas InvalidSignature
        raise WebAuthnError(f"signature WebAuthn malformée : {e}") from e
    return int.from_bytes(authenticator_data[33:37], "big")


__all__ = [
    "SignatureError", "TelegramInitDataError", "WebAuthnError", "args_sha256", "b64_decode", "b64url_decode",
    "canonical_json", "load_p256_public_key_spki", "new_nonce", "new_totp_secret", "sha256_bytes", "sha256_hex",
    "totp_code", "totp_matching_step", "verify_p1363_signature", "verify_telegram_init_data",
    "verify_webauthn_assertion",
]
