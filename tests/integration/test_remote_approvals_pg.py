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


@requires_pg
def test_webauthn_sign_count_is_persisted_in_devices_on_postgres(pg, pod_env):  # noqa: F811
    """Anti-clonage (docs/APPROVALS.md §3.4) : `devices.sign_count` (BIGINT, ajouté par 0001_approvals.sql au démarrage de
    `PgApprovals`) survit à un nouveau backend ; compteur égal/inférieur refusé, croissant accepté, 0 permanent accepté."""
    import hashlib

    import psycopg

    from nokker.approvals.pg import PgApprovals
    from tests.unit.approvals.test_webauthn import ORIGIN as WEB_ORIGIN
    from tests.unit.approvals.test_webauthn import _assertion

    def login(backend, priv, device, count: int) -> None:
        ch = backend.login_challenge(device.id)
        a = _assertion(priv, hashlib.sha256(ch.canonical().encode("utf-8")).digest(), sign_count=count)
        backend.create_session(device.id, ch.canonical(), webauthn=a)

    def stored(device) -> tuple:
        with psycopg.connect(pg["su"]) as conn:
            return conn.execute("SELECT sign_count FROM devices WHERE id = %s", (device.id,)).fetchone()

    approvals = PgApprovals(dsn=pg["app"], webauthn_origin=WEB_ORIGIN)
    try:
        priv, spki = gen_keypair()
        device = approvals.confirm_device_cli(approvals.enroll_device(user="alice", name="MacBook",
                                                                      public_key_spki=spki).code)
        login(approvals, priv, device, 7)
        for stale in (7, 2):
            with pytest.raises(ProofInvalid, match="compteur de signature non croissant"):
                login(approvals, priv, device, stale)
        assert stored(device) == (7,)

        reopened = PgApprovals(dsn=pg["app"], webauthn_origin=WEB_ORIGIN)  # redémarrage : le compteur est en base
        try:
            with pytest.raises(ProofInvalid, match="compteur de signature non croissant"):
                login(reopened, priv, device, 7)
            login(reopened, priv, device, 8)
        finally:
            reopened.close()
        assert stored(device) == (8,)

        priv0, spki0 = gen_keypair()
        synced = approvals.confirm_device_cli(approvals.enroll_device(user="alice", name="iCloud",
                                                                      public_key_spki=spki0).code)
        for _ in range(2):
            login(approvals, priv0, synced, 0)  # passkey synchronisé : compteur toujours nul, accepté
        assert stored(synced) == (None,)
    finally:
        approvals.close()
