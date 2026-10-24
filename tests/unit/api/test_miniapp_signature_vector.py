"""Preuve croisée JS <-> Python du format de signature de la Mini App (docs/APPROVALS.md §3).

`nokker/api/miniapp/approve-core.js` signe `challenge + "." + decision` avec une clé ECDSA P-256 via
WebCrypto (`crypto.subtle.sign({name: "ECDSA", hash: "SHA-256"}, ...)`). WebCrypto produit nativement
une signature au format IEEE P1363 (concaténation brute r || s, 64 octets pour P-256) — PAS le format
DER qu'attend `cryptography.hazmat...EllipticCurvePublicKey.verify()`. Ce test ne réimplémente rien :
il prend un VECTEUR FIGÉ, produit une seule fois par `nokker/api/miniapp/gen-signature-vector.mjs`
(`node nokker/api/miniapp/gen-signature-vector.mjs`), convertit juste r||s -> DER (ce que le serveur
doit faire pour vérifier une décision signée par la Mini App) et vérifie avec `cryptography`.

Si ce test casse après une modification de approve-core.js, régénérer le vecteur avec le script et
reporter la nouvelle sortie ici — le vecteur est volontairement figé (ECDSA est randomisé : deux runs
du script ne produisent jamais la même signature bit à bit, mais la vérification doit toujours passer).

Aucun réseau, aucun secret : la clé privée du vecteur n'existe plus (le script ne la persiste pas).
"""
from __future__ import annotations

import base64

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

# --- Vecteur figé : sortie exacte de `node nokker/api/miniapp/gen-signature-vector.mjs` -------------
PUBLIC_KEY_SPKI_B64URL = (
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAESZuPV0LMT2VWsT5-bOudy13D_7pcCEA3CZX7pjd_"
    "P-aCy9kyaHKua3gw9e5Ptu8jYXeTBDAWehCLGjmmHzRqyQ"
)
CHALLENGE = (
    '{"approval_id":"appr_fixture_0001",'
    '"args_sha256":"3f786850e387550fdab836ed7e6dc881de23001b7bcc908ac8c5e2e1c4abd5b",'
    '"aud":"nokker-approval","exp":1760000000,"nonce":"fixture-nonce-abcdef"}'
)
DECISION = "approve"
SIGNATURE_P1363_B64URL = (
    "dYHnuKmfDn2eA0VYpwLoS1fF_ugk11l5iA1aQEsZXQa3A5d5kk_KkabziUJXXS0Dw10VxwwzxefHFT_JzsCzoA"
)


def _b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded)


def _load_public_key(spki_b64url: str) -> ec.EllipticCurvePublicKey:
    key = serialization.load_der_public_key(_b64url_decode(spki_b64url))
    assert isinstance(key, ec.EllipticCurvePublicKey)
    return key


def _p1363_to_der(signature: bytes) -> bytes:
    """r || s (WebCrypto) -> DER (ce que `cryptography` exige pour `.verify()`)."""
    assert len(signature) == 64, f"signature P1363 P-256 attendue à 64 octets, reçu {len(signature)}"
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    return encode_dss_signature(r, s)


def test_vector_public_key_is_p256() -> None:
    key = _load_public_key(PUBLIC_KEY_SPKI_B64URL)
    assert isinstance(key.curve, ec.SECP256R1)


def test_js_webcrypto_signature_is_p1363_64_bytes_not_der() -> None:
    sig = _b64url_decode(SIGNATURE_P1363_B64URL)
    assert len(sig) == 64
    assert sig[0] != 0x30  # une signature DER commencerait par une SEQUENCE ASN.1 (0x30)


def test_server_side_verification_of_js_produced_signature_succeeds() -> None:
    """Le coeur de la preuve : la signature produite par approve-core.js (WebCrypto) se vérifie,
    côté serveur, avec `cryptography`, une fois convertie P1363 -> DER — même message, même clé."""
    public_key = _load_public_key(PUBLIC_KEY_SPKI_B64URL)
    signature_der = _p1363_to_der(_b64url_decode(SIGNATURE_P1363_B64URL))
    message = f"{CHALLENGE}.{DECISION}".encode("utf-8")

    public_key.verify(signature_der, message, ec.ECDSA(hashes.SHA256()))  # ne lève pas -> signature valide


def test_decision_is_bound_into_the_signed_message() -> None:
    """La décision fait partie du message signé (contrat §3) : vérifier avec l'autre décision échoue."""
    public_key = _load_public_key(PUBLIC_KEY_SPKI_B64URL)
    signature_der = _p1363_to_der(_b64url_decode(SIGNATURE_P1363_B64URL))
    tampered_message = f"{CHALLENGE}.reject".encode("utf-8")

    with pytest.raises(InvalidSignature):
        public_key.verify(signature_der, tampered_message, ec.ECDSA(hashes.SHA256()))


def test_tampered_challenge_is_rejected() -> None:
    """Toute modification du défi (même d'un caractère) invalide la signature."""
    public_key = _load_public_key(PUBLIC_KEY_SPKI_B64URL)
    signature_der = _p1363_to_der(_b64url_decode(SIGNATURE_P1363_B64URL))
    tampered_challenge = CHALLENGE.replace("appr_fixture_0001", "appr_fixture_0002")
    tampered_message = f"{tampered_challenge}.{DECISION}".encode("utf-8")

    with pytest.raises(InvalidSignature):
        public_key.verify(signature_der, tampered_message, ec.ECDSA(hashes.SHA256()))
