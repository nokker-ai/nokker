"""Consolidation nocturne (migration 053, docs/CONSOLIDATION.md) en base : vrai Postgres pgvector jetable, rôles de
deploy/postgres/roles.sql, moteur connecté en `nokker_app` (ni propriétaire, ni BYPASSRLS), faux modèle. Ignoré sans
`NOKKER_TEST_PG_DSN`.

Comptes : le payeur P, un membre adulte M et un profil `teen` K (chacun dans SON locataire), O hors foyer. Prouve :
  - la consolidation du payeur touche sa mémoire et celle de son foyer, jamais la mémoire de profil d'un membre ;
  - défaire rend les lignes EXACTES d'avant (plongement pgvector compris), et redéfaire ne change rien ;
  - en SQL brut, un autre compte (même en posant le locataire du payeur) ne lit ni nuit, ni opération, ni archive ;
  - un seul passage à la fois par locataire (verrou consultatif, tous pods confondus) ;
  - l'effacement d'un membre retire ses contributions archivées et les opérations qui les citent."""
from __future__ import annotations

import pytest

from tests.integration.test_household_rls import Acc, Accounts, _att
from tests.integration.test_rls import pg, pod_env, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

pytestmark = pytest.mark.integration

TP, TM, TK, TO = (f"tn_{c * 12}" for c in "pmko")
P = Acc(TP, "us_payeur00001", "payeur@example.com")
M = Acc(TM, "us_membre00001", "membre@example.com")
K = Acc(TK, "us_ado00000001", "ado@example.com")
O = Acc(TO, "us_voisin00001", "voisin@example.com")
ALL = (P, M, K, O)
NOTES = "SELECT name, title, body, origin, embed_model, embedding::text, created_at, updated_at FROM nokker_long_memory " \
        "ORDER BY tenant_id, account_id, name"


@pytest.fixture(scope="module")
def cx(pg):  # noqa: F811
    import psycopg

    from nokker.household.model import Settings as HSettings
    from nokker.household.service import Household
    from nokker.household.store import PgHouseholdStore
    from nokker.memory.consolidation.engine import Consolidator
    from nokker.memory.consolidation.stores import PgConsolidationStore
    from nokker.memory.tenant_long import (
        HashEmbedder,
        LongMemory,
        PgLongMemoryStore,
        household_membership,
    )
    from nokker.onboarding.consents import PgConsents
    from nokker.store.pg import apply_migrations
    from nokker.team.service import Team
    from nokker.tenancy.context import tenant_scope

    with psycopg.connect(pg["su"], autocommit=True) as su:
        for a in ALL:
            su.execute("INSERT INTO tenants (id) VALUES (%s) ON CONFLICT DO NOTHING", (a.tenant_id,))
            su.execute("INSERT INTO accounts (id, tenant_id, email) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                       (a.account_id, a.tenant_id, a.email))
        su.execute("CREATE EXTENSION IF NOT EXISTS vector")
    assert apply_migrations(pg["owner"], strict=True)
    assert apply_migrations(pg["owner"], strict=True)  # 053 rejouée : idempotente
    accounts = Accounts(*ALL)
    hh = Household(team=Team(store=PgHouseholdStore(pg["app"], migrate_now=False), tenancy=accounts),
                   settings=HSettings(minors=True), consents=PgConsents(pg["app"]))
    with tenant_scope(TP):
        hid = hh.create(P, {"name": "Maison"})["id"]
        iv_m = hh.invite(P, {"email": M.email})
        iv_k = hh.invite(P, {"email": K.email, "profile": "teen", "attestation": _att()})
    with tenant_scope(TM):
        hh.accept(M, iv_m["id"])
    with tenant_scope(TK):
        hh.accept(K, iv_k["id"])
    long = LongMemory(store=PgLongMemoryStore(pg["app"], migrate_now=False), membership=household_membership(hh),
                      embedder=HashEmbedder(), account_for_tenant=accounts.account_for_tenant, review=True)
    store = PgConsolidationStore(pg["app"], migrate_now=False)
    c = Consolidator(store=store, embed=long._embed_doc)
    return {"long": long, "store": store, "c": c, "hid": hid, "pg": pg, "hh": hh}


def _rows(su: str, sql: str = NOTES, params: tuple = ()) -> list[tuple]:
    import psycopg

    with psycopg.connect(su) as conn:
        return conn.execute(sql, params).fetchall()


def _seed(cx) -> None:
    from nokker.memory.tenant_long import LongNote
    from nokker.tenancy.context import tenant_scope

    long, hid = cx["long"], cx["hid"]
    with tenant_scope(TP):
        long.write(P.account_id, scope="user", title="Thé", text="Le payeur préfère le thé vert.")
        long.write(P.account_id, scope="household", title="Poubelles", text="Les poubelles sortent le lundi.")
        actor = long.actor(P.account_id)
        m = long._member(P.account_id)
        # contribution d'un membre acceptée (auteur M), doublon exact de la note du payeur
        vec, model = long._embed_doc("Poubelles (M)", "Les poubelles sortent le lundi.")
        long.store.put(actor, LongNote(name="fact-poubelles-m", scope="household", kind="fact", title="Poubelles (M)",
                                       text="Les poubelles sortent le lundi.", account_id=P.account_id, tenant_id=TP,
                                       author_account=M.account_id, household_id=m.household_id,
                                       origin="pending:mp_test", created_at=2.0e9, updated_at=2.0e9),
                       vec, model)
    with tenant_scope(TM):
        long.write(M.account_id, scope="user", title="Train", text="Le membre prend le train de 7 h 12.")
        actor = long.actor(M.account_id)
        vec, model = long._embed_doc("Train bis", "Le membre prend le train de 7 h 12.")
        long.store.put(actor, LongNote(name="fact-train-bis", scope="user", kind="fact", title="Train bis",
                                       text="Le membre prend le train de 7 h 12.", account_id=M.account_id,
                                       tenant_id=TM, author_account=M.account_id, origin="propose:x",
                                       created_at=2.0e9, updated_at=2.0e9), vec, model)
    _ = hid


@requires_pg
def test_payer_night_touches_only_its_own_and_household_notes_and_undo_is_exact(cx):
    from nokker.tenancy.context import tenant_scope

    _seed(cx)
    su, c = cx["pg"]["su"], cx["c"]
    before = _rows(su)
    assert any(r[5] is not None for r in before)  # plongements pgvector présents
    with tenant_scope(TP):
        rep = c.run(P.account_id)
    assert rep["run"]["status"] == "done" and rep["run"]["counts"]["merge"] == 1
    after = _rows(su)
    gone = {r[0] for r in before} - {r[0] for r in after}
    assert gone == {"fact-poubelles-m"}  # le doublon du foyer ; le profil du membre (doublon aussi) n'est pas touché
    assert _rows(su, "SELECT count(*) FROM nokker_memory_archive WHERE author_account = %s",
                 (M.account_id,))[0][0] == 1
    with tenant_scope(TP):
        out = c.undo_run(P.account_id, rep["run"]["id"])
        assert out["complete"]
        assert _rows(su) == before  # EXACT, plongement compris
        again = c.undo_run(P.account_id, rep["run"]["id"])
        assert again["undone"] == [] and _rows(su) == before
    # le membre consolide SA mémoire seulement
    with tenant_scope(TM):
        rep_m = c.run(M.account_id)
    assert rep_m["run"]["counts"]["merge"] == 1
    assert {r[0] for r in _rows(su)} == {r[0] for r in before} - {"fact-train-bis"}


@requires_pg
def test_raw_sql_sees_no_other_account_journal(cx):
    import psycopg

    app = cx["pg"]["app"]
    assert _rows(cx["pg"]["su"], "SELECT count(*) FROM nokker_consolidation_ops")[0][0] >= 1
    for tenant, account in ((TP, M.account_id), (TM, P.account_id), (TO, O.account_id), (TP, ""), (TK, K.account_id)):
        with psycopg.connect(app) as conn:
            conn.execute("SELECT set_config('app.tenant_id', %s, false), set_config('app.account_id', %s, false)",
                         (tenant, account))
            for t in ("nokker_consolidation_runs", "nokker_consolidation_ops", "nokker_memory_archive",
                      "nokker_memory_annotations", "nokker_memory_pins", "nokker_consolidation_settings"):
                n = conn.execute(f"SELECT count(*) FROM {t} WHERE account_id <> %s", (account,)).fetchone()[0]
                assert n == 0, (t, tenant, account)
            if (tenant, account) == (TP, M.account_id):  # le locataire du payeur posé à la main : rien du payeur
                assert conn.execute("SELECT count(*) FROM nokker_consolidation_runs").fetchone()[0] == 0
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute("INSERT INTO nokker_memory_pins (tenant_id, account_id, name, pinned_at) VALUES "
                                 "(%s, %s, 'x', 0)", (TP, P.account_id))


@requires_pg
def test_one_pass_at_a_time_per_tenant(cx):
    import psycopg

    from nokker.tenancy.context import tenant_scope

    with psycopg.connect(cx["pg"]["app"], autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (f"nokker-consolidation:{TP}",))
        with tenant_scope(TP):
            assert cx["c"].run(P.account_id)["status"] == "busy"
        other.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (f"nokker-consolidation:{TP}",))
    with tenant_scope(TP):
        assert cx["c"].run(P.account_id, trigger="schedule", night="2026-11-03")["run"]["status"] == "done"
        assert cx["c"].run(P.account_id, trigger="schedule", night="2026-11-03")["status"] == "already"


@requires_pg
def test_erasing_a_member_removes_archived_contributions(cx):
    from nokker.admin.erase import erase_account
    from nokker.tenancy.context import tenant_scope

    su, app = cx["pg"]["su"], cx["pg"]["app"]
    with tenant_scope(TP):
        cx["c"].run(P.account_id)  # le doublon écrit par M est (ou reste) à l'archive
    assert _rows(su, "SELECT count(*) FROM nokker_memory_archive WHERE author_account = %s AND restored_at IS NULL",
                 (M.account_id,))[0][0] >= 1
    r = erase_account(app, account_id=M.account_id, apply=True, confirm=M.account_id)
    assert r.applied and not r.unscoped
    assert _rows(su, "SELECT count(*) FROM nokker_memory_archive WHERE author_account = %s", (M.account_id,))[0][0] == 0
    assert _rows(su, "SELECT count(*) FROM nokker_consolidation_ops WHERE %s = ANY (authors)",
                 (M.account_id,))[0][0] == 0
    assert _rows(su, "SELECT count(*) FROM nokker_consolidation_runs WHERE tenant_id = %s", (TP,))[0][0] >= 1
