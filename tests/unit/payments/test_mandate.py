"""Mandats signés par la passkey (docs/PAYMENTS.md §3) : signé puis vérifié, altéré rejeté, preuves refusées."""
from __future__ import annotations

import copy
import json

import pytest

from nokker.approvals.errors import ProofInvalid
from nokker.core.ports import Proof
from nokker.payments.mandate import AP2_VCT, mandate_sha256, verify_envelope
from nokker.tenancy.context import tenant_scope
from tests.unit.approvals.conftest import sign_p1363
from tests.unit.payments.conftest import ALICE, BOB, ORIGIN, TENANT, webauthn_proof


def _signed(world, account=ALICE):
    card = world.request(account)
    world.approve(account, card.id)
    with tenant_scope(TENANT[account]):
        rec = world.svc.store.by_approval(card.id)
    return card, rec


def test_card_builds_an_ap2_shaped_mandate_bound_to_the_challenge(world):
    card = world.request(ALICE)
    with tenant_scope(TENANT[ALICE]):
        rec = world.svc.store.by_approval(card.id)
        ch = json.loads(world.backend.challenge(card.id).canonical())
    m = rec.mandate
    assert rec.status == "pending" and m["cart_sha256"] == card.args_sha256
    from nokker.payments.mandate import principal_handle

    h = principal_handle(rec.id, ALICE)
    assert m["principal"] == {"account": h, "approver": h, "by_representative": False} and h.startswith("pr_")
    assert "tenant_id" not in m and ALICE not in json.dumps(m) and TENANT[ALICE] not in json.dumps(m)
    assert m["max_amount"] == {"amount": 12990, "currency": "CHF"} and m["merchant"]["id"] == "shop.example.ch"
    assert m["ap2"]["vct"] == AP2_VCT and m["ap2"]["payment_amount"] == {"amount": 12990, "currency": "CHF"}
    assert ch["mandate_sha256"] == rec.mandate_sha256 == mandate_sha256(m)


def test_mandate_signed_then_verified_offline(world):
    card, rec = _signed(world)
    assert rec.status == "signed"
    with tenant_scope(TENANT[BOB]):  # autre locataire : la carte n'existe pas
        assert world.backend.get(card.id) is None
    with tenant_scope(TENANT[ALICE]):
        assert world.backend.get(card.id).status == "approved"
        assert world.backend.get(card.id).decided_via == "webauthn"
    out = verify_envelope(rec.envelope, expected_origin=ORIGIN, trusted_spki=world.device(ALICE)[1].public_key_spki)
    assert out.valid and not out.expired and out.checks["device_key_trusted"], out.errors
    out2 = verify_envelope(json.loads(json.dumps(rec.envelope)))  # sérialisé puis relu : même verdict
    assert out2.valid and not out2.checks["device_key_trusted"]


@pytest.mark.parametrize("tamper", ["amount", "amount_and_hashes", "merchant", "challenge", "spki", "valid_until"])
def test_altered_mandate_is_rejected(world, tamper):
    _, rec = _signed(world)
    env = copy.deepcopy(rec.envelope)
    m = env["mandate"]
    if tamper == "amount":
        m["cart"]["amount"] = "1299.00"
    elif tamper == "amount_and_hashes":  # le faussaire recalcule toutes les empreintes : la signature tombe
        from nokker.payments.mandate import canonical_json, parse_cart, sha256_hex

        m["cart"]["amount"] = "1299.00"
        cart = parse_cart(m["cart"])
        m["cart_sha256"] = sha256_hex(canonical_json(m["cart"]))
        m["max_amount"] = m["ap2"]["payment_amount"] = {"amount": cart.amount_minor, "currency": "CHF"}
        for c in m["ap2"]["constraints"]:
            if c["type"] == "payment.amount_range":
                c["max"] = cart.amount_minor
            if c["type"] == "payment.reference":
                c["conditional_transaction_id"] = m["cart_sha256"]
        env["mandate_sha256"] = mandate_sha256(m)
        ch = json.loads(env["proof"]["challenge"])
        ch["args_sha256"], ch["mandate_sha256"] = m["cart_sha256"], env["mandate_sha256"]
        env["proof"]["challenge"] = json.dumps(ch, sort_keys=True, separators=(",", ":"))
    elif tamper == "merchant":
        m["merchant"]["name"] = "Autre marchand"
        env["mandate_sha256"] = mandate_sha256(m)
    elif tamper == "challenge":
        env["proof"]["challenge"] = env["proof"]["challenge"].replace('"aud"', '"aud" ')
    elif tamper == "spki":
        from tests.unit.approvals.conftest import gen_keypair

        env["proof"]["public_key_spki"] = gen_keypair()[1]
    elif tamper == "valid_until":
        m["valid_until"] = "2099-01-01T00:00:00Z"
        m["ap2"]["exp"] = 4070908800
    out = verify_envelope(env)
    assert not out.valid and out.errors


def test_expired_mandate_still_verifies_but_says_expired(world):
    _, rec = _signed(world)
    out = verify_envelope(rec.envelope, now=rec.mandate["ap2"]["exp"] + 1)
    assert out.valid and out.expired


def test_route_and_cli_verify(world, tmp_path, capsys):
    from nokker.cli import main
    from nokker.payments import http as payments_http

    _, rec = _signed(world)
    payments_http.configure(world.svc, device_key=lambda d: world.device(ALICE)[1].public_key_spki, origin=ORIGIN)
    try:
        with tenant_scope(TENANT[ALICE]):
            status, body = payments_http.dispatch(world.svc, "GET", ["mandates", rec.id, "verify"], ALICE, None,
                                                  dict)
            _, full = payments_http.dispatch(world.svc, "GET", ["mandates", rec.id], ALICE, None, dict)
            _, listing = payments_http.dispatch(world.svc, "GET", ["mandates"], ALICE, None, dict)
    finally:
        payments_http.configure(None)
    assert status == 200 and body["valid"] and body["checks"]["device_key_trusted"] and body["status"] == "signed"
    assert [x["mandate_id"] for x in listing["mandates"]] == [rec.id]
    good = tmp_path / "m.json"
    good.write_text(json.dumps(full), encoding="utf-8")  # réponse entière de la route : l'enveloppe est lue dedans
    assert main(["mandate", "verify", str(good), "--origin", ORIGIN]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    full["envelope"]["mandate"]["cart"]["amount"] = "1.00"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(full["envelope"]), encoding="utf-8")
    assert main(["mandate", "verify", str(bad)]) == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False


def test_device_signature_without_passkey_is_refused_for_a_payment_card(world):
    card = world.request(ALICE)
    priv, dev = world.device(ALICE)
    with tenant_scope(TENANT[ALICE]):
        ch = world.backend.challenge(card.id).canonical()
        sig = sign_p1363(priv, f"{ch}.approve".encode())
        with pytest.raises(ProofInvalid):  # clé logicielle (P1363) : pas une passkey, carte forte refusée
            world.backend.decide(card.id, "approve", Proof(challenge=ch, device_id=dev.id, signature=sig))
        assert world.backend.get(card.id).status == "pending"
        assert world.svc.store.by_approval(card.id).status == "pending"


def test_challenge_without_the_mandate_is_refused(world):
    card = world.request(ALICE)
    priv, dev = world.device(ALICE)
    with tenant_scope(TENANT[ALICE]):
        ch = json.loads(world.backend.challenge(card.id).canonical())
        ch.pop("mandate_sha256")
        text = json.dumps(ch, sort_keys=True, separators=(",", ":"))
        with pytest.raises(ProofInvalid, match="mandat"):
            world.backend.decide(card.id, "approve", webauthn_proof(priv, dev.id, text))
        assert world.svc.store.by_approval(card.id).status == "pending"


def test_local_token_attestation_and_other_signer_cannot_approve(world):
    card = world.request(ALICE)
    with tenant_scope(TENANT[ALICE]):
        with pytest.raises(ProofInvalid, match="passkey"):
            world.backend.decide_local(card.id, "approve", ALICE)
        ch = world.backend.challenge(card.id).canonical()
        with pytest.raises(ProofInvalid, match="passkey"):
            world.backend.decide_attested(card.id, "approve", ch, decided_by=ALICE, account=ALICE, device_id="d",
                                          jti="j")
        priv, dev = world.device(BOB)  # bob (autre locataire) signe la carte d'alice : refusé
        with pytest.raises(ProofInvalid):
            world.backend.decide(card.id, "approve", webauthn_proof(priv, dev.id,
                                                                     world.backend.challenge(card.id).canonical()))
        assert world.svc.store.by_approval(card.id).status == "pending"


def test_reject_marks_the_mandate_and_frees_the_budget(world):
    card = world.request(ALICE)
    priv, dev = world.device(ALICE)
    with tenant_scope(TENANT[ALICE]):
        ch = world.backend.challenge(card.id).canonical()
        world.backend.decide(card.id, "reject", webauthn_proof(priv, dev.id, ch, "reject"))
        rec = world.svc.store.by_approval(card.id)
        assert rec.status == "rejected" and rec.envelope is None
        assert world.svc.store.spent(ALICE, "CHF", 0, world.svc.clock()) == 0
