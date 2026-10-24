"""WebAuthn/passkey (web) — ADR `nokker-apps/docs/adr/0001-webauthn-vs-ecdsa-contrat.md`. Fabrique un
`clientDataJSON`/`authenticatorData`/signature DER synthétiques (pas de vrai navigateur ici) selon la
forme exacte que `apps/web`/`packages/mock-server` produisent, et vérifie que notre serveur les accepte
— ou les rejette pour les bonnes raisons (mauvais type, mauvaise origine, UV absent, challenge forgé)."""
from __future__ import annotations

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from nokker.approvals.crypto import WebAuthnError, verify_webauthn_assertion
from nokker.approvals.errors import ProofInvalid
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.ports import Proof
from tests.unit.approvals.conftest import gen_keypair
from tests.unit.approvals.test_memory_approvals import enroll_approved_device, make_call, make_turn, make_verdict

ORIGIN = "https://approve.example.ch"
ALICE = "alice"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _authenticator_data(*, up: bool = True, uv: bool = True) -> bytes:
    rp_id_hash = hashlib.sha256(b"approve.example.ch").digest()
    flags = (0x01 if up else 0) | (0x04 if uv else 0)
    sign_count = (0).to_bytes(4, "big")
    return rp_id_hash + bytes([flags]) + sign_count


def _client_data_json(challenge_bytes: bytes, *, origin: str = ORIGIN, type_: str = "webauthn.get") -> bytes:
    return json.dumps({"type": type_, "challenge": _b64url(challenge_bytes), "origin": origin,
                       "crossOrigin": False}, separators=(",", ":")).encode("utf-8")


def _assertion(priv, challenge_bytes: bytes, *, up=True, uv=True, origin=ORIGIN, type_="webauthn.get"):
    authenticator_data = _authenticator_data(up=up, uv=uv)
    client_data_json = _client_data_json(challenge_bytes, origin=origin, type_=type_)
    message = authenticator_data + hashlib.sha256(client_data_json).digest()
    signature_der = priv.sign(message, ec.ECDSA(hashes.SHA256()))
    return {
        "client_data_json": _b64url(client_data_json),
        "authenticator_data": _b64url(authenticator_data),
        "signature": _b64url(signature_der),
    }


# ═══════════════════════════ crypto.verify_webauthn_assertion en isolation ═══════════════════════════


def test_valid_assertion_verifies():
    priv, spki = gen_keypair()
    challenge = hashlib.sha256(b"abc.approve").digest()
    a = _assertion(priv, challenge)
    verify_webauthn_assertion(spki, a["client_data_json"], a["authenticator_data"], a["signature"],
                              expected_challenge=challenge, expected_origin=ORIGIN)  # ne lève pas


def test_wrong_challenge_is_rejected():
    priv, spki = gen_keypair()
    challenge = hashlib.sha256(b"abc.approve").digest()
    a = _assertion(priv, challenge)
    other_challenge = hashlib.sha256(b"abc.reject").digest()
    with pytest.raises(WebAuthnError):
        verify_webauthn_assertion(spki, a["client_data_json"], a["authenticator_data"], a["signature"],
                                  expected_challenge=other_challenge, expected_origin=ORIGIN)


def test_wrong_origin_is_rejected():
    priv, spki = gen_keypair()
    challenge = hashlib.sha256(b"abc.approve").digest()
    a = _assertion(priv, challenge, origin="https://evil.example.com")
    with pytest.raises(WebAuthnError):
        verify_webauthn_assertion(spki, a["client_data_json"], a["authenticator_data"], a["signature"],
                                  expected_challenge=challenge, expected_origin=ORIGIN)


def test_wrong_type_is_rejected():
    priv, spki = gen_keypair()
    challenge = hashlib.sha256(b"abc.approve").digest()
    a = _assertion(priv, challenge, type_="webauthn.create")
    with pytest.raises(WebAuthnError):
        verify_webauthn_assertion(spki, a["client_data_json"], a["authenticator_data"], a["signature"],
                                  expected_challenge=challenge, expected_origin=ORIGIN)


def test_missing_user_present_flag_is_rejected():
    priv, spki = gen_keypair()
    challenge = hashlib.sha256(b"abc.approve").digest()
    a = _assertion(priv, challenge, up=False, uv=False)
    with pytest.raises(WebAuthnError):
        verify_webauthn_assertion(spki, a["client_data_json"], a["authenticator_data"], a["signature"],
                                  expected_challenge=challenge, expected_origin=ORIGIN)


def test_require_uv_rejects_assertion_without_user_verification():
    priv, spki = gen_keypair()
    challenge = hashlib.sha256(b"abc.approve").digest()
    a = _assertion(priv, challenge, up=True, uv=False)
    with pytest.raises(WebAuthnError):
        verify_webauthn_assertion(spki, a["client_data_json"], a["authenticator_data"], a["signature"],
                                  expected_challenge=challenge, expected_origin=ORIGIN, require_uv=True)
    verify_webauthn_assertion(spki, a["client_data_json"], a["authenticator_data"], a["signature"],
                              expected_challenge=challenge, expected_origin=ORIGIN, require_uv=False)


def test_tampered_signature_is_rejected():
    priv, spki = gen_keypair()
    challenge = hashlib.sha256(b"abc.approve").digest()
    a = _assertion(priv, challenge)
    bad_sig = a["signature"][:-4] + ("AAAA" if a["signature"][-4:] != "AAAA" else "BBBB")
    with pytest.raises(WebAuthnError):
        verify_webauthn_assertion(spki, a["client_data_json"], a["authenticator_data"], bad_sig,
                                  expected_challenge=challenge, expected_origin=ORIGIN)


# ═══════════════════════════ bout en bout : decide() avec une preuve WebAuthn ═══════════════════════════


def test_decide_with_webauthn_happy_path_for_strong_action():
    backend = InMemoryApprovals(webauthn_origin=ORIGIN)
    priv, device = enroll_approved_device(backend)
    approval = backend.request(make_turn(), make_call(), make_verdict("strong"), None)
    ch = backend.challenge(approval.id)

    challenge_bytes = hashlib.sha256(f"{ch.canonical()}.approve".encode("utf-8")).digest()
    a = _assertion(priv, challenge_bytes, uv=True)
    decided = backend.decide(approval.id, "approve", Proof(
        challenge=ch.canonical(), device_id=device.id, webauthn_client_data_json=a["client_data_json"],
        webauthn_authenticator_data=a["authenticator_data"], webauthn_signature=a["signature"]))
    assert decided.status == "approved" and decided.decided_via == "webauthn" and decided.decided_by == ALICE


def test_decide_with_webauthn_strong_action_without_uv_is_rejected():
    backend = InMemoryApprovals(webauthn_origin=ORIGIN)
    priv, device = enroll_approved_device(backend)
    approval = backend.request(make_turn(), make_call(), make_verdict("strong"), None)
    ch = backend.challenge(approval.id)

    challenge_bytes = hashlib.sha256(f"{ch.canonical()}.approve".encode("utf-8")).digest()
    a = _assertion(priv, challenge_bytes, uv=False)  # UP seul, pas de biométrie confirmée
    with pytest.raises(ProofInvalid):
        backend.decide(approval.id, "approve", Proof(
            challenge=ch.canonical(), device_id=device.id, webauthn_client_data_json=a["client_data_json"],
            webauthn_authenticator_data=a["authenticator_data"], webauthn_signature=a["signature"]))


def test_decide_with_webauthn_standard_action_without_uv_is_accepted():
    backend = InMemoryApprovals(webauthn_origin=ORIGIN)
    priv, device = enroll_approved_device(backend)
    approval = backend.request(make_turn(), make_call(), make_verdict("standard"), None)
    ch = backend.challenge(approval.id)

    challenge_bytes = hashlib.sha256(f"{ch.canonical()}.approve".encode("utf-8")).digest()
    a = _assertion(priv, challenge_bytes, uv=False)
    decided = backend.decide(approval.id, "approve", Proof(
        challenge=ch.canonical(), device_id=device.id, webauthn_client_data_json=a["client_data_json"],
        webauthn_authenticator_data=a["authenticator_data"], webauthn_signature=a["signature"]))
    assert decided.status == "approved"


def test_decide_with_webauthn_decision_is_bound_into_the_signed_challenge():
    """Signer pour « approve » ne doit pas marcher pour « reject » — la décision est DANS l'empreinte
    (ADR : évite qu'un refus intercepté soit rejoué comme une approbation)."""
    backend = InMemoryApprovals(webauthn_origin=ORIGIN)
    priv, device = enroll_approved_device(backend)
    approval = backend.request(make_turn(), make_call(), make_verdict("standard"), None)
    ch = backend.challenge(approval.id)

    challenge_bytes = hashlib.sha256(f"{ch.canonical()}.approve".encode("utf-8")).digest()
    a = _assertion(priv, challenge_bytes)
    with pytest.raises(ProofInvalid):
        backend.decide(approval.id, "reject", Proof(
            challenge=ch.canonical(), device_id=device.id, webauthn_client_data_json=a["client_data_json"],
            webauthn_authenticator_data=a["authenticator_data"], webauthn_signature=a["signature"]))


def test_decide_with_webauthn_without_configured_origin_is_rejected():
    backend = InMemoryApprovals()  # webauthn_origin=None par défaut
    priv, device = enroll_approved_device(backend)
    approval = backend.request(make_turn(), make_call(), make_verdict("standard"), None)
    ch = backend.challenge(approval.id)
    challenge_bytes = hashlib.sha256(f"{ch.canonical()}.approve".encode("utf-8")).digest()
    a = _assertion(priv, challenge_bytes)
    with pytest.raises(ProofInvalid):
        backend.decide(approval.id, "approve", Proof(
            challenge=ch.canonical(), device_id=device.id, webauthn_client_data_json=a["client_data_json"],
            webauthn_authenticator_data=a["authenticator_data"], webauthn_signature=a["signature"]))


# ═══════════════════════════ session WebAuthn (connexion, pas de décision) ═══════════════════════════


def test_create_session_with_webauthn_happy_path():
    backend = InMemoryApprovals(webauthn_origin=ORIGIN)
    priv, device = enroll_approved_device(backend)
    ch = backend.login_challenge(device.id)
    challenge_bytes = hashlib.sha256(ch.canonical().encode("utf-8")).digest()  # PAS de suffixe ".login" ici
    a = _assertion(priv, challenge_bytes)
    session = backend.create_session(device.id, ch.canonical(), webauthn=a)
    assert session.user == ALICE
