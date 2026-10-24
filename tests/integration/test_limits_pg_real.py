"""`PgUsage` (migration 007) contre un vrai Postgres : incrément borné, compteurs par locataire, RLS forcée vue d'un
rôle non propriétaire. Ignoré sans `NOKKER_TEST_PG_DSN` (comme test_pg_real.py) ; le DSN doit être superutilisateur
(création du rôle de test)."""
from __future__ import annotations

import os
from datetime import date

import pytest

DSN = os.environ.get("NOKKER_TEST_PG_DSN")
requires_pg = pytest.mark.skipif(not DSN, reason="NOKKER_TEST_PG_DSN non posé — pas de Postgres jetable")

A, B = "tn_aaaaaaaaaaaa", "tn_bbbbbbbbbbbb"
DAY = date(2026, 10, 28)


@requires_pg
def test_take_is_bounded_and_per_tenant():
    import psycopg

    from nokker.store.pg import apply_migrations
    from nokker.tenancy.limits import PgUsage

    apply_migrations(DSN)
    apply_migrations(DSN)  # rejouable
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DELETE FROM tenant_usage WHERE day = %s", (DAY,))
    usage = PgUsage(DSN)
    assert [usage.take(A, "us_a", DAY, "turns", 2) for _ in range(3)] == [True, True, False]
    assert usage.take(B, "us_a", DAY, "turns", 2)  # autre locataire, autre compteur
    assert usage.take(A, "us_a", DAY, "deep", 1) and not usage.take(A, "us_a", DAY, "deep", 1)
    assert not usage.take(A, "us_z", DAY, "turns", 0)


@requires_pg
def test_rls_hides_other_tenants_from_non_owner_role():
    import psycopg

    from nokker.store.pg import apply_migrations
    from nokker.tenancy.limits import PgUsage

    apply_migrations(DSN)
    PgUsage(DSN).take(A, "us_rls", DAY, "turns", 50)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DO $$ BEGIN CREATE ROLE nokker_limits_test NOLOGIN NOSUPERUSER NOBYPASSRLS; "
                     "EXCEPTION WHEN duplicate_object THEN NULL; END $$")
        conn.execute("GRANT SELECT, INSERT, UPDATE ON tenant_usage TO nokker_limits_test")
        with conn.transaction():
            conn.execute("SET LOCAL ROLE nokker_limits_test")
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (B,))
            rows = conn.execute("SELECT tenant_id FROM tenant_usage WHERE account = 'us_rls'").fetchall()
            assert rows == []
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (A,))
            rows = conn.execute("SELECT tenant_id FROM tenant_usage WHERE account = 'us_rls'").fetchall()
            assert rows == [(A,)]


@requires_pg
def test_week_counter_give_back_and_read():
    import psycopg

    from nokker.store.pg import apply_migrations
    from nokker.tenancy.limits import PgUsage

    apply_migrations(DSN)
    apply_migrations(DSN)  # 011 rejouable
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DELETE FROM tenant_usage WHERE day = %s", (DAY,))
    usage = PgUsage(DSN)
    # Même ligne (même date), deux colonnes : le jour de quota qui commence ce jour-là et la semaine qui y commence.
    assert usage.take(A, "us_w", DAY, "turns", 5) and usage.take(A, "us_w", DAY, "week_turns", 1)
    assert not usage.take(A, "us_w", DAY, "week_turns", 1)
    assert (usage.read(A, "us_w", DAY, "turns"), usage.read(A, "us_w", DAY, "week_turns")) == (1, 1)
    usage.give_back(A, "us_w", DAY, "turns")
    usage.give_back(A, "us_w", DAY, "turns")  # jamais sous zéro
    assert usage.read(A, "us_w", DAY, "turns") == 0 and usage.read(B, "us_w", DAY, "week_turns") == 0
