"""Course de la rc.9.3 (Lokkal) : `ensure()` au démarrage tournait PENDANT les migrations du paquet `corthexis` (premier
`Store()` de la mémoire longue) — « column x.project does not exist » entre les migrations 10 et 11. Correction :
`ensure_managed` joue les migrations du paquet AVANT `ensure` ; et `ensure` sur un schéma pas encore migré (table sans
`project`) ne touche RIEN et rend False (rejoué), au lieu de ranger des lignes au locataire interne.
Postgres seulement (migrations du paquet simulées) ; ignoré sans `NOKKER_TEST_PG_DSN`. La course RÉELLE, avec le
paquet, est rejouée par `scripts/` hors CI (voir le message du commit)."""
from __future__ import annotations

import os
import threading
import time
import uuid

import pytest

DSN = os.environ.get("NOKKER_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="NOKKER_TEST_PG_DSN non posé — pas de Postgres jetable")

# `corthexis/schema.sql` (v1, sans `project`) et les migrations 0011/0012 (colonne `project`).
SCHEMA_V1 = [
    "CREATE TABLE notes (id serial PRIMARY KEY, name text UNIQUE, body text)",
    "CREATE TABLE links (src text, dst text)",
    "CREATE TABLE note_versions (id serial PRIMARY KEY, note_name text, body text)",
    "CREATE TABLE recall_log (id serial PRIMARY KEY, query text, note_name text)",
]
MIGRATION_PROJECT = [
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS project text NOT NULL DEFAULT 'default'",
    "ALTER TABLE links ADD COLUMN IF NOT EXISTS project text NOT NULL DEFAULT 'default'",
    "ALTER TABLE note_versions ADD COLUMN IF NOT EXISTS project text NOT NULL DEFAULT 'default'",
    "ALTER TABLE recall_log ADD COLUMN IF NOT EXISTS project text",
]


@pytest.fixture
def db():
    import psycopg
    from psycopg.conninfo import make_conninfo

    name = f"cx_race_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as su:
        su.execute(f'CREATE DATABASE "{name}"')
    yield make_conninfo(DSN, dbname=name)
    with psycopg.connect(DSN, autocommit=True) as su:
        su.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def _run(dsn, stmts):
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as con:
        for s in stmts:
            con.execute(s)


def _cols(dsn, table):
    import psycopg

    with psycopg.connect(dsn) as con:
        return {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                                          (table,))}


def test_old_base_with_user_notes_is_never_reattributed_before_its_migration(db, caplog):
    """(a) Base ANCIENNE (avant 0011) avec des notes de fils `s-…`, migrée au même démarrage : tant que `project`
    manque, rien n'est écrit (ni colonne tenant_id, ni rattachement) ; après la migration, chaque note va au locataire
    de SON fil, jamais au locataire interne."""
    import psycopg

    from nokker.context import corthexis_tenancy as ct

    _run(db, SCHEMA_V1)
    _run(db, ["INSERT INTO notes (name, body) VALUES ('n-alice', 'a'), ('n-bob', 'b')"])
    assert ct.ensure(db, lambda: {"s-alice": "tn_alice"}) is False
    assert "pas encore migré" in caplog.text and "ERROR" not in [r.levelname for r in caplog.records]
    assert "tenant_id" not in _cols(db, "notes")  # AUCUNE écriture
    _run(db, MIGRATION_PROJECT + ["UPDATE notes SET project = 's-alice' WHERE name = 'n-alice'",
                                  "UPDATE notes SET project = 's-bob' WHERE name = 'n-bob'"])
    assert ct.ensure(db, lambda: {"s-alice": "tn_alice"}) is True
    with psycopg.connect(db) as con:
        got = dict(con.execute("SELECT name, tenant_id FROM notes").fetchall())
    assert got == {"n-alice": "tn_alice", "n-bob": ct.ORPHAN}


def test_fresh_base_managed_start_has_no_error(db, caplog):
    """(b) Base neuve : `ensure_managed` migre d'abord (migrations simulées), puis pose locataire et RLS."""
    import psycopg

    from nokker.context import corthexis_tenancy as ct

    def migrate(dsn):
        _run(dsn, [s.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ") for s in SCHEMA_V1] + MIGRATION_PROJECT)

    assert ct.ensure_managed(db, None, migrate=migrate) is True
    assert "ERROR" not in [r.levelname for r in caplog.records]
    with psycopg.connect(db) as con:
        forced = con.execute("SELECT relforcerowsecurity FROM pg_class WHERE oid = 'notes'::regclass").fetchone()[0]
    assert forced and "tenant_id" in _cols(db, "notes")
    assert ct.ensure_managed(db, None, migrate=migrate) is True  # rejoué : rien à refaire


def test_concurrent_package_migrations_make_ensure_wait(db, caplog):
    """(c) Un autre `Store()` migre EN MÊME TEMPS (v1 posée, `project` 300 ms plus tard) en tenant le verrou des
    migrations ; `ensure_managed` passe par le même verrou (`migrate_package` → `Store.migrate`), donc attend la fin,
    puis pose locataire et RLS — jamais d'ERROR."""
    from nokker.context import corthexis_tenancy as ct

    migrations_lock = threading.Lock()  # le verrou consultatif du paquet, simulé
    started = threading.Event()

    def other_store():
        with migrations_lock:
            _run(db, SCHEMA_V1)
            started.set()
            time.sleep(0.3)
            _run(db, MIGRATION_PROJECT)

    def migrate(dsn):
        with migrations_lock:  # rien à jouer : l'autre a tout fait
            pass

    t = threading.Thread(target=other_store)
    t.start()
    started.wait(5)
    assert ct.ensure_managed(db, None, migrate=migrate) is True
    t.join()
    assert {"tenant_id", "project"} <= _cols(db, "notes")
    assert "ERROR" not in [r.levelname for r in caplog.records]
