"""Reconnexion de `TenantConn` (nokker/store/conn.py) sur un vrai Postgres, rôle `nokker_app`, connexion tuée par
`pg_terminate_backend` (ce que fait un redémarrage de Postgres managé) : `get()` rouvre UNE connexion de façon
transparente, jamais au milieu d'une transaction, jamais en rejouant un ordre d'écriture. Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import logging
import threading
import time

import pytest

from tests.integration.test_rls import A, B, pg, pod_env, requires_pg  # noqa: F401 — fixtures (base neuve, rôles)

pytestmark = pytest.mark.integration

LOG = "nokker.store.conn"
REOPENED = "connexion Postgres rétablie"


def _pid(db) -> int:
    return db._conn.info.backend_pid


def _kill(su_dsn: str, pid: int) -> None:
    import psycopg

    with psycopg.connect(su_dsn, autocommit=True) as su:
        assert su.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone()[0]
        for _ in range(100):  # le backend a disparu de pg_stat_activity
            if not su.execute("SELECT 1 FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone():
                return
            time.sleep(0.02)
    raise AssertionError("backend toujours là")


def _reopenings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOG and r.getMessage() == REOPENED]


@pytest.fixture
def scratch(pg):  # noqa: F811
    """Table d'essai sans RLS, écrite par `nokker_app` : de quoi compter les exécutions d'un ordre d'écriture."""
    import psycopg

    with psycopg.connect(pg["su"], autocommit=True) as su:
        su.execute("DROP TABLE IF EXISTS resil_t")
        su.execute("CREATE TABLE resil_t (k text PRIMARY KEY, tenant text DEFAULT current_setting('app.tenant_id'))")
        su.execute("GRANT ALL ON resil_t TO nokker_app")
    yield "resil_t"
    with psycopg.connect(pg["su"], autocommit=True) as su:
        su.execute("DROP TABLE IF EXISTS resil_t")


def _rows(dsns, table="resil_t") -> list[str]:
    import psycopg

    with psycopg.connect(dsns["su"], autocommit=True) as su:
        return sorted(r[0] for r in su.execute(f"SELECT k FROM {table}").fetchall())


# --- (1) get() reconnecte, pour trois familles de stores --------------------------------------------------------------

def _threads(dsn):
    from nokker.threads.pg import PgThreads
    from nokker.threads.types import Thread

    s = PgThreads(dsn=dsn)

    def op(i):
        s._store(Thread(id=f"th_{i}", title="t"))
        assert s._load(f"th_{i}").id == f"th_{i}"
    return s, op


def _bubbles(dsn):
    from nokker.bubbles.pg import PgBubbles

    s = PgBubbles(dsn=dsn)

    def op(i):
        b = s.create(thread_id=f"th_{i}", kind="timer", title="t")
        assert s.get(b.id) is not None
    return s, op


def _approvals(dsn):
    from nokker.approvals.pg import PgApprovals

    s = PgApprovals(dsn=dsn)

    def op(i):
        assert s._list_pending_approvals(None) == []
    return s, op


@requires_pg
@pytest.mark.parametrize("make", [_threads, _bubbles, _approvals], ids=["threads", "bubbles", "approvals"])
def test_killed_connection_is_reopened_transparently(pg, pod_env, make, caplog):  # noqa: F811
    from nokker.tenancy.context import tenant_scope

    store, op = make(pg["app"])
    caplog.set_level(logging.INFO, logger=LOG)
    try:
        with tenant_scope(A):
            op(0)
            old = _pid(store._db)
            _kill(pg["su"], old)
            op(1)  # get() : set_config échoue sur la connexion morte -> reconnexion, locataire reposé, l'ordre passe
            new = _pid(store._db)
            assert new != old
            _kill(pg["su"], new)  # une seconde fois : la reconnexion n'est pas un coup unique
            op(2)
        assert len(_reopenings(caplog)) == 2
        from psycopg.conninfo import conninfo_to_dict

        secret = conninfo_to_dict(pg["app"])["password"]
        for r in caplog.records:
            assert secret not in r.getMessage() and pg["app"] not in r.getMessage()  # ni mot de passe ni DSN dans le journal
    finally:
        store.close()


@requires_pg
def test_tenant_is_reposed_on_the_new_connection(pg, pod_env):  # noqa: F811
    from nokker.store.conn import TenantConn
    from nokker.tenancy.context import tenant_scope

    tc = TenantConn(pg["app"])
    try:
        with tenant_scope(A):
            assert tc.get().execute("SELECT current_setting('app.tenant_id')").fetchone()[0] == A
        _kill(pg["su"], _pid(tc))
        with tenant_scope(B):  # connexion neuve : le locataire du get() qui la rouvre, pas celui d'avant
            assert tc.get().execute("SELECT current_setting('app.tenant_id')").fetchone()[0] == B
    finally:
        tc.close()


@requires_pg
def test_dead_connection_is_not_kept_after_an_error_reaches_the_caller(pg, pod_env, scratch, caplog):  # noqa: F811
    """Tuée PENDANT l'usage (ordre en autocommit) : l'erreur remonte telle quelle, l'ordre n'est pas rejoué ;
    le get() suivant rouvre."""
    import psycopg

    from nokker.store.conn import TenantConn
    from nokker.tenancy.context import tenant_scope

    caplog.set_level(logging.INFO, logger=LOG)
    tc = TenantConn(pg["app"])
    try:
        with tenant_scope(A):
            conn = tc.get()
            _kill(pg["su"], _pid(tc))
            with pytest.raises((psycopg.OperationalError, psycopg.InterfaceError)):
                conn.execute("INSERT INTO resil_t (k) VALUES ('once')")  # l'appelant tient déjà la connexion
            assert _rows(pg) == []  # ni exécuté, ni rejoué par un autre chemin
            assert _reopenings(caplog) == []
            tc.get().execute("INSERT INTO resil_t (k) VALUES ('after')")
            assert _reopenings(caplog) == [REOPENED]
        assert _rows(pg) == ["after"]
    finally:
        tc.close()


# --- (2) connexion tuée en milieu de transaction explicite : pas de rejeu --------------------------------------------

@requires_pg
def test_kill_in_the_middle_of_a_transaction_is_never_replayed(pg, pod_env, scratch, caplog):  # noqa: F811
    import psycopg

    from nokker.store.conn import TenantConn
    from nokker.tenancy.context import tenant_scope

    caplog.set_level(logging.INFO, logger=LOG)
    tc = TenantConn(pg["app"])
    try:
        with tenant_scope(A):
            conn = tc.get()
            with pytest.raises((psycopg.OperationalError, psycopg.InterfaceError)):
                with conn.transaction():
                    conn.execute("INSERT INTO resil_t (k) VALUES ('t1')")  # premier ordre d'écriture de la transaction
                    _kill(pg["su"], _pid(tc))
                    tc.get()  # get() EN COURS de transaction : ne reconnecte pas, l'erreur remonte
                    conn.execute("INSERT INTO resil_t (k) VALUES ('t2')")  # inatteignable
            assert _rows(pg) == []  # la transaction n'a pas été committée, rien n'a été rejoué sur une autre connexion
            assert _reopenings(caplog) == []
            tc.get().execute("INSERT INTO resil_t (k) VALUES ('t3')")  # transaction finie : le prochain get() rouvre
        assert _rows(pg) == ["t3"]
        assert len(_reopenings(caplog)) == 1
    finally:
        tc.close()


@requires_pg
def test_kill_before_the_write_of_a_transaction_surfaces_on_the_statement(pg, pod_env, scratch):  # noqa: F811
    """Connexion morte entre le get() et l'ordre : l'ordre échoue (il n'est jamais rejoué), pas de ligne."""
    import psycopg

    from nokker.store.conn import TenantConn
    from nokker.tenancy.context import tenant_scope

    tc = TenantConn(pg["app"])
    try:
        with tenant_scope(A):
            conn = tc.get()
            _kill(pg["su"], _pid(tc))
            with pytest.raises((psycopg.OperationalError, psycopg.InterfaceError)), conn.transaction():
                conn.execute("INSERT INTO resil_t (k) VALUES ('x')")
        assert _rows(pg) == []
    finally:
        tc.close()


# --- (3) la reconnexion échoue : l'erreur remonte, une seule tentative, pas de boucle --------------------------------

@requires_pg
def test_failed_reconnection_raises_once_and_recovers_later(pg, pod_env, monkeypatch):  # noqa: F811
    import psycopg

    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from nokker.store import conn as c
    from nokker.tenancy.context import tenant_scope

    tc = c.TenantConn(pg["app"])
    attempts: list[str] = []
    real = c._open

    def counting(dsn, kw):
        attempts.append(dsn)
        return real(dsn, kw)

    try:
        with tenant_scope(A):
            tc.get()
            _kill(pg["su"], _pid(tc))
            good = tc._dsn
            tc._dsn = make_conninfo(good, port=1, connect_timeout=2)  # ...maintenant : port sans écoute (base arrêtée)
            assert conninfo_to_dict(tc._dsn)["port"] == "1"
            monkeypatch.setattr(c, "_open", counting)
            t0 = time.monotonic()
            with pytest.raises(psycopg.OperationalError):
                tc.get()
            assert len(attempts) == 1 and time.monotonic() - t0 < 10  # UNE tentative par get(), pas de boucle
            with pytest.raises(psycopg.OperationalError):
                tc.get()
            assert len(attempts) == 2  # la connexion cassée n'est pas rendue : chaque get() retente, une fois
            tc._dsn = good  # la base revient
            assert tc.get().execute("SELECT 1").fetchone() == (1,)
            assert len(attempts) == 3
    finally:
        tc.close()


# --- (6) fonctionnement normal inchangé ------------------------------------------------------------------------------

@requires_pg
def test_normal_operation_is_unchanged(pg, pod_env, caplog):  # noqa: F811
    import psycopg

    from nokker.store.conn import TenantConn
    from nokker.tenancy.context import NoTenant, tenant_scope

    caplog.set_level(logging.INFO, logger=LOG)
    tc = TenantConn(pg["app"])
    try:
        with pytest.raises(NoTenant):  # hors scope : avant toute E/S
            tc.get()
        pid = _pid(tc)
        seen = set()
        for t in (A, B, A, B, A):
            with tenant_scope(t):
                conn = tc.get()
                assert conn.autocommit
                seen.add(id(conn))
                assert conn.execute("SELECT current_setting('app.tenant_id')").fetchone()[0] == t
        assert len(seen) == 1 and _pid(tc) == pid  # même connexion, pas de reconnexion
        assert _reopenings(caplog) == []
        with tenant_scope(A):
            with pytest.raises(psycopg.errors.UndefinedTable):  # une erreur SQL d'une connexion vivante remonte telle quelle
                tc.get().execute("SELECT * FROM table_inexistante_resil")
            assert tc.get() is tc._conn and _pid(tc) == pid  # et ne coûte pas la connexion
        tc.close()
        with tenant_scope(A), pytest.raises((psycopg.OperationalError, psycopg.InterfaceError)):  # close() : pas de résurrection
            tc.get()
        assert _reopenings(caplog) == []
    finally:
        tc.close()


# --- (5) concurrence : 8 fils partageant un store pendant des terminaisons ------------------------------------------

@requires_pg
def test_eight_threads_sharing_a_store_during_terminations(pg, pod_env, caplog):  # noqa: F811
    import psycopg

    from nokker.tenancy.context import tenant_scope
    from nokker.threads.pg import PgThreads

    caplog.set_level(logging.INFO, logger=LOG)
    store = PgThreads(dsn=pg["app"])
    per_thread, n_threads = 25, 8
    ok: list[str] = []
    failed: list[str] = []
    unexpected: list[BaseException] = []
    guard = threading.Lock()
    stop = threading.Event()

    def worker(w: int) -> None:
        with tenant_scope(A):
            for i in range(per_thread):
                block = f"w{w}-{i}"
                try:
                    store._push(f"th_{w}", block)
                except (psycopg.OperationalError, psycopg.InterfaceError):
                    with guard:
                        failed.append(block)  # remonté proprement : l'écriture a pu partir ou non
                except BaseException as e:  # noqa: BLE001
                    with guard:
                        unexpected.append(e)
                else:
                    with guard:
                        ok.append(block)
                time.sleep(0.005)

    def killer() -> None:
        while not stop.is_set():
            time.sleep(0.05)
            try:
                _kill(pg["su"], _pid(store._db))
            except Exception:  # noqa: BLE001 — déjà reconnecté ailleurs ou en cours
                pass

    try:
        threads = [threading.Thread(target=worker, args=(w,)) for w in range(n_threads)]
        k = threading.Thread(target=killer)
        k.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        stop.set()
        k.join(10)
        assert not any(t.is_alive() for t in threads)
        assert not unexpected, unexpected
        assert len(ok) + len(failed) == n_threads * per_thread
        assert ok, "aucune écriture n'a abouti"
        assert len(_reopenings(caplog)) >= 1  # des terminaisons ont bien eu lieu
        with tenant_scope(A):
            store._push("th_final", "final")  # la connexion est à nouveau saine
        import psycopg as pg3

        with pg3.connect(pg["su"], autocommit=True) as su:
            rows = [r[0] for r in su.execute("SELECT block FROM nokker_thread_inbox WHERE tenant_id = %s", (A,))]
        stored = [b for b in rows if b != "final"]
        assert len(stored) == len(set(stored)), "ordre rejoué : bloc en double"  # jamais deux fois le même ordre
        assert set(ok) <= set(stored)  # tout succès rendu à l'appelant est bien en base, une seule fois
        assert set(stored) <= set(ok) | set(failed)
    finally:
        stop.set()
        store.close()
