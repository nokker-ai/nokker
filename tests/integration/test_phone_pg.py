"""Nœud téléphone (077) sur un vrai Postgres sous RLS, connexion `nokker_app` : nœuds et tâches invisibles d'un autre
locataire ; une machine `phone` n'est pas une machine Lokkal (outils Lokkal, relais) ; file et résultat idempotent.
Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import time

import pytest

from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401  (pg : fixture)

pytestmark = pytest.mark.integration


@requires_pg
def test_pg_phone_store_and_link_kind(pg):  # noqa: F811
    from dataclasses import replace

    from nokker.cloud.link import LinkService
    from nokker.cloud.pg import PgLinkStore
    from nokker.cloud.phone import PgPhoneStore, PhoneNode, PhoneTask
    from nokker.tenancy.context import tenant_scope

    links = LinkService(store=PgLinkStore(pg["app"]))
    m, token = links.add_phone("us_pga", A, "iPhone")
    got = links.machine_for_token(token)
    assert got is not None and got.kind == "phone" and got.id == m.id
    assert links.machines_of("us_pga") == [] and [x.id for x in links.machines_of("us_pga", phones=True)] == [m.id]
    assert not links.has_machine(A) and links.machine_by_host(m.host) is None  # pas un Lokkal
    st = PgPhoneStore(pg["app"])
    now = time.time()
    node = PhoneNode(machine_id=m.id, account="us_pga", platform="ios", attested=False, key_storage="secure_enclave",
                     pubkey_spki="AAAA", key_fp="ab" * 32, created_at=now)
    with tenant_scope(A):
        st.add_node(node)
        st.set_beat(m.id, {"reach": "live"}, now)
        assert st.node(m.id).beat == {"reach": "live"} and [n.machine_id for n in st.nodes_of("us_pga")] == [m.id]
        t = PhoneTask(task_id="pt_" + "1" * 16, machine_id=m.id, account="us_pga", op="zone.state", args={},
                      args_sha256="x", deadline=now + 60, created_at=now)
        st.add_task(t)
        st.put_task(replace(t, status="done", result="ok"))
        assert st.task(t.task_id).result == "ok" and st.count_since("us_pga", now - 10) == 1
        assert st.queued(m.id) == []
    with tenant_scope(B):
        assert st.node(m.id) is None and st.task(t.task_id) is None and st.nodes_of("us_pga") == []


@requires_pg
def test_pg_phone_registrations_claim_once_and_rls(pg):  # noqa: F811
    from nokker.cloud.phone import PgPhoneStore
    from nokker.tenancy.context import tenant_scope

    st = PgPhoneStore(pg["app"])
    now = time.time()
    reg = {"approval_id": "ap_pgreg01", "account": "us_pga", "name": "iPhone", "platform": "ios",
           "key_storage": "secure_enclave", "pubkey": "AAAA", "enc_key": "BBBB", "key_fp": "ab" * 32,
           "state": "pending", "expires_at": now + 600, "created_at": now}
    with tenant_scope(A):
        st.put_reg(reg)
        assert st.reg("ap_pgreg01")["state"] == "pending" and st.claim_reg("ap_pgreg01") is None  # pas prête
        st.set_reg("ap_pgreg01", state="ready", machine_id="mc_x", sealed_token={"v": 1, "ct": "x"})
        assert st.claim_reg("ap_pgreg01") == {"v": 1, "ct": "x"}
        assert st.claim_reg("ap_pgreg01") is None  # une seule fois
        assert st.reg("ap_pgreg01")["state"] == "claimed" and st.reg("ap_pgreg01")["sealed_token"] is None
        st.put_reg({**reg, "approval_id": "ap_pgreg02", "expires_at": now - 1})
        assert st.purge_regs("us_pga", now) == 1 and st.reg("ap_pgreg02") is None  # ménage à l'expiration
    with tenant_scope(B):
        assert st.reg("ap_pgreg01") is None  # autre locataire : rien


@requires_pg
def test_pg_egress_hosts_per_member_and_rls(pg):  # noqa: F811
    from nokker.cloud.egress import PgEgressStore
    from nokker.tenancy.context import tenant_scope

    st = PgEgressStore(pg["app"])
    with tenant_scope(A):
        assert st.hosts("us_pga") == []
        st.put_hosts("us_pga", ["news.example.org", "*.example.net"])
        st.put_hosts("us_pga", ["news.example.org"])  # remplacée
        assert st.hosts("us_pga") == ["news.example.org"]
    with tenant_scope(B):
        assert st.hosts("us_pga") == []


@requires_pg
def test_pg_task_origin_and_home_journal_under_rls_then_erased_with_the_account(pg):  # noqa: F811
    from nokker.admin.erase import erase_account
    from nokker.cloud.link import LinkService
    from nokker.cloud.pg import PgLinkStore
    from nokker.cloud.phone import PgPhoneStore, PhoneNode, PhoneTask
    from nokker.cloud.phone_home import PgHomeJournal, journal_rows
    from nokker.tenancy.context import tenant_scope
    from nokker.tenancy.store import PgTenancy

    tenancy = PgTenancy(pg["app"])
    acc = tenancy.create_account("alice-maison@example.org")
    links, st = LinkService(store=PgLinkStore(pg["app"])), PgPhoneStore(pg["app"])
    m, token = links.add_phone(acc.account_id, acc.tenant_id, "Tablette")
    now = time.time()
    decision = {"class": "strong", "approval_id": "ap_pg1"}
    with tenant_scope(acc.tenant_id):
        st.add_node(PhoneNode(machine_id=m.id, account=acc.account_id, platform="android", attested=True,
                              key_storage="strongbox", pubkey_spki="AAAA", key_fp="ef" * 32, created_at=now))
        st.add_task(PhoneTask(task_id="pt_" + "2" * 16, machine_id=m.id, account=acc.account_id, op="ha.call_service",
                              args={}, args_sha256="x", deadline=now + 60, created_at=now,
                              origin={"kind": "turn", "decision": decision}))
        assert st.task("pt_" + "2" * 16).origin["decision"] == decision  # 081 : l'origine est en base
        PgHomeJournal(pg["app"], acc.account_id).append({"event": "home.action", "who": acc.account_id,
                                                         "what": {"entity_id": "light.salon"}, "outcome": "executed"})
        rows = journal_rows(pg["app"], acc.account_id)
        assert [r["record"]["outcome"] for r in rows] == ["executed"]
    with tenant_scope(B):
        assert journal_rows(pg["app"], acc.account_id) == []  # autre locataire : rien
    rep = erase_account(pg["app"], account_id=acc.account_id, apply=True, confirm=acc.account_id)
    assert rep.deleted.get("home_journal") == 1 and links.machine_for_token(token) is None
