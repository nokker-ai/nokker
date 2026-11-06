"""Plusieurs connecteurs et partages dans le foyer (068) sur un vrai Postgres, connexion `nokker_app`. Ignoré sans
`NOKKER_TEST_PG_DSN`. Prouve en SQL brut :
  - une ligne d'avant 068 devient le connecteur par défaut, avec un id et un libellé ;
  - le bénéficiaire lit SA ligne de partage active, jamais celle d'un autre, jamais une demande en attente, jamais le
    secret (`connector_credentials` reste fermée hors du locataire du propriétaire) ;
  - la sortie du foyer (de lui OU du propriétaire) ferme la lecture aussitôt ;
  - le journal est en ajout seul ;
  - le service complet : carte approuvée, boîte partagée lue par le bénéficiaire, retrait.
"""
from __future__ import annotations

import pytest

from tests.integration.test_household_rls import Accounts, Acc, _raw
from tests.integration.test_rls import pg, pod_env, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

pytestmark = pytest.mark.integration

TP, TM, TO = (f"tn_{c * 12}" for c in "xyz")
P = Acc(TP, "us_cgpayeur0001", "payeur@example.com")
M = Acc(TM, "us_cgmembre0001", "membre@example.com")
O = Acc(TO, "us_cgvoisin0001", "voisin@example.com")  # noqa: E741


@pytest.fixture(scope="module")
def fam(pg):  # noqa: F811
    import psycopg

    from nokker.household.model import Settings
    from nokker.household.service import Household
    from nokker.household.store import PgHouseholdStore
    from nokker.team.service import Team
    from nokker.tenancy.context import tenant_scope

    with psycopg.connect(pg["su"], autocommit=True) as su:
        for a in (P, M, O):
            su.execute("INSERT INTO tenants (id) VALUES (%s) ON CONFLICT DO NOTHING", (a.tenant_id,))
            su.execute("INSERT INTO accounts (id, tenant_id, email) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                       (a.account_id, a.tenant_id, a.email))
    hh = Household(team=Team(store=PgHouseholdStore(pg["app"], migrate_now=False), tenancy=Accounts(P, M, O)),
                   settings=Settings())
    with tenant_scope(TP):
        hid = hh.create(P, {"name": "Maison"})["id"]
        iv = hh.invite(P, {"email": M.email})
    with tenant_scope(TM):
        hh.accept(M, iv["id"])
    return {"hh": hh, "hid": hid, "pg": pg}


def _grant(dsn, owner, grantee, hid, htenant, level="read", cid="mb_cgtest0001", pending=False):
    _raw(dsn, owner, "INSERT INTO connector_grants (connector_id, owner_account, grantee_account, household_id, "
                     "household_tenant, kind, level, pending_level, granted_at) VALUES (%s,%s,%s,%s,%s,'mail',%s,%s,now())",
         (cid, owner.account_id, grantee.account_id, hid, htenant, None if pending else level,
          level if pending else None), account=False)


@requires_pg
def test_legacy_row_becomes_the_default(pg):  # noqa: F811
    import psycopg

    from nokker.store.pg import migration_files

    with psycopg.connect(pg["owner"], autocommit=True) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (TO,))
        # une ligne « d'avant » : sans id ni libellé (on retire les contraintes le temps de l'écrire)
        conn.execute("ALTER TABLE connector_credentials DROP CONSTRAINT connector_credentials_pkey")
        conn.execute("ALTER TABLE connector_credentials ALTER COLUMN id DROP NOT NULL")
        conn.execute("ALTER TABLE connector_credentials ALTER COLUMN label DROP NOT NULL")
        conn.execute("ALTER TABLE connector_credentials ADD CONSTRAINT connector_credentials_pkey PRIMARY KEY (account_id, kind)")
        conn.execute("INSERT INTO connector_credentials (tenant_id, account_id, kind, settings, secret_enc) VALUES "
                     "(%s, %s, 'mail', '{\"address\": \"v@example.com\"}', 'x')", (TO, O.account_id))
        sql = next(f for f in migration_files() if f.name == "068_connectors_multi.sql").read_text(encoding="utf-8")
        conn.execute(sql)
        conn.execute(sql)  # rejouée : rien ne bouge
        r = conn.execute("SELECT id, label, is_default FROM connector_credentials WHERE account_id = %s",
                         (O.account_id,)).fetchone()
        pk = conn.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'connector_credentials_pkey'"
                          ).fetchone()[0]
        conn.execute("DELETE FROM connector_credentials WHERE account_id = %s", (O.account_id,))
    assert r[0].startswith("mb_") and len(r[0]) == 13 and r[1] == "v@example.com" and r[2] is True
    assert pk == "PRIMARY KEY (account_id, id)"


@requires_pg
def test_grantee_reads_only_his_active_grant_while_both_in_household(fam):
    dsn, hid = fam["pg"]["app"], fam["hid"]
    _grant(dsn, P, M, hid, TP, cid="mb_cgactive001")
    _grant(dsn, P, M, hid, TP, cid="mb_cgpending01", pending=True)
    _grant(dsn, P, O, hid, TP, cid="mb_cgvoisin001")  # O n'est pas du foyer
    seen = {r[0] for r in _raw(dsn, M, "SELECT connector_id FROM connector_grants")}
    assert seen == {"mb_cgactive001"}  # ni la demande en attente, ni la ligne d'un autre
    assert _raw(dsn, O, "SELECT connector_id FROM connector_grants") == []  # pas membre : rien
    assert _raw(dsn, M, "SELECT connector_id FROM connector_grants", account=False) == []  # sans acteur : rien
    # usurpation : M se déclare acteur dans le locataire de P → nokker_actor() nul (059)
    assert _raw(dsn, Acc(TP, M.account_id, M.email), "SELECT 1 FROM connector_grants WHERE grantee_account = %s "
                "AND tenant_id <> %s", (M.account_id, TP)) == []
    # le bénéficiaire n'écrit rien chez le propriétaire, et ne lit jamais le secret
    assert _raw(dsn, M, "UPDATE connector_grants SET level = 'send'") == 0
    assert _raw(dsn, M, "DELETE FROM connector_grants") == 0
    assert _raw(dsn, M, "SELECT count(*) FROM connector_credentials WHERE tenant_id = %s", (TP,))[0][0] == 0


@requires_pg
def test_leaving_the_household_closes_the_read(fam):
    from nokker.tenancy.context import tenant_scope

    dsn, hid, hh = fam["pg"]["app"], fam["hid"], fam["hh"]
    _grant(dsn, M, P, hid, TP, cid="mb_cgfrommemb1")  # M partage avec le payeur
    assert {r[0] for r in _raw(dsn, P, "SELECT connector_id FROM connector_grants")} >= {"mb_cgfrommemb1"}
    with tenant_scope(TM):
        hh.remove(M, M.account_id)  # M quitte le foyer
    # M sorti : il ne lit plus ce que P lui partageait (ligne dans le locataire de P)…
    assert _raw(dsn, M, "SELECT connector_id FROM connector_grants WHERE tenant_id = %s", (TP,)) == []
    # … et P ne lit plus ce que M lui partageait (propriétaire sorti)
    assert "mb_cgfrommemb1" not in {r[0] for r in _raw(dsn, P, "SELECT connector_id FROM connector_grants")}
    with tenant_scope(TP):  # retour au foyer pour les tests suivants
        iv = hh.invite(P, {"email": M.email})
    with tenant_scope(TM):
        hh.accept(M, iv["id"])


@requires_pg
def test_events_are_append_only(fam):
    import psycopg

    dsn = fam["pg"]["app"]
    _raw(dsn, P, "INSERT INTO connector_grant_events (connector_id, owner_account, grantee_account, actor_account, event, "
                 "level) VALUES ('mb_x', %s, %s, %s, 'granted', 'read')", (P.account_id, M.account_id, P.account_id),
         account=False)
    for sql in ("UPDATE connector_grant_events SET level = 'send'", "DELETE FROM connector_grant_events"):
        with pytest.raises(psycopg.errors.RaiseException):
            _raw(dsn, P, sql, account=False)


@requires_pg
def test_service_end_to_end_card_shared_box_and_revoke(fam):
    from cryptography.fernet import Fernet

    from nokker.connectors import TestResult
    from nokker.connectors.crypto import SecretBox
    from nokker.connectors.grants import PgGrantStore
    from nokker.connectors.household import HouseholdLink
    from nokker.connectors.provider import ConnectorProvider
    from nokker.connectors.service import Connectors
    from nokker.connectors.store import PgConnectorStore
    from nokker.tenancy.context import tenant_scope

    dsn, hh = fam["pg"]["app"], fam["hh"]
    accounts = Accounts(P, M, O)

    class Approvals:
        cards: dict = {}

        def request(self, turn, call, verdict, advice):
            c = type("C", (), {"id": f"ap_{len(self.cards)}", "arguments": call.arguments, "status": "pending",
                               "decided_by": None})()
            self.cards[c.id] = c
            return c

        def get(self, i):
            return self.cards.get(i)

        def mark_failed(self, i, why):
            self.cards[i].status = "failed"

    store = PgConnectorStore(dsn, SecretBox(Fernet.generate_key()))
    ap = Approvals()
    svc = Connectors(store, tester=lambda *a: TestResult(True, "ok"), grants=PgGrantStore(dsn),
                     households=HouseholdLink(lambda: hh, accounts), approvals=ap,
                     tenant_of=lambda a: accounts.account(a).tenant_id)
    mail = {"preset": "custom", "address": "membre@example.com", "imap_host": "imap.example.com", "imap_port": 993,
            "smtp_host": "smtp.example.com", "smtp_port": 465}
    with tenant_scope(TM):
        box = svc.add(M, "mail", "famille", mail, "s3cret")
        code, out = svc.grant(M, box["id"], P.account_id, "send")
    assert code == 202
    ap.cards[out["approval_id"]].status = "approved"
    with tenant_scope(TM):
        svc.on_grant_decision(ap.cards[out["approval_id"]])
    prov = ConnectorProvider(store, accounts.account_for_tenant, shared=svc.shared)
    with tenant_scope(TP):
        listed = svc.listing(P)["connectors"]
        boxes = prov.boxes_for(TP)
        built = prov._build_box(P.account_id, boxes[0])  # le secret lu dans le locataire de M, par le noyau
    assert [(c["id"], c["access"], c["owner"]["account_id"]) for c in listed] == [(box["id"], "send", M.account_id)]
    assert boxes[0].owner_name == "membre" and boxes[0].can_write and built is not None
    with tenant_scope(TP):
        assert svc.revoke_grant(P, box["id"], "me") is True
        assert svc.listing(P)["connectors"] == [] and prov.boxes_for(TP) == []
    with tenant_scope(TM):
        assert [e.event for e in svc.grants.events(M.account_id)] == ["granted", "revoked"]
