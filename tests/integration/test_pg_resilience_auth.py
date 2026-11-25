"""Connexion d'authentification de `PgApprovals` (`_auth`, hors locataire) : même reconnexion à l'acquisition que
`TenantConn` (`AdminConn`, nokker/store/conn.py), sur un vrai Postgres, connexion tuée par `pg_terminate_backend`.
Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import logging
import time

import pytest

from tests.integration.test_pg_resilience import LOG, REOPENED, _kill, _reopenings  # noqa: F401
from tests.integration.test_rls import pg, pod_env, requires_pg  # noqa: F401 — fixtures (base neuve, rôles)

pytestmark = pytest.mark.integration


def _pid(store) -> int:
    return store._auth_db._conn.info.backend_pid


@requires_pg
def test_killed_auth_connection_is_reopened_and_lookups_succeed(pg, pod_env, caplog):  # noqa: F811
    from nokker.approvals.pg import PgApprovals
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    caplog.set_level(logging.INFO, logger=LOG)
    store = PgApprovals(dsn=pg["app"])
    try:
        assert store._load_totp("ada") is None  # lookup d'authentification, hors de tout scope
        old = _pid(store)
        _kill(pg["su"], old)
        assert store._load_totp("ada") is None  # get() : SELECT 1 échoue -> reconnexion, la requête réussit
        assert _pid(store) != old
        exp = time.time() + 60
        _kill(pg["su"], _pid(store))
        store._reserve_nonce("n-1", "scope", exp)  # écriture d'authentification après une terminaison : réussit
        assert store._consume_nonce("n-1", "scope") is True
        assert len(_reopenings(caplog)) == 2
        assert pg["app"] not in caplog.text
        with tenant_scope(INTERNAL_TENANT):  # la connexion à locataire n'est pas touchée par la reconnexion d'`_auth`
            assert store._list_pending_approvals(None) == []
    finally:
        store.close()


@requires_pg
def test_auth_killed_in_use_surfaces_without_replay_then_next_call_reopens(pg, pod_env, caplog):  # noqa: F811
    import psycopg

    from nokker.approvals.pg import PgApprovals

    caplog.set_level(logging.INFO, logger=LOG)
    store = PgApprovals(dsn=pg["app"])
    try:
        conn = store._auth
        _kill(pg["su"], _pid(store))
        with pytest.raises((psycopg.OperationalError, psycopg.InterfaceError)):
            conn.execute("INSERT INTO used_nonces (nonce, scope_id, exp, used) VALUES ('n-use', 's', 9999999999, FALSE)")
        assert _reopenings(caplog) == []  # rien rejoué
        assert store._consume_nonce("n-use", "s") is False  # l'ordre interrompu n'a pas eu lieu ; ce get() rouvre
        assert _reopenings(caplog) == [REOPENED]
    finally:
        store.close()


@requires_pg
def test_auth_close_does_not_resurrect(pg, pod_env):  # noqa: F811
    import psycopg

    from nokker.approvals.pg import PgApprovals

    store = PgApprovals(dsn=pg["app"])
    store.close()
    with pytest.raises((psycopg.OperationalError, psycopg.InterfaceError)):
        store._load_totp("ada")
