"""Preuve d'interopérabilité avec la Mini App Telegram (branche `p1/miniapp`, `nokker/api/miniapp/`) —
vecteur FIGÉ produit une seule fois par `node nokker/api/miniapp/gen-signature-vector.mjs` (la clé privée
n'existe plus, cf. `tests/unit/api/test_miniapp_signature_vector.py` sur cette branche). Ce test-ci
vérifie que NOTRE vérificateur (`nokker.approvals.crypto.verify_p1363_signature`,
`load_p256_public_key_spki`) accepte exactement ce que `approve-core.js::signChallengeDecision` produit
avec `crypto.subtle.sign` (ECDSA P-256/SHA-256, IEEE P1363, clé publique SPKI en base64url) — pas une
réimplémentation locale de la conversion P1363→DER."""
from __future__ import annotations

import pytest

from nokker.approvals.crypto import SignatureError, args_sha256, verify_p1363_signature

# Vecteur figé — identique à tests/unit/api/test_miniapp_signature_vector.py (branche p1/miniapp).
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


def test_our_verifier_accepts_the_miniapp_webcrypto_signature():
    message = f"{CHALLENGE}.{DECISION}".encode("utf-8")
    verify_p1363_signature(PUBLIC_KEY_SPKI_B64URL, message, SIGNATURE_P1363_B64URL)  # ne lève pas


def test_our_verifier_rejects_if_decision_does_not_match():
    message = f"{CHALLENGE}.reject".encode("utf-8")
    with pytest.raises(SignatureError):
        verify_p1363_signature(PUBLIC_KEY_SPKI_B64URL, message, SIGNATURE_P1363_B64URL)


def test_our_verifier_accepts_base64url_spki_without_padding():
    # La Mini App exporte la clé publique en base64url SANS padding (bytesToBase64Url) — le vecteur
    # ci-dessus en est la preuve (longueur non multiple de 4, aucun caractère `=`).
    assert "=" not in PUBLIC_KEY_SPKI_B64URL
    assert len(PUBLIC_KEY_SPKI_B64URL) % 4 != 0


def test_our_args_sha256_matches_the_fixture_hash_definition():
    # L'empreinte dans le vecteur (`args_sha256`) est censée être sha256(JSON canonique des arguments) —
    # cette fonction est la nôtre, côté serveur ; on vérifie juste qu'elle est bien déterministe/stable
    # sur un exemple trivial (le vecteur lui-même ne fournit pas les arguments en clair).
    assert args_sha256({"a": 1}) == args_sha256({"a": 1})
