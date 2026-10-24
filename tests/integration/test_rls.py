"""Isolation des locataires en base (docs/TENANCY.md §4, §6) : vrai Postgres, rôles de deploy/postgres/roles.sql, le
noyau connecté en `nokker_app` (ni propriétaire, ni BYPASSRLS). Ignoré sans `NOKKER_TEST_PG_DSN` (superutilisateur
d'un Postgres jetable) ; chaque exécution crée sa propre base, détruite à la fin.

Prouve : une ligne d'un locataire est invisible à un autre sur chaque table de contenu, une écriture qui force le
locataire d'un autre est refusée, sans locataire on ne lit ni n'écrit rien, les lignes d'avant la migration
appartiennent à l'interne, les vrais stores du noyau passent A puis B sans fuite, le balayeur de fond passe sur chaque
locataire, et une migration sans droits (pod `nokker_app`) n'empêche pas le démarrage."""
from __future__ import annotations

import logging
import os
import secrets
import time
import uuid
from pathlib import Path

import pytest

DSN = os.environ.get("NOKKER_TEST_PG_DSN")

pytestmark = pytest.mark.integration
requires_pg = pytest.mark.skipif(not DSN, reason="NOKKER_TEST_PG_DSN non posé — pas de Postgres jetable")

ROLES_SQL = Path(__file__).resolve().parents[2] / "deploy" / "postgres" / "roles.sql"
A, B = "tn_aaaaaaaaaaaa", "tn_bbbbbbbbbbbb"

# docs/TENANCY.md §4 ; `connector_credentials` et `terms_acceptances` viennent de 005 (autre session) : couvertes si
# elles existent (voir test_every_table_is_covered).
CONTENT_TABLES = [
    "threads", "turns", "nokker_threads", "nokker_thread_inbox", "nokker_bubbles",
    "nokker_episodes", "nokker_day_summaries", "nokker_view_state", "nokker_reservoir",
    "approvals", "approval_events",
    "ledger_entries", "ledger_verdicts", "llm_calls", "verdicts", "nokker_agents",
]
OUT_OF_RLS = {"tenants", "accounts", "account_identities", "telegram_pairing", "operators", "admin_audit", "beta_applications",
              "devices", "sessions", "totp_secrets", "used_nonces", "auth_email_codes",  # authentification : lues avant le locataire
              "lokkal_machines", "lokkal_link_requests"}  # lien Lokkal (014) : cherchées par empreinte de jeton ou par code


@pytest.fixture(scope="module")
def pg():
    """Base neuve : rôles (étape 1), migrations en nokker_owner (étape 3) avec une ligne d'avant la RLS, rôles
    rejoués (étape 4). Rend les DSN propriétaire et applicatif."""
    import psycopg
    from psycopg.conninfo import make_conninfo

    from nokker.store.conn import migrate
    from nokker.store.pg import migration_files

    db = f"nokker_rls_{uuid.uuid4().hex[:8]}"
    pw_owner, pw_app = secrets.token_hex(12), secrets.token_hex(12)
    with psycopg.connect(DSN, autocommit=True) as su:
        su.execute(f'CREATE DATABASE "{db}"')
    su_db = make_conninfo(DSN, dbname=db)
    with psycopg.connect(su_db, autocommit=True) as su:
        su.execute(ROLES_SQL.read_text(encoding="utf-8"))
        su.execute(f"ALTER ROLE nokker_owner PASSWORD '{pw_owner}'")
        su.execute(f"ALTER ROLE nokker_app PASSWORD '{pw_app}'")
    owner = make_conninfo(DSN, dbname=db, user="nokker_owner", password=pw_owner)
    app = make_conninfo(DSN, dbname=db, user="nokker_app", password=pw_app)

    files = migration_files()
    rls_at = [f.name for f in files].index("006_tenant_rls.sql")  # les migrations suivantes (007…) viennent après
    # canari d'avant la bêta : tables créées, une ligne écrite, PUIS la RLS
    migrate(owner, *(p.read_text(encoding="utf-8") for p in files[:rls_at]), rls=False, strict=True)
    with psycopg.connect(owner, autocommit=True) as conn:
        conn.execute("INSERT INTO threads (thread_id) VALUES ('legacy-thread')")
        conn.execute("INSERT INTO turns (thread_id, turn_index, turn) VALUES ('legacy-thread', 0, '{}')")
    from nokker.store.pg import apply_migrations

    assert apply_migrations(owner, strict=True)
    assert apply_migrations(owner, strict=True)  # rejouée à chaque démarrage
    with psycopg.connect(su_db, autocommit=True) as su:
        su.execute(ROLES_SQL.read_text(encoding="utf-8"))
        su.execute("INSERT INTO tenants (id) VALUES (%s), (%s)", (A, B))
    yield {"owner": owner, "app": app, "su": su_db}
    with psycopg.connect(DSN, autocommit=True) as su:
        su.execute(f'DROP DATABASE "{db}" WITH (FORCE)')


@pytest.fixture
def pod_env(monkeypatch):
    """Le pod applicatif de la bêta : seulement le DSN nokker_app (pas de NOKKER_PG_OWNER_DSN)."""
    monkeypatch.delenv("NOKKER_PG_OWNER_DSN", raising=False)


def _dummy_row(conn, table: str) -> tuple[list[str], list]:
    """Colonnes obligatoires sans défaut et des valeurs uniques (clés primaires, unicités)."""
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = %s AND is_nullable = 'NO' AND column_default IS NULL ORDER BY ordinal_position",
        (table,)).fetchall()
    from psycopg.types.json import Jsonb

    vals = {"text": lambda: uuid.uuid4().hex, "integer": lambda: secrets.randbelow(10**6),
            "bigint": lambda: secrets.randbelow(10**9), "double precision": lambda: 1.0,
            "boolean": lambda: False, "jsonb": lambda: Jsonb({})}
    return [c for c, _ in rows], [vals[t]() for _, t in rows]


def _insert(conn, table: str, tenant: str | None = None) -> None:
    cols, vals = _dummy_row(conn, table)
    if table == "turns":  # clé étrangère vers threads (même locataire)
        th = uuid.uuid4().hex
        conn.execute("INSERT INTO threads (thread_id) VALUES (%s)", (th,))
        vals[cols.index("thread_id")] = th
    if tenant is not None:
        cols, vals = [*cols, "tenant_id"], [*vals, tenant]
    names = ", ".join(f'"{c}"' for c in cols)
    conn.execute(f"INSERT INTO {table} ({names}) VALUES ({', '.join(['%s'] * len(vals))})", vals)


@requires_pg
def test_connect_without_scope_raises_notenant(pg):
    from nokker.store.conn import TenantConn, connect
    from nokker.tenancy.context import NoTenant

    with pytest.raises(NoTenant):
        connect(pg["app"])
    tc = TenantConn(pg["app"])
    try:
        with pytest.raises(NoTenant):
            tc.get()
    finally:
        tc.close()


@requires_pg
@pytest.mark.parametrize("table", CONTENT_TABLES)
def test_row_of_a_invisible_to_b(pg, table):
    import psycopg

    from nokker.store.conn import connect
    from nokker.tenancy.context import tenant_scope

    for tid in (A, B):
        with tenant_scope(tid), connect(pg["app"], autocommit=True) as conn:
            _insert(conn, table)
    for tid in (A, B):
        with tenant_scope(tid), connect(pg["app"], autocommit=True) as conn:
            seen = {r[0] for r in conn.execute(f"SELECT tenant_id FROM {table}").fetchall()}
            assert seen == {tid}, f"{table} : {tid} voit {seen}"
            other = A if tid == B else B
            # ni modification ni suppression des lignes de l'autre
            assert conn.execute(f"UPDATE {table} SET tenant_id = tenant_id WHERE tenant_id = %s",
                                (other,)).rowcount == 0
            if table != "turns":  # append-only (trigger) : aucune ligne visible, donc rien à refuser
                assert conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (other,)).rowcount == 0
    # B qui écrit une ligne au nom de A : refusé par WITH CHECK
    with tenant_scope(B), connect(pg["app"], autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _insert(conn, table, tenant=A)
        # B qui déplace sa propre ligne chez A : refusé aussi
        with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.RaiseException)):
            conn.execute(f"UPDATE {table} SET tenant_id = %s", (A,))


@requires_pg
@pytest.mark.parametrize("table", CONTENT_TABLES)
def test_no_tenant_reads_and_writes_nothing(pg, table):
    """Connexion nokker_app sans `app.tenant_id` (connect_admin) : aucune ligne, aucune écriture."""
    import psycopg

    from nokker.store.conn import connect_admin

    with connect_admin(pg["app"], autocommit=True) as conn:
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        with pytest.raises((psycopg.errors.UndefinedObject, psycopg.errors.InsufficientPrivilege)):
            _insert(conn, table)
        # même avec un locataire vide
        conn.execute("SELECT set_config('app.tenant_id', '', false)")
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


@requires_pg
def test_every_table_is_covered(pg):
    """Toute table du schéma est soit sous RLS forcée avec la politique, soit hors RLS par conception (§4) : une
    table de contenu ajoutée sans passer par 006 fait échouer ce test."""
    import psycopg

    with psycopg.connect(pg["su"]) as conn:
        rows = conn.execute(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
            "EXISTS (SELECT 1 FROM pg_policies p WHERE p.schemaname = n.nspname AND p.tablename = c.relname "
            "AND p.policyname = 'tenant_isolation'), "
            "EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'tenant_id') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r'").fetchall()
        app_attrs = conn.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'nokker_app'").fetchone()
        owners = {r[0] for r in conn.execute(
            "SELECT tableowner FROM pg_tables WHERE schemaname = 'public'").fetchall()}
    assert app_attrs == (False, False)
    assert owners == {"nokker_owner"}
    tables = {r[0]: r[1:] for r in rows}
    assert set(CONTENT_TABLES) <= set(tables)
    for name, (rls, force, policy, has_col) in tables.items():
        if name in OUT_OF_RLS:
            assert not rls, f"{name} est hors RLS par conception"
            continue
        assert rls and force and policy and has_col, f"{name} : table de contenu sans RLS complète"


@requires_pg
def test_legacy_rows_belong_to_internal(pg):
    from nokker.store.conn import connect
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    with tenant_scope(INTERNAL_TENANT), connect(pg["app"]) as conn:
        assert conn.execute("SELECT count(*) FROM turns WHERE thread_id = 'legacy-thread'").fetchone()[0] == 1
    with tenant_scope(A), connect(pg["app"]) as conn:
        assert conn.execute("SELECT count(*) FROM turns WHERE thread_id = 'legacy-thread'").fetchone()[0] == 0


@requires_pg
def test_pod_without_owner_dsn_starts(pg, pod_env, caplog):
    """Pod `nokker_app` sans DSN propriétaire : migration impossible, log clair, le store démarre et sert."""
    import psycopg

    from nokker.store.pg import PgStore, apply_migrations
    from nokker.tenancy.context import tenant_scope

    with caplog.at_level(logging.WARNING, logger="nokker.store.conn"):
        assert apply_migrations(pg["app"]) is False
    assert "NOKKER_PG_OWNER_DSN" in caplog.text
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        apply_migrations(pg["app"], strict=True)  # `nokker migrate` (job d'init) échoue franchement
    store = PgStore(pg["app"])
    with tenant_scope(A):
        assert store.history("tn_aaaaaaaaaaaa:rien") == []
    store.close()


@requires_pg
def test_owner_dsn_env_used_for_migrations(pg, monkeypatch):
    from nokker.store.pg import apply_migrations

    monkeypatch.setenv("NOKKER_PG_OWNER_DSN", pg["owner"])
    assert apply_migrations(pg["app"], strict=True) is True


@requires_pg
def test_real_stores_a_then_b(pg, pod_env):
    from nokker.agents.pg import PgAgents
    from nokker.approvals.pg import PgApprovals
    from nokker.bubbles.pg import PgBubbles
    from nokker.context.pg import PgEpisodeStore, PgReservoirStore
    from nokker.context.types import Episode, ReservoirItem
    from nokker.core.turn import Decision, Gate, InputCheck, LLMCall, Tier, ToolCall, Turn, Usage, Verdict
    from nokker.ledger.costs import PostgresLedger
    from nokker.store.pg import PgStore
    from nokker.tenancy.context import scoped_thread_id, tenant_scope
    from nokker.threads.pg import PgThreads

    app = pg["app"]
    store, episodes, reservoir = PgStore(app), PgEpisodeStore(app), PgReservoirStore(app)
    approvals, threads, bubbles, agents = PgApprovals(dsn=app), PgThreads(dsn=app), PgBubbles(dsn=app), PgAgents(dsn=app)
    ledger = PostgresLedger(app)
    ids: dict[str, dict[str, str]] = {}
    try:
        for tid in (A, B):
            with tenant_scope(tid):
                th = scoped_thread_id(tid, "same-session")  # même X-Session-Id chez A et B
                turn = Turn(thread_id=th, index=store.next_index(th), input=InputCheck(text=f"secret de {tid}"))
                call = LLMCall(tier=Tier.SHIP, model="m", reasoning=False, prefix_sha256="x", layer_tokens={},
                               usage=Usage(prompt=1), latency_s=0.1, route_reason="test")
                verdict = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="r")
                turn.calls, turn.verdicts = [call], [verdict]
                store.append(turn)
                ep = Episode(id=f"ep-{tid}", thread_id=th, seq=0, first_turn=0)
                episodes.save(ep)
                reservoir.put(ReservoirItem(id=f"rs-{tid}", thread_id=th, kind="tool_result", content="x"))
                appr = approvals.request(turn, ToolCall(id="c1", name="send_mail", arguments={"to": tid}),
                                         verdict, None)
                t = threads.create("fil", f"us_{tid[3:]}", thread_id=th)
                threads.push_context(th, f"bloc {tid}")
                bub = bubbles.create(thread_id=th, kind="monitor", title=tid)
                ag = agents.create(name=tid, role="r", model="m")
                ledger.record(th, 0, call)
                ledger.record_verdict(th, 0, verdict)
                ids[tid] = {"th": th, "appr": appr.id, "thread": t.id, "bub": bub.id, "ag": ag.id}
        assert ids[A]["th"] != ids[B]["th"]
        for tid, other in ((A, B), (B, A)):
            mine, theirs = ids[tid], ids[other]
            with tenant_scope(tid):
                assert [t.input.text for t in store.history(mine["th"])] == [f"secret de {tid}"]
                assert store.history(theirs["th"]) == [] and store.history_from(theirs["th"], 0) == []
                assert episodes.get(f"ep-{other}") is None and episodes.list(theirs["th"]) == []
                assert reservoir.get(f"rs-{other}") is None and reservoir.list(theirs["th"]) == []
                assert approvals.get(theirs["appr"]) is None
                pend = {a.id for a in approvals.pending()}
                assert mine["appr"] in pend and theirs["appr"] not in pend
                assert threads.get(theirs["thread"]) is None
                assert theirs["thread"] not in {t.id for t in threads.list_for(None)}
                assert threads.drain_context(theirs["th"]) == []
                assert bubbles.get(theirs["bub"]) is None
                assert [b.id for b in bubbles.list_thread(mine["th"])] == [mine["bub"]]
                ags = {a.id for a in agents.list_all()}
                assert mine["ag"] in ags and theirs["ag"] not in ags
                assert theirs["th"] not in {c.thread_id for c in ledger.fetch_calls()}
                assert theirs["th"] not in {v.thread_id for v in ledger.fetch_verdicts()}
                assert ledger.aggregates(thread_id=mine["th"]).n_calls == 1
                assert ledger.aggregates(thread_id=theirs["th"]).n_calls == 0
            # la même chose en SQL brut, rôle nokker_app, app.tenant_id = tid
            from nokker.store.conn import connect

            with tenant_scope(tid), connect(pg["app"]) as conn:
                for table in CONTENT_TABLES:
                    n = conn.execute(f"SELECT count(*) FROM {table} WHERE tenant_id <> %s", (tid,)).fetchone()[0]
                    assert n == 0, table
    finally:
        for s in (store, episodes, reservoir, approvals, threads, bubbles, agents):
            s.close()


@requires_pg
def test_background_sweep_visits_each_tenant(pg, pod_env):
    """Balayeur de fond (hors requête, hors scope) : un passage par locataire, chacun sous sa RLS."""
    from nokker.bubbles.hub import BubbleHub
    from nokker.bubbles.pg import PgBubbles
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
    from nokker.threads.pg import PgThreads

    bubbles, threads = PgBubbles(dsn=pg["app"]), PgThreads(dsn=pg["app"])
    hub = BubbleHub(threads=threads, bubbles=bubbles)
    try:
        made = {}
        for tid in (INTERNAL_TENANT, A, B):
            with tenant_scope(tid):
                made[tid] = bubbles.create(thread_id=None, kind="notify", title=tid).id
        hub.clock = lambda: time.time() + 3600
        assert hub.sweep_expired() >= 3
        for tid, bid in made.items():
            with tenant_scope(tid):
                assert bubbles.get(bid).closed_at is not None
        with tenant_scope(A):  # dans une requête : seulement le locataire courant
            mine = bubbles.create(thread_id=None, kind="notify", title="a").id
        with tenant_scope(B):
            theirs = bubbles.create(thread_id=None, kind="notify", title="b").id
            assert hub.sweep_expired() == 1
        with tenant_scope(A):
            assert bubbles.get(mine).closed_at is None
        with tenant_scope(B):
            assert bubbles.get(theirs).closed_at is not None
    finally:
        bubbles.close()
        threads.close()


@requires_pg
def test_auth_tables_work_without_tenant(pg, pod_env):
    """devices, sessions, totp_secrets, used_nonces : hors RLS, lues avant de connaître le locataire (verify_session,
    login_challenge). Même rôle nokker_app, aucune portée nécessaire ; approvals reste sous RLS."""
    from nokker.approvals.pg import PgApprovals
    from nokker.approvals.types import Session
    from nokker.tenancy.context import NoTenant, tenant_scope

    approvals = PgApprovals(dsn=pg["app"])
    try:
        dev = approvals.enroll_device("us_aaaaaaaaaaaa", "tel", "spki")  # hors scope
        assert approvals.get_device(dev.id).user == "us_aaaaaaaaaaaa"
        approvals.enroll_totp_secret("us_aaaaaaaaaaaa", "JBSWY3DPEHPK3PXP")
        approvals._reserve_nonce("n-" + uuid.uuid4().hex, "login:" + dev.id, time.time() + 60)
        token = uuid.uuid4().hex
        approvals._store_session(Session(token=token, user="us_aaaaaaaaaaaa", device_id=dev.id,
                                         created_at=time.time(), expires_at=time.time() + 60))
        assert approvals.verify_session(token).user == "us_aaaaaaaaaaaa"
        with tenant_scope(B):  # l'appareil d'un autre compte est invisible (filtrage par compte dans base.py)
            assert approvals.get_device(dev.id) is None
        with pytest.raises(NoTenant):
            approvals.pending()  # approvals : sous RLS, jamais hors scope
    finally:
        approvals.close()



def test_role_check_flags_superuser_and_passes_the_app_role(pg):
    """`require_rls` (api/server.py::check_rls_role) s'appuie sur ce contrôle."""
    from nokker.store.conn import role_bypasses_rls

    assert role_bypasses_rls(DSN) is True  # le superutilisateur du conteneur de test
    assert role_bypasses_rls(pg["app"]) is False


@requires_pg
def test_data_consents_isolated_between_tenants(pg):
    from nokker.store.conn import connect
    from nokker.tenancy.context import tenant_scope

    for tid in (A, B):
        with tenant_scope(tid), connect(pg["app"], autocommit=True) as conn:
            conn.execute("INSERT INTO data_consents (account_id, scope, granted, source) VALUES (%s,'training',true,'signup')",
                         (f"us_{tid}",))
    for tid in (A, B):
        with tenant_scope(tid), connect(pg["app"], autocommit=True) as conn:
            assert {r[0] for r in conn.execute("SELECT tenant_id FROM data_consents").fetchall()} == {tid}
