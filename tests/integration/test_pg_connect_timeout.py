"""Délai de connexion Postgres (nokker/store/conn.py) : 10 s par défaut sur toute ouverture, réglable, inchangé quand
l'appelant ou le DSN le fixent déjà. Sans Postgres : `psycopg.connect` est espionné, puis une vraie tentative vers une
adresse non routable (DSN sans écoute) doit échouer vite."""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.integration

DSN = "postgresql://u:p@db.invalid:5432/x"
CALLERS = ("connect", "connect_tenant", "connect_key", "connect_admin")


@pytest.fixture
def seen(monkeypatch):
    import psycopg

    for var in ("NOKKER_PG_CONNECT_TIMEOUT", "PGCONNECT_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    calls: list[dict] = []

    class Stop(Exception):
        pass

    def fake(dsn, **kw):
        calls.append(kw)
        raise Stop

    monkeypatch.setattr(psycopg, "connect", fake)
    return calls, Stop


def _call(name: str, dsn: str, **kw):
    from nokker.store import conn as c
    from nokker.tenancy.context import tenant_scope

    with tenant_scope("tn_t1"):
        if name == "connect":
            return c.connect(dsn, **kw)
        if name == "connect_tenant":
            return c.connect_tenant(dsn, "tn_t1", **kw)
        if name == "connect_key":
            return c.connect_key(dsn, "k", **kw)
        if name == "connect_admin":
            return c.connect_admin(dsn, **kw)
        return c.TenantConn(dsn, **kw)


@pytest.mark.parametrize("name", [*CALLERS, "TenantConn"])
def test_default_is_10_seconds(seen, name):
    calls, stop = seen
    with pytest.raises(stop):
        _call(name, DSN)
    assert calls[0]["connect_timeout"] == 10


@pytest.mark.parametrize("name", [*CALLERS, "TenantConn"])
def test_argument_and_dsn_win(seen, name):
    calls, stop = seen
    with pytest.raises(stop):
        _call(name, DSN, connect_timeout=3)
    assert calls[-1]["connect_timeout"] == 3
    with pytest.raises(stop):
        _call(name, DSN + "?connect_timeout=7")
    assert "connect_timeout" not in calls[-1]  # le DSN garde la main, rien n'est ajouté


def test_env_override_and_libpq_env(seen, monkeypatch):
    calls, stop = seen
    monkeypatch.setenv("NOKKER_PG_CONNECT_TIMEOUT", "4")
    with pytest.raises(stop):
        _call("connect_admin", DSN)
    assert calls[-1]["connect_timeout"] == 4
    monkeypatch.setenv("NOKKER_PG_CONNECT_TIMEOUT", "n'importe quoi")
    with pytest.raises(stop):
        _call("connect_admin", DSN)
    assert calls[-1]["connect_timeout"] == 10
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "2")
    with pytest.raises(stop):
        _call("connect_admin", DSN)
    assert "connect_timeout" not in calls[-1]


def test_other_kwargs_untouched(seen):
    calls, stop = seen
    with pytest.raises(stop):
        _call("connect_admin", DSN, autocommit=True)
    assert calls[-1] == {"autocommit": True, "connect_timeout": 10}


@pytest.mark.parametrize("name", CALLERS)
def test_unreachable_host_fails_fast(name):
    """Adresse non routable (blackhole) : l'échec arrive au délai injecté (1 s), pas au délai TCP du noyau."""
    import psycopg

    t0 = time.monotonic()
    with pytest.raises(psycopg.OperationalError):
        _call(name, "postgresql://u:p@10.255.255.1:5432/x", connect_timeout=1)
    assert time.monotonic() - t0 < 15
