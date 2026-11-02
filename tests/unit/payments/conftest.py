"""Aides des tests de paiements : deux locataires (alice, bob), une passkey synthétique (WebAuthn, UV), le service
branché sur des approbations en mémoire. Aucun réseau, aucun modèle."""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from nokker.approvals.memory import InMemoryApprovals
from nokker.core.ports import Proof
from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
from nokker.payments.service import PAY_RULE, Payments, Person, Settings
from nokker.payments.store import MemoryPaymentStore
from nokker.tenancy.context import tenant_scope
from tests.unit.approvals.conftest import gen_keypair

ORIGIN = "https://app.example.ch"
RP_ID = "app.example.ch"
ALICE, BOB, CAROL, KID = "us_alice00000001", "us_bob000000002", "us_carol0000003", "us_kid00000004"
TENANT = {ALICE: "tn_alice", BOB: "tn_bob", CAROL: "tn_carol", KID: "tn_kid"}
CART = {"merchant": "Boutique Example", "amount": "129.90", "currency": "CHF",
        "items": [{"name": "Bouilloire", "quantity": 1, "unit_price": "129.90"}],
        "url": "https://shop.example.ch/checkout"}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def assertion(priv, challenge_text: str, decision: str = "approve", *, origin: str = ORIGIN, uv: bool = True,
              sign_count: int = 0) -> dict:
    """Assertion WebAuthn telle qu'un navigateur la rend pour `navigator.credentials.get` (docs/APPROVALS.md §3.4)."""
    expected = hashlib.sha256(f"{challenge_text}.{decision}".encode()).digest()
    auth_data = hashlib.sha256(RP_ID.encode()).digest() + bytes([0x01 | (0x04 if uv else 0)]) + sign_count.to_bytes(4,
                                                                                                                  "big")
    cdj = json.dumps({"type": "webauthn.get", "challenge": b64url(expected), "origin": origin, "crossOrigin": False},
                     separators=(",", ":")).encode()
    sig = priv.sign(auth_data + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
    return {"client_data_json": b64url(cdj), "authenticator_data": b64url(auth_data), "signature": b64url(sig)}


def webauthn_proof(priv, device_id: str, challenge_text: str, decision: str = "approve", **kw) -> Proof:
    a = assertion(priv, challenge_text, decision, **kw)
    return Proof(challenge=challenge_text, device_id=device_id, webauthn_client_data_json=a["client_data_json"],
                 webauthn_authenticator_data=a["authenticator_data"], webauthn_signature=a["signature"])


@dataclass
class FakePeople:
    """Foyer d'alice (payeuse) : bob (adulte), kid (mineur, représentant alice). Carol : seule, hors foyer."""

    payer_of: dict = field(default_factory=lambda: {ALICE: ALICE, BOB: ALICE, KID: ALICE, CAROL: CAROL})
    minors: set = field(default_factory=lambda: {KID})

    def person(self, account):
        return Person(account=account, payer=self.payer_of.get(account), minor=account in self.minors,
                      guardian=self.payer_of.get(account) if account in self.minors else None)

    def members_of(self, payer):
        return [self.person(a) for a, p in self.payer_of.items() if p == payer]

    def tenant_of(self, account):
        return TENANT.get(account)


def turn_of(account: str, thread: str = "t1") -> Turn:
    return Turn(thread_id=f"{TENANT[account]}:{thread}", index=0, input=InputCheck(text="achète"), channel="web",
                sender=account)


def pay_call(**over) -> ToolCall:
    return ToolCall(id="c-pay", name="pay_request", arguments={**CART, **over})


def to_pay_verdict() -> Verdict:
    return Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=PAY_RULE, reason="à payer",
                   strength="strong")


@dataclass
class World:
    backend: InMemoryApprovals
    svc: Payments
    keys: dict

    def device(self, account: str):
        return self.keys[account]

    def request(self, account: str, **over):
        with tenant_scope(TENANT[account]):
            return self.backend.request(turn_of(account), pay_call(**over), to_pay_verdict(), None)

    def approve(self, account: str, approval_id: str, *, signer: str | None = None):
        signer = signer or account
        priv, dev = self.keys[signer]
        with tenant_scope(TENANT[account]):
            ch = self.backend.challenge(approval_id).canonical()
            return self.backend.decide(approval_id, "approve", webauthn_proof(priv, dev.id, ch))


@pytest.fixture
def world() -> World:
    backend = InMemoryApprovals(webauthn_origin=ORIGIN, account_tenant=lambda u: TENANT.get(u))
    svc = Payments(store=MemoryPaymentStore(), people=FakePeople(),
                   settings=Settings.from_config({"default_budget": {"currency": "CHF", "per_purchase": 200,
                                                                     "per_day": 300, "per_month": 1000}}))
    backend.payment_mandates = svc
    keys = {}
    for acc in (ALICE, BOB, CAROL, KID):
        priv, spki = gen_keypair()
        dev = backend.enroll_device(user=acc, name="passkey", public_key_spki=spki)
        keys[acc] = (priv, backend.confirm_device_cli(dev.code))
    return World(backend=backend, svc=svc, keys=keys)
