"""`nokker/approvals/crypto.py` — primitives sans état, testées en isolation (docs/APPROVALS.md §3)."""
from __future__ import annotations

import base64
import time

import pytest

from nokker.approvals import crypto
from tests.unit.approvals.conftest import gen_keypair, sign_p1363, telegram_init_data


def test_canonical_json_sorts_keys_and_has_no_spaces():
    text = crypto.canonical_json({"b": 1, "a": 2})
    assert text == '{"a":2,"b":1}'


def test_args_sha256_is_deterministic_regardless_of_insertion_order():
    assert crypto.args_sha256({"b": 1, "a": 2}) == crypto.args_sha256({"a": 2, "b": 1})


def test_args_sha256_changes_if_a_value_changes():
    assert crypto.args_sha256({"a": 1}) != crypto.args_sha256({"a": 2})


def test_p1363_signature_round_trip_verifies():
    priv, spki_b64 = gen_keypair()
    message = b"challenge.approve"
    sig = sign_p1363(priv, message)
    crypto.verify_p1363_signature(spki_b64, message, sig)  # ne lève pas


def test_p1363_signature_wrong_key_is_rejected():
    priv, _ = gen_keypair()
    _, other_spki = gen_keypair()
    sig = sign_p1363(priv, b"challenge.approve")
    with pytest.raises(crypto.SignatureError):
        crypto.verify_p1363_signature(other_spki, b"challenge.approve", sig)


def test_p1363_signature_wrong_message_is_rejected():
    priv, spki_b64 = gen_keypair()
    sig = sign_p1363(priv, b"challenge.approve")
    with pytest.raises(crypto.SignatureError):
        crypto.verify_p1363_signature(spki_b64, b"challenge.reject", sig)


def test_p1363_signature_malformed_base64_is_rejected():
    _, spki_b64 = gen_keypair()
    with pytest.raises(crypto.SignatureError):
        crypto.verify_p1363_signature(spki_b64, b"x", "pas-une-signature-valide-!!")


def test_p1363_signature_der_instead_of_raw_is_rejected():
    """Une signature DER (ASN.1 SEQUENCE, ce que produit `cryptography` par défaut) n'a PAS la bonne
    longueur en IEEE P1363 (64 octets bruts attendus) — doit être rejetée, pas acceptée par accident."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec as ec_mod

    priv, spki_b64 = gen_keypair()
    der_sig = priv.sign(b"m", ec_mod.ECDSA(hashes.SHA256()))
    b64url = base64.urlsafe_b64encode(der_sig).decode().rstrip("=")
    with pytest.raises(crypto.SignatureError):
        crypto.verify_p1363_signature(spki_b64, b"m", b64url)


def test_totp_code_is_stable_within_a_period_and_verifiable():
    secret = crypto.new_totp_secret()
    now = 1_700_000_000.0
    code = crypto.totp_code(secret, for_time=now)
    assert crypto.totp_matching_step(secret, code, for_time=now) is not None


def test_totp_wrong_code_does_not_match():
    secret = crypto.new_totp_secret()
    now = 1_700_000_000.0
    real = crypto.totp_code(secret, for_time=now)
    wrong = "000000" if real != "000000" else "111111"
    assert crypto.totp_matching_step(secret, wrong, for_time=now, window=0) is None


def test_totp_window_accepts_adjacent_step_but_not_further():
    secret = crypto.new_totp_secret()
    now = 1_700_000_000.0
    code_next = crypto.totp_code(secret, for_time=now + 30)  # 1 pas plus tard
    assert crypto.totp_matching_step(secret, code_next, for_time=now, window=1) is not None
    code_far = crypto.totp_code(secret, for_time=now + 300)  # 10 pas plus tard
    assert crypto.totp_matching_step(secret, code_far, for_time=now, window=1) is None


def test_telegram_init_data_valid_round_trip():
    token = "123456:FAKE-BOT-TOKEN"
    init_data = telegram_init_data(token, user_id=1000000001, auth_date=int(time.time()))
    out = crypto.verify_telegram_init_data(init_data, token)
    assert out["user"]["id"] == 1000000001


def test_telegram_init_data_wrong_token_is_rejected():
    init_data = telegram_init_data("123456:FAKE-BOT-TOKEN", user_id=1, auth_date=int(time.time()))
    with pytest.raises(crypto.TelegramInitDataError):
        crypto.verify_telegram_init_data(init_data, "999999:AUTRE-TOKEN")


def test_telegram_init_data_expired_is_rejected():
    token = "123456:FAKE-BOT-TOKEN"
    init_data = telegram_init_data(token, user_id=1, auth_date=int(time.time()) - 3600)
    with pytest.raises(crypto.TelegramInitDataError):
        crypto.verify_telegram_init_data(init_data, token, max_age_s=600)
