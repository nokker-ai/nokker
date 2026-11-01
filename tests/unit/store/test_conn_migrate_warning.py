"""`migrate` sans les droits : l'avertissement sort une fois par processus, pas une fois par store."""
from __future__ import annotations

import logging

import psycopg
import pytest

from nokker.store import conn


@pytest.fixture
def no_privilege(monkeypatch):
    def refuse(*a, **k):
        raise psycopg.errors.InsufficientPrivilege("permission denied for schema public\ndétail")

    monkeypatch.setattr(conn, "connect_admin", refuse)
    monkeypatch.setattr(conn, "_warned_unmigrated", False)


def test_missing_privilege_warns_once_for_all_stores(no_privilege, caplog):
    caplog.set_level(logging.DEBUG, logger=conn.logger.name)
    assert [conn.migrate("postgresql://x", "SELECT 1", rls=False) for _ in range(3)] == [False] * 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "permission denied for schema public" in warnings[0].getMessage()
    assert sum(r.levelno == logging.DEBUG for r in caplog.records) == 2


def test_strict_still_raises(no_privilege):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.migrate("postgresql://x", "SELECT 1", rls=False, strict=True)
