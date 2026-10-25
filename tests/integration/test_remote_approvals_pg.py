"""Lot X10 côté machine sur un vrai Postgres (rôle du pod `nokker_app`) : une carte forte du noyau local (`PgApprovals`,
table `approvals` sous RLS), un défi relayé à durée longue (nonce en base, `used_nonces`), une preuve WebAuthn d'un
appareil du compte lié vérifiée par `decide_remote` ; preuve fausse sans brûler le défi, rejeu refusé, nonce consommé
une seule fois. Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import pytest

from nokker.approvals.errors import ApprovalNotPending, ProofInvalid
from nokker.approvals.types import Device
from nokker.core.ports import Proof
from tests.integration.test_rls import pg, pod_env, requires_pg  # noqa: F401  (fixtures)
from tests.unit.approvals.conftest import gen_keypair
from tests.unit.approvals.test_memory_approvals import make_call, make_turn, make_verdict
from tests.unit.test_remote_approvals import ORIGIN, RP_ID, webauthn_proof


def _proof(p: dict, challenge: str) -> Proof:
    return Proof(challenge=challenge, device_id=p["device_id"],
                 webauthn_client_data_json=p["webauthn_client_data_json"],
                 webauthn_authenticator_data=p["webauthn_authenticator_data"],
                 webauthn_signature=p["webauthn_signature"])


@requires_pg
def test_decide_remote_on_postgres(pg, pod_env):  # noqa: F811
    import psycopg

    from nokker.approvals.pg import PgApprovals
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    approvals = PgApprovals(dsn=pg["app"])
    try:
        priv, spki = gen_keypair()
        device = Device(id="dev-tel", code="", user="us_pierre", name="iPhone", public_key_spki=spki,
                        status="approved", approved_via="nokker.ai")
        with tenant_scope(INTERNAL_TENANT):
            card = approvals.request(make_turn(), make_call(), make_verdict("strong"), None)
            ch = approvals.challenge(card.id, ttl_s=3600)
            assert ch.exp - approvals.clock() > 3000  # défi relayé : une heure, pas 120 s
            text = ch.canonical()
            thief, _ = gen_keypair()
            with pytest.raises(ProofInvalid):
                approvals.decide_remote(card.id, "approve", _proof(webauthn_proof(thief, device.id, text, "approve"),
                                                                   text), device, origin=ORIGIN, rp_id=RP_ID,
                                        decided_by="local-owner")
            good = _proof(webauthn_proof(priv, device.id, text, "approve"), text)
            decided = approvals.decide_remote(card.id, "approve", good, device, origin=ORIGIN, rp_id=RP_ID,
                                              decided_by="local-owner")
            assert decided.status == "approved" and decided.decided_via == "webauthn"
            with pytest.raises(ApprovalNotPending):
                approvals.decide_remote(card.id, "approve", good, device, origin=ORIGIN, rp_id=RP_ID,
                                        decided_by="local-owner")
            assert approvals.get(card.id).status == "approved"
        with psycopg.connect(pg["su"]) as conn:
            used = conn.execute("SELECT used FROM used_nonces WHERE nonce = %s", (ch.nonce,)).fetchone()
            events = conn.execute("SELECT event_type, data->>'device_id', data->>'remote' FROM approval_events "
                                  "WHERE approval_id = %s ORDER BY 1", (card.id,)).fetchall()
        assert used == (True,)
        assert ("approved", "dev-tel", "true") in events
    finally:
        approvals.close()


@requires_pg
def test_card_keeps_the_call_id_on_postgres(pg, pod_env):  # noqa: F811
    """`call_id` de la carte (docs/LOKKAL.md §7.4.9) : écrit à la demande, relu après une décision et un `mark_*`."""
    from nokker.approvals.pg import PgApprovals
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    approvals = PgApprovals(dsn=pg["app"])
    try:
        with tenant_scope(INTERNAL_TENANT):
            call = make_call()
            card = approvals.request(make_turn(), call, make_verdict("standard"), None)
            assert approvals.get(card.id).call_id == call.id
            approvals.mark_failed(card.id, "test")
            assert approvals.get(card.id).call_id == call.id
    finally:
        approvals.close()
