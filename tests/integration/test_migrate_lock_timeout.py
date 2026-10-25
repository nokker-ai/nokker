"""`NOKKER_MIGRATE_LOCK_TIMEOUT` (nokker/store/conn.py::migrate) : un job de migration qui attend un verrou tenu par la
bêta en service abandonne au lieu de bloquer derrière lui toutes les requêtes. Vrai Postgres (`NOKKER_TEST_PG_DSN`)."""
from __future__ import annotations

import os
import threading
import uuid

import psycopg
import pytest

from nokker.store.conn import migrate

DSN = os.environ.get("NOKKER_TEST_PG_DSN")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not DSN, reason="NOKKER_TEST_PG_DSN absent")]


def test_lock_timeout_aborts_a_blocked_migration(monkeypatch):
    table = f"lt_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(f"CREATE TABLE {table} (id int)")
    held, release = threading.Event(), threading.Event()

    def hold() -> None:  # une requête de la bêta qui tient la table
        with psycopg.connect(DSN) as c:
            c.execute(f"LOCK TABLE {table} IN ACCESS SHARE MODE")
            held.set()
            release.wait(10)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert held.wait(5)
    try:
        monkeypatch.setenv("NOKKER_MIGRATE_LOCK_TIMEOUT", "300ms")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            migrate(DSN, f"ALTER TABLE {table} ADD COLUMN x int", rls=False, strict=True)
    finally:
        release.set()
        t.join(5)
    # verrou rendu : la même migration passe
    assert migrate(DSN, f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS x int", rls=False, strict=True)


def test_unreadable_lock_timeout_is_refused(monkeypatch):
    monkeypatch.setenv("NOKKER_MIGRATE_LOCK_TIMEOUT", "5s; DROP TABLE x")
    with pytest.raises(ValueError):
        migrate(DSN, "SELECT 1", rls=False, strict=True)
