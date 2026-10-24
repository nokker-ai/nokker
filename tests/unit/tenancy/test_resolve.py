"""Frontière multi-locataire (docs/TENANCY.md §2, §5) : résolution expéditeur → locataire, GO par locataire,
décideur d'une carte d'approbation par locataire. Fonctions et backends en mémoire, sans HTTP."""
from __future__ import annotations

import dataclasses
import json

import pytest

from nokker.approvals.errors import ApprovalNotFound, ProofInvalid
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.ports import Proof, ToolSpec
from nokker.core.turn import Decision, Effect, Gate, InputCheck, ToolCall, Turn, Verdict
from nokker.qa.invariants import DeterministicInvariants
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.tenancy.resolve import UnknownSender, account_tenant_fn, resolve_tenant
from nokker.tenancy.store import MemoryTenancy
from tests.unit.approvals.conftest import gen_keypair, sign_p1363

INTERNAL = frozenset({"alice", "bruno"})


@pytest.fixture
def tenancy():
    t = MemoryTenancy()
    t.create_account("a@example.org")
    t.create_account("b@example.org")
    return t


def accounts(tenancy):
    a, b = sorted(tenancy.accounts.values(), key=lambda x: x.email)
    return a, b


# ─────────────────────────── résolution ───────────────────────────


def test_account_resolves_to_its_tenant_internal_to_internal(tenancy):
    a, b = accounts(tenancy)
    assert resolve_tenant(a.account_id, tenancy, INTERNAL) == a.tenant_id
    assert resolve_tenant(b.account_id, tenancy, INTERNAL) == b.tenant_id
    assert a.tenant_id != b.tenant_id
    assert resolve_tenant("alice", tenancy, INTERNAL) == INTERNAL_TENANT


@pytest.mark.parametrize("sender", [None, "", "mallory", "us_000000000000", "tn_ninabot"])
def test_unknown_sender_raises_never_falls_back(tenancy, sender):
    with pytest.raises(UnknownSender):
        resolve_tenant(sender, tenancy, INTERNAL)


def test_account_never_becomes_internal_even_if_listed(tenancy):
    a, _ = accounts(tenancy)
    assert resolve_tenant(a.account_id, tenancy, INTERNAL | {a.account_id}) == a.tenant_id
    with pytest.raises(UnknownSender):
        resolve_tenant("us_000000000000", tenancy, INTERNAL | {"us_000000000000"})


def test_suspended_account_is_refused(tenancy):
    a, _ = accounts(tenancy)
    tenancy.accounts[a.account_id] = dataclasses.replace(a, status="suspended")
    with pytest.raises(UnknownSender):
        resolve_tenant(a.account_id, tenancy, INTERNAL)
    assert account_tenant_fn(tenancy)(a.account_id) is None


# ─────────────────────────── GO par locataire ───────────────────────────

GO_TEXT = "vas-y, lance infra_lvextend sur /srv/share à 50G"
CALL = ToolCall(id="c1", name="infra_lvextend", arguments={"path": "/srv/share", "size": "50G"})
SPEC = ToolSpec("infra_lvextend", "lvm", {}, Effect.DESTRUCTIVE)


def _verdict(inv, sender, thread_id="t1"):
    turn = Turn(thread_id=thread_id, index=0, input=InputCheck(text=GO_TEXT), sender=sender)
    return inv.check_action(CALL, SPEC, turn).decision


def test_go_internal_unchanged_tester_only_from_own_account(tenancy):
    a, b = accounts(tenancy)
    inv = DeterministicInvariants(account_tenant=account_tenant_fn(tenancy))
    # Hors scope (bancs, tests) et dans l'interne : Alice et Bruno, comme avant ; jamais un compte de testeur.
    assert _verdict(inv, "alice") is Decision.ALLOW
    with tenant_scope(INTERNAL_TENANT):
        assert _verdict(inv, "alice") is Decision.ALLOW
        assert _verdict(inv, "bruno") is Decision.ALLOW
        assert _verdict(inv, a.account_id) is Decision.HITL
    with tenant_scope(a.tenant_id):
        assert _verdict(inv, a.account_id) is Decision.ALLOW
        assert _verdict(inv, "alice") is Decision.HITL  # Alice n'a AUCUN GO chez un testeur
        assert _verdict(inv, "bruno") is Decision.HITL
        assert _verdict(inv, b.account_id) is Decision.HITL


def test_go_tester_without_resolver_is_never_granted(tenancy):
    a, _ = accounts(tenancy)
    inv = DeterministicInvariants()
    with tenant_scope(a.tenant_id):
        assert _verdict(inv, a.account_id) is Decision.HITL


# ─────────────────────────── décideur d'une carte ───────────────────────────


def _device(backend, user):
    priv, spki = gen_keypair()
    d = backend.enroll_device(user=user, name="x", public_key_spki=spki)
    backend.confirm_device_cli(d.code)
    return d, priv


def _card(backend, thread_id, sender):
    turn = Turn(thread_id=thread_id, index=0, input=InputCheck(text="x"), channel="api", sender=sender)
    verdict = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:destructive",
                      reason="sans GO", strength="standard")
    return backend.request(turn, CALL, verdict, None)


def _decide(backend, approval_id, device, priv):
    ch = backend.challenge(approval_id)
    sig = sign_p1363(priv, f"{ch.canonical()}.approve".encode())
    return backend.decide(approval_id, "approve", Proof(challenge=ch.canonical(), signature=sig, device_id=device.id))


def test_card_is_decided_only_inside_its_tenant(tenancy):
    a, b = accounts(tenancy)
    backend = InMemoryApprovals(account_tenant=account_tenant_fn(tenancy))
    dev_a, priv_a = _device(backend, a.account_id)
    dev_b, priv_b = _device(backend, b.account_id)
    dev_n, priv_n = _device(backend, "alice")
    with tenant_scope(a.tenant_id):
        card = _card(backend, f"{a.tenant_id}:s-1", a.account_id)
        with pytest.raises(ProofInvalid, match="non autorisé"):
            _decide(backend, card.id, dev_b, priv_b)  # appareil de B sur une carte de A
        with pytest.raises(ProofInvalid, match="non autorisé"):
            _decide(backend, card.id, dev_n, priv_n)  # Alice n'a aucun droit chez un testeur
        assert _decide(backend, card.id, dev_a, priv_a).decided_by == a.account_id
    with tenant_scope(INTERNAL_TENANT):
        internal = _card(backend, "s-1", "alice")
        with pytest.raises(ProofInvalid, match="non autorisé"):
            _decide(backend, internal.id, dev_a, priv_a)  # un testeur ne décide jamais une carte interne
        assert _decide(backend, internal.id, dev_n, priv_n).decided_by == "alice"
    with tenant_scope(b.tenant_id):
        # La carte de A n'existe pas chez B : ni lecture, ni défi, ni décision.
        assert backend.get(card.id) is None and backend.pending() == []
        with pytest.raises(ApprovalNotFound):
            backend.challenge(card.id)
        with pytest.raises(ApprovalNotFound):
            backend.decide(card.id, "approve", Proof(challenge=json.dumps({"approval_id": card.id})))
        assert backend.get_device(dev_a.id) is None and [d.id for d in backend.list_devices()] == [dev_b.id]


def test_tester_card_is_never_sent_to_internal_notifier(tenancy):
    a, _ = accounts(tenancy)
    sent = []

    class Rec:
        def notify(self, approval):
            sent.append(approval.thread_id)

    backend = InMemoryApprovals(notifier=Rec(), account_tenant=account_tenant_fn(tenancy))
    with tenant_scope(a.tenant_id):
        _card(backend, f"{a.tenant_id}:s-1", a.account_id)
    with tenant_scope(INTERNAL_TENANT):
        _card(backend, "s-1", "alice")
    assert sent == ["s-1"]


def test_session_for_live_account_only(tenancy):
    a, _ = accounts(tenancy)
    backend = InMemoryApprovals(account_tenant=account_tenant_fn(tenancy))
    dev, priv = _device(backend, a.account_id)
    ch = backend.login_challenge(dev.id)
    s = backend.create_session(dev.id, ch.canonical(), sign_p1363(priv, f"{ch.canonical()}.login".encode()))
    assert s.user == a.account_id
    ghost, _ = _device(backend, "us_000000000000")
    with pytest.raises(ProofInvalid):
        backend.login_challenge(ghost.id)
    tenancy.accounts[a.account_id] = dataclasses.replace(a, status="suspended")
    with pytest.raises(ProofInvalid):
        backend.login_challenge(dev.id)
