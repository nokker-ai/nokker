"""ACCORD — octets canoniques et vérifications PURES du protocole (docs/ACCORD-DEVICE.md v1.1).

Aucun état, aucune entrée/sortie : le hub (`nokker/accord/hub.py`) tient les nonces, les fenêtres et les débits ; le
simulateur d'appareil et les vecteurs de test (`docs/accord-vectors.json`) utilisent les MÊMES fonctions."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import unicodedata
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

VERSION = 1
SEP = b"\x1f"
HELLO_DOMAIN = "accord-v1/hello"
DECISION_DOMAIN = "accord-v1/decision"
LEVELS = ("ordinary", "strong")
DECISIONS = ("approve", "refuse")
TITLE_MAX = 60
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DECIMAL = re.compile(r"^(0|[1-9][0-9]*)$")
_B64 = re.compile(r"^[A-Za-z0-9_-]+$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class BadCard(ValueError):
    """Champ hors contrat (non NFC, caractère de contrôle, titre trop long, niveau inconnu) : `error bad_card`."""


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(text: str) -> bytes:
    if not isinstance(text, str) or not _B64.fullmatch(text):
        raise ValueError("base64url attendu")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def clean_field(value: Any) -> str:
    """Un champ du protocole : texte NFC, sans caractère de contrôle (U+0000–U+001F, U+007F). Lève `BadCard`."""
    if not isinstance(value, str):
        raise BadCard("champ non texte")
    if unicodedata.normalize("NFC", value) != value:
        raise BadCard("champ non normalisé NFC")
    if _CONTROL.search(value):
        raise BadCard("caractère de contrôle dans un champ")
    return value


def decimal(n: Any) -> str:
    """Secondes Unix en décimal, sans zéro devant (entier ≥ 0)."""
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise BadCard("entier positif attendu")
    return str(n)


def check_card(card: dict[str, Any]) -> None:
    """Carte prête à l'envoi (hôte) ou reçue (appareil) : lève `BadCard` si un champ sort du contrat."""
    for k in ("card_id", "title", "level", "nonce"):
        clean_field(card.get(k))
    if len(card["title"]) > TITLE_MAX or not card["title"]:
        raise BadCard("titre vide ou de plus de 60 caractères")
    if card["level"] not in LEVELS:
        raise BadCard("niveau inconnu")
    decimal(card.get("expire_at"))
    if len(unb64u(card["nonce"])) != 16:
        raise BadCard("nonce de 16 octets attendu")


def shown(title: str, level: str, expire_at: int) -> str:
    """sha256(title ‖ 0x1F ‖ level ‖ 0x1F ‖ expire_at décimal) en hex : ce que l'écran a MONTRÉ."""
    data = SEP.join([clean_field(title).encode(), clean_field(level).encode(), decimal(expire_at).encode()])
    return hashlib.sha256(data).hexdigest()


def decision_bytes(card_id: str, decision: str, nonce: str, shown_hex: str, ts: int) -> bytes:
    if decision not in DECISIONS:
        raise ValueError("décision inconnue")
    parts = [DECISION_DOMAIN, clean_field(card_id), decision, clean_field(nonce), clean_field(shown_hex), decimal(ts)]
    return SEP.join(p.encode() for p in parts)


def hello_bytes(nonce: str, host_fp: str) -> bytes:
    return SEP.join(p.encode() for p in (HELLO_DOMAIN, clean_field(nonce), clean_field(host_fp)))


def pair_mac(code: str, pubkey: str, host_fp: str) -> str:
    """HMAC-SHA256(clé = code à 6 chiffres en ASCII, message = pubkey ‖ 0x1F ‖ host_fp), base64url."""
    if not re.fullmatch(r"[0-9]{6}", code or ""):
        raise ValueError("code à 6 chiffres attendu")
    msg = SEP.join([clean_field(pubkey).encode(), clean_field(host_fp).encode()])
    return b64u(hmac.new(code.encode("ascii"), msg, hashlib.sha256).digest())


def mac_matches(code: str, pubkey: str, host_fp: str, mac: str) -> bool:
    try:
        return hmac.compare_digest(pair_mac(code, pubkey, host_fp), mac or "")
    except (ValueError, BadCard):
        return False


def cert_fingerprint(der: bytes) -> str:
    """`host_fp` : SHA-256 du certificat TLS (DER), hex minuscule."""
    return hashlib.sha256(der).hexdigest()


def key8(pubkey: str) -> str:
    """8 premiers hex de SHA-256(clé publique brute de l'appareil), affichés des deux côtés à l'appairage."""
    return hashlib.sha256(unb64u(pubkey)).hexdigest()[:8]


def public_key_of(pubkey: str) -> Ed25519PublicKey:
    raw = unb64u(pubkey)
    if len(raw) != 32:
        raise ValueError("clé Ed25519 de 32 octets attendue")
    return Ed25519PublicKey.from_public_bytes(raw)


def verify(pubkey: str, data: bytes, sig: str) -> bool:
    try:
        public_key_of(pubkey).verify(unb64u(sig), data)
        return True
    except (InvalidSignature, ValueError):
        return False


def sign(key: Ed25519PrivateKey, data: bytes) -> str:
    return b64u(key.sign(data))


def raw_public(key: Ed25519PrivateKey) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return b64u(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def is_host_fp(text: Any) -> bool:
    return isinstance(text, str) and bool(_HEX64.fullmatch(text))
