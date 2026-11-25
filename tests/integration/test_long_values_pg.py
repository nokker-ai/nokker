"""Valeurs datées de la mémoire longue (migration 096) en VRAI Postgres jetable, service connecté en `nokker_app`
(ni propriétaire, ni BYPASSRLS). Ignoré sans `NOKKER_TEST_PG_DSN`. Prouve :
  - une valeur remplacée est archivée avec son lien, jamais deux valeurs VIVANTES pour la même clé (l'index unique
    partiel le refuse même en SQL brut) ; le rappel ne rend que la valeur finale, datée ;
  - capture automatique de bout en bout (extraction scriptée → écriture) ;
  - RLS par locataire : la valeur, l'archive et le journal des rappels d'un compte restent invisibles d'un autre, même
    en posant l'identifiant de compte d'un autre ;
  - chaque recherche de rappel laisse une ligne de journal (même sans résultat) ;
  - l'effacement d'un compte efface sa mémoire, valeurs archivées et journal compris.
"""
from __future__ import annotations

import json

import pytest

from tests.integration.test_household_rls import Acc, Accounts
from tests.integration.test_rls import pg, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

pytestmark = pytest.mark.integration

TA, TB = "tn_" + "a" * 12, "tn_" + "b" * 12
A = Acc(TA, "us_aaaaaaaaaaaa", "a@example.com")
B = Acc(TB, "us_bbbbbbbbbbbb", "b@example.com")


@pytest.fixture(scope="module")
def world(pg):  # noqa: F811
    import psycopg

    from nokker.memory.tenant_long import HashEmbedder, LongMemory, PgLongMemoryStore
    from nokker.memory.values import ValueCapture

    with psycopg.connect(pg["su"], autocommit=True) as su:
        for a in (A, B):
            su.execute("INSERT INTO tenants (id) VALUES (%s) ON CONFLICT DO NOTHING", (a.tenant_id,))
            su.execute("INSERT INTO accounts (id, tenant_id, email) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                       (a.account_id, a.tenant_id, a.email))
    store = PgLongMemoryStore(pg["app"], migrate_now=False)
    svc = LongMemory(store=store, embedder=HashEmbedder(), account_for_tenant=Accounts(A, B).account_for_tenant)
    model = {"answer": ""}
    cap = ValueCapture(long=svc, complete=lambda _m: model["answer"], enabled=True)
    return {"svc": svc, "cap": cap, "model": model, "pg": pg}


def _raw(dsn, actor, sql, params=(), *, tenant=None, account=None):
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant or actor.tenant_id,))
        conn.execute("SELECT set_config('app.account_id', %s, false)", (actor.account_id if account is None else account,))
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if cur.description else cur.rowcount
        conn.commit()
        return rows


def _turns(*items):
    from nokker.core.turn import InputCheck, Turn

    return [Turn(thread_id="th_1", index=i, input=InputCheck(text=t), reply="Noté.") for i, t in items]


def _vals(key, value, turn):
    return json.dumps({"values": [{"kind": "decision", "key": key, "value": value, "turn": turn}]})


def _in(actor, fn, *a, **kw):
    from nokker.tenancy.context import tenant_scope

    with tenant_scope(actor.tenant_id):
        return fn(*a, **kw)


@requires_pg
def test_migration_096_is_idempotent_and_adds_columns(world):
    from pathlib import Path

    import psycopg

    sql = (Path(__file__).parents[2] / "nokker" / "store" / "migrations" / "096_long_memory_values.sql").read_text(
        encoding="utf-8")
    with psycopg.connect(world["pg"]["owner"], autocommit=True) as conn:
        conn.execute(sql)
        conn.execute(sql)
        cols = {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE "
                                           "table_name = 'nokker_long_memory'").fetchall()}
    assert {"value_key", "value_text", "source_thread", "source_turn", "superseded_by", "superseded_at"} <= cols


@requires_pg
def test_capture_then_replacement_leaves_one_live_value_and_an_archive(world):
    svc, cap, model, dsn = world["svc"], world["cap"], world["model"], world["pg"]["app"]
    model["answer"] = _vals("budget marketing", "5000 francs", 1)
    assert _in(A, cap.capture, A.account_id, "th_1", _turns((1, "Le budget marketing est fixé à 5000 francs.")),
               "silence") == 1
    model["answer"] = _vals("budget marketing", "8000 francs", 3)
    assert _in(A, cap.capture, A.account_id, "th_1",
               _turns((3, "Finalement le budget marketing passe de 5000 à 8000 francs.")), "silence") == 1
    rows = _raw(dsn, A, "SELECT value_text, superseded_by, source_thread, source_turn FROM nokker_long_memory "
                        "WHERE value_key = 'budget marketing' ORDER BY created_at")
    assert [r[0] for r in rows] == ["5000 francs", "8000 francs"]
    assert rows[0][1] is not None and rows[1][1] is None  # l'ancienne archivée avec son lien, la nouvelle vivante
    assert rows[1][2] == "th_1" and rows[1][3] == 3
    live_name = _raw(dsn, A, "SELECT name FROM nokker_long_memory WHERE value_key = 'budget marketing' "
                             "AND superseded_by IS NULL")[0][0]
    assert rows[0][1] == live_name
    # l'index unique partiel refuse une deuxième valeur vivante, même en SQL brut
    with pytest.raises(Exception, match="nokker_long_memory_live_key|duplicate key"):
        _raw(dsn, A, "INSERT INTO nokker_long_memory (account_id, name, scope, kind, title, body, author_account, "
                     "created_at, updated_at, value_key, value_text) VALUES (%s, 'x-deux', 'user', 'fact', 't', 'b', "
                     "%s, 1, 1, 'budget marketing', '9 francs')", (A.account_id, A.account_id))
    out = _in(A, svc.recall, A.account_id, "quel est le budget marketing ?")
    assert out and "8000" in out[0].text and "5000" not in out[0].text
    arch = _in(A, svc.export, A.account_id)["archived"]
    assert [n["value"] for n in arch] == ["5000 francs"]


@requires_pg
def test_values_archive_and_journal_are_invisible_to_another_account(world):
    svc, dsn = world["svc"], world["pg"]["app"]
    _in(A, svc.recall, A.account_id, "budget marketing")
    assert _raw(dsn, A, "SELECT count(*) FROM nokker_long_recall_log")[0][0] >= 1
    for table in ("nokker_long_memory", "nokker_long_recall_log"):
        assert _raw(dsn, B, f"SELECT count(*) FROM {table}")[0][0] == 0
        # l'identifiant de compte de A posé sous le locataire de B, ou le locataire de A sans son compte : rien
        assert _raw(dsn, B, f"SELECT count(*) FROM {table}", account=A.account_id)[0][0] == 0
        assert _raw(dsn, A, f"SELECT count(*) FROM {table}", account="")[0][0] == 0
        assert _raw(dsn, B, f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (TA,))[0][0] == 0
    assert _in(B, svc.recall, B.account_id, "budget marketing") == []
    assert _in(B, svc.export, B.account_id)["user"] == []


@requires_pg
def test_every_search_is_journaled_even_without_result(world):
    svc, dsn = world["svc"], world["pg"]["app"]
    before = _raw(dsn, B, "SELECT count(*) FROM nokker_long_recall_log")[0][0]
    assert _in(B, svc.recall, B.account_id, "une question sans aucune réponse", 5, "th_9") == []
    rows = _raw(dsn, B, "SELECT thread_id, injected FROM nokker_long_recall_log ORDER BY id DESC LIMIT 1")
    assert _raw(dsn, B, "SELECT count(*) FROM nokker_long_recall_log")[0][0] == before + 1
    assert rows == [("th_9", 0)]


@requires_pg
def test_erasing_the_account_erases_values_archive_and_journal(world):
    import psycopg

    from nokker.admin.erase import erase_account

    su, app = world["pg"]["su"], world["pg"]["app"]

    def count(table, tenant):
        with psycopg.connect(su) as conn:
            return conn.execute(f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (tenant,)).fetchone()[0]

    assert count("nokker_long_memory", TA) == 2 and count("nokker_long_recall_log", TA) >= 1
    r = erase_account(app, account_id=A.account_id, apply=True, confirm=A.account_id)
    assert r.applied and not r.unscoped
    assert count("nokker_long_memory", TA) == 0 and count("nokker_long_recall_log", TA) == 0
