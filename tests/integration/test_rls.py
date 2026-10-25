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
    "lokkal_machine_load",  # charge des machines Lokkal (020) : lue et écrite en connaissant le locataire
    "lokkal_relay_calls",  # journal des connecteurs relayés (022) : écrit dans le locataire de la machine
    "lokkal_relay_approvals",  # approbations fortes relayées (021, X10) : machine authentifiée ou compte, même règle
    # équipe (023) : locataire du PROJET ; les politiques d'équipe (membres d'autres locataires) dans test_team_rls.py
    "team_projects", "team_access", "team_members", "team_invites", "team_skills", "team_notes",
    "team_note_quarantine",
    "lokkal_machine_resources", "lokkal_handoffs", "lokkal_mirror_notes",  # délégation par besoin (025, X21)
    "nokker_agent_runs",  # passages des agents Crew (024, X14) : écrits dans le locataire de l'agent
    # plan et incidents (029, X18) : sources signées, signatures reçues, incidents, cartes, propositions, brief
    "nokker_alert_sources", "nokker_alert_seen", "nokker_incidents", "nokker_plan_cards", "nokker_plan_proposals",
    "nokker_plan_briefs",
    "lokkal_machine_deck",  # deck de chaque machine (028, X15) : machine authentifiée ou compte du deck
    "subscriptions",  # abonnements Stripe (010) : sous RLS
    "nokker_mail_watch",  # curseurs de la relève du déclencheur mail: (031) : écrits dans le locataire de l'agent
    "lokkal_mirror_pages",  # pages copiées, servies machine éteinte (033, §7.23) : machine authentifiée ou relais
    # SSO OIDC des équipes (032) : locataire de la connexion ; lecture du lien par les membres dans test_sso_rls.py
    "sso_connections", "sso_domains", "sso_bindings", "sso_managed", "sso_audit",
    # équipe, suite (037) : journal du projet (locataire du projet ; owner et maintainer d'autres locataires plus bas),
    # choix de livraison du brief du matin (locataire de la personne)
    "team_audit", "nokker_brief_delivery",
    # foyer (039, X23) : locataire du FOYER (le payeur) ; politiques des membres dans test_household_rls.py
    "household_members", "parental_consents",
    "nokker_code_sessions",  # sessions de code multi-utilisateur (038, §7.28) : écrites dans le locataire du compte
    "nokker_pack_installs",  # packs installés par un compte chez nokker.ai (036) : locataire du compte
]
OUT_OF_RLS = {"tenants", "accounts", "account_identities", "telegram_pairing", "operators", "admin_audit", "beta_applications",
              "devices", "sessions", "totp_secrets", "used_nonces", "auth_email_codes",  # authentification : lues avant le locataire
              "lokkal_machines", "lokkal_link_requests",  # lien Lokkal (014) : cherchées par empreinte de jeton ou par code
              "lokkal_gateway_codes", "lokkal_gateway_refresh",  # connexion passerelle (019), idem, par empreinte
              "billing_customers", "billing_events",  # webhook Stripe (010) : lus avant de connaître le locataire
              "lokkal_relay_used_jti",  # jti des jetons de pages présentés au relais (035) : par empreinte, sans locataire
              # SSO (032) : connexion → locataire, domaine prouvé → connexion, connexions en cours, (issuer, sub)
              "sso_routes", "sso_domain_claims", "sso_flows", "sso_identities",
              # foyer (039) : « ce compte est un profil mineur », lu avant le locataire (SSO, consentements)
              "account_minors",
              # modération des pages publiques (040) : registre lu par le relais avant le locataire, et par
              # l'opérateur pour tous les locataires ; audit en ajout seul
              "lokkal_public_pages", "lokkal_page_moderation",
              "account_deletions"}  # file d'effacement (017) : lue par l'outil d'opérateur pour tous les locataires


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
    from datetime import datetime, timezone

    from psycopg.types.json import Jsonb

    vals = {"text": lambda: uuid.uuid4().hex, "integer": lambda: secrets.randbelow(10**6),
            "bigint": lambda: secrets.randbelow(10**9), "double precision": lambda: 1.0,
            "boolean": lambda: False, "jsonb": lambda: Jsonb({}), "bytea": lambda: b"<p>x</p>",
            "timestamp with time zone": lambda: datetime.now(timezone.utc)}
    return [c for c, _ in rows], [vals[t]() for _, t in rows]


def _insert(conn, table: str, tenant: str | None = None) -> None:
    cols, vals = _dummy_row(conn, table)
    if table == "turns":  # clé étrangère vers threads (même locataire)
        th = uuid.uuid4().hex
        conn.execute("INSERT INTO threads (thread_id) VALUES (%s)", (th,))
        vals[cols.index("thread_id")] = th
    if table == "parental_consents":  # foyer (039) : une attestation est attestée, pour un profil mineur
        vals[cols.index("attested")], vals[cols.index("profile")] = True, "teen"
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
                run = agents.new_run(ag.id, "manual", thread_id=th)
                ledger.record(th, 0, call)
                ledger.record_verdict(th, 0, verdict)
                ids[tid] = {"th": th, "appr": appr.id, "thread": t.id, "bub": bub.id, "ag": ag.id, "run": run.id}
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
                assert agents.get_run(theirs["run"]) is None and agents.run_by_thread(theirs["th"]) is None
                assert agents.get_run(mine["run"]) is not None and agents.run_by_thread(mine["th"]).id == mine["run"]
                queued = {r.id for r in agents.runs_with_status("queued")}
                assert mine["run"] in queued and theirs["run"] not in queued
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


@requires_pg
def test_crew_agent_and_run_fields_roundtrip_on_postgres(pg, pod_env):
    """Lot X14 : tous les champs de §9.5 et des passages survivent à Postgres (migration 024, rejouée sans danger)."""
    from nokker.agents.pg import PgAgents
    from nokker.store.pg import apply_migrations
    from nokker.tenancy.context import tenant_scope
    from nokker.threads.pg import PgThreads

    assert apply_migrations(pg["owner"], strict=True)  # 024 rejouée : aucune erreur, rien ne change
    agents, threads = PgAgents(dsn=pg["app"]), PgThreads(dsn=pg["app"])
    try:
        with tenant_scope(A):
            a = agents.create(name="Matin", role="veille", model="ship", tools=["web_search"], status="paused",
                              created_by="us_aaaa", armed=False, instructions="Résume.",
                              trigger={"kind": "schedule", "cron": "0 7 * * 1-5", "tz": "Europe/Zurich"},
                              secrets=["API_TOKEN"], budget={"max_calls": 5, "max_cost": 1.0, "currency": "EUR"},
                              auto_approve=["note_write(notes/*)"], placement=None, project="pj_1")
            a.next_run_at, a.last_run, a.approved_rules = 1_800_000_000.0, {"run_id": "ar_x", "status": "failed",
                                                                             "ended_at": 1.0}, "abc"
            a.webhook_key, a.arm_approval_id = "raw:whsec_test", "appr-1"
            agents.save(a)
            got = agents.get(a.id)
            assert got.trigger == a.trigger and got.budget == a.budget
            assert (got.secrets, got.auto_approve, got.project, got.armed) == (["API_TOKEN"], ["note_write(notes/*)"],
                                                                                "pj_1", False)
            assert got.last_run == a.last_run and got.webhook_key == "raw:whsec_test"
            assert got.next_run_at == a.next_run_at
            r1 = agents.new_run(a.id, "schedule", status="queued", scheduled_for=1.0, catch_up=True, event=None,
                                input="données", approvals=["c1"])
            r1.created_at = 10.0
            agents.save_run(r1)
            r2 = agents.new_run(a.id, "manual", status="succeeded", thread_id="tn_aaaaaaaaaaaa:th_ar_x",
                                cost={"tokens_in": 1, "tokens_out": 2, "amount": 0.1, "currency": "EUR"})
            r2.created_at = 20.0
            agents.save_run(r2)
            assert agents.live_run(a.id).id == r1.id and agents.get_run(r1.id).catch_up
            page, nxt = agents.runs(a.id, limit=1)
            assert [r.id for r in page] == [r2.id] and nxt == r2.id
            assert [r.id for r in agents.runs(a.id, limit=5, before=nxt)[0]] == [r1.id]
            assert agents.get_run(r2.id).cost["currency"] == "EUR" and agents.get_run(r1.id).approvals == ["c1"]
            t = threads.create("passage", "us_aaaa", thread_id="tn_aaaaaaaaaaaa:th_ar_x",
                               origin={"kind": "agent", "agent_id": a.id, "run_id": r2.id})
            assert threads.get(t.id).origin == {"kind": "agent", "agent_id": a.id, "run_id": r2.id}
            agents.delete(a.id)
            assert agents.get(a.id) is None and agents.get_run(r1.id) is None
    finally:
        agents.close()
        threads.close()


@requires_pg
def test_crew_engine_ticks_each_tenant_in_its_own_scope(pg, pod_env, tmp_path):
    """L'ordonnanceur passe sur chaque locataire connu, sous SA RLS : un agent de A tourne chez A, jamais vu par B."""
    from datetime import datetime

    from nokker.agents.pg import PgAgents
    from nokker.tenancy.context import tenant_scope
    from tests.unit.agents.conftest import make_world

    w = make_world(tmp_path, [("text", "ok"), ("text", "ok")])
    w.agents.store = PgAgents(dsn=pg["app"])
    w.engine.tenants = lambda: [A, B]
    t0 = datetime.fromisoformat("2026-10-26T05:00:00+00:00").timestamp()
    w.clock.t = t0
    w.agents.store.clock = w.clock
    made = {}
    try:
        for tid in (A, B):
            with tenant_scope(tid):
                a = w.agents.create_from_body({"name": tid, "role": "r",
                                               "trigger": {"kind": "schedule", "cron": "*/5 * * * *"}}, "us_x")
                w.agents.arm(a.id, "us_x")
                made[tid] = a.id
        w.clock.t = t0 + 301
        w.engine.tick()
        for tid, other in ((A, B), (B, A)):
            with tenant_scope(tid):
                runs = w.agents.store.runs(made[tid])[0]
                assert [r.status for r in runs] == ["succeeded"] and runs[0].thread_id.startswith(tid + ":")
                assert w.agents.store.get(made[other]) is None and w.agents.store.runs(made[other])[0] == []
    finally:
        w.agents.store.close()


def test_billing_pg_store_isolation_idempotence_and_stale_events(pg, pod_env):
    """nokker/billing sur le vrai rôle nokker_app : webhook sans locataire (tables hors RLS), abonnement sous RLS."""
    from datetime import datetime, timedelta, timezone

    from nokker.billing.store import PgBilling
    from nokker.tenancy.context import tenant_scope

    store = PgBilling(pg["app"])
    t0 = datetime(2100, 1, 1, 12, 0, tzinfo=timezone.utc)  # plus récent que les lignes factices des autres tests du module
    row = {"stripe_subscription": "sub_1", "stripe_customer": "cus_a", "plan": "solo", "status": "active",
           "current_period_end": t0 + timedelta(days=30), "cancel_at_period_end": False, "last_event_at": t0}

    assert store.put_customer("cus_a", A, "us_a") == "cus_a"
    assert store.put_customer("cus_a2", A, "us_a") == "cus_a"  # un seul client Stripe par locataire
    assert store.put_customer("cus_b", B, "us_b") == "cus_b"
    assert store.tenant_for_customer("cus_a") == (A, "us_a") and store.tenant_for_customer("cus_inconnu") is None
    assert not store.event_seen("evt_1")
    store.mark_event("evt_1", "customer.subscription.created")
    store.mark_event("evt_1", "customer.subscription.created")
    assert store.event_seen("evt_1")

    with tenant_scope(A):
        assert store.upsert_subscription(row) is True
        assert store.upsert_subscription({**row, "status": "canceled", "last_event_at": t0 - timedelta(seconds=1)}) is False
        assert store.upsert_subscription({**row, "status": "past_due", "last_event_at": t0 + timedelta(seconds=1)}) is True
    with tenant_scope(B):
        assert store.upsert_subscription({**row, "stripe_customer": "cus_b", "plan": "essentiel"}) is True
    assert store.subscription(A)["status"] == "past_due" and store.subscription(A)["plan"] == "solo"
    assert store.subscription(B)["plan"] == "essentiel"

    with tenant_scope(A):  # résilié puis égalité de seconde : jamais ressuscité ; un abonnement actif passe devant
        gone = {**row, "stripe_subscription": "sub_old", "status": "canceled", "last_event_at": t0 + timedelta(days=1)}
        assert store.upsert_subscription(gone) is True
        assert store.upsert_subscription({**gone, "status": "active"}) is False
        assert store.subscription(A)["stripe_subscription"] == "sub_1"  # sub_old est plus récent mais résilié

    import psycopg

    with psycopg.connect(pg["app"]) as conn:  # hors scope : la RLS ne rend rien
        assert conn.execute("SELECT count(*) FROM subscriptions").fetchone()[0] == 0


@requires_pg
def test_mail_watch_cursor_roundtrips_and_stays_in_its_tenant(pg, pod_env):
    """Déclencheur `mail:` (031) : le curseur d'un agent survit à Postgres (redémarrage : pas de rejeu), et un
    locataire ne voit ni n'efface celui d'un autre."""
    from nokker.agents.mailwatch import PgWatchStore, WatchState
    from nokker.store.pg import apply_migrations
    from nokker.tenancy.context import tenant_scope

    assert apply_migrations(pg["owner"], strict=True)  # 031 rejouée : aucune erreur
    store = PgWatchStore(pg["app"])
    try:
        with tenant_scope(A):
            st = WatchState(agent_id="ag_aaaaaaaaaaaa", filter="from:billing@example.com", uidvalidity=41, last_uid=7,
                            last_poll_at=1.5, last_ok_at=1.5, next_poll_at=121.5, errors=0, pending=2,
                            held="un passage est en cours", updated_at=1.5)
            store.save(st)
            st.last_uid, st.pending = 9, 0
            st.inflight_run_id, st.inflight_uid = "ar_0123456789abcdef", 12  # curseur « en vol » (p2/mail-local)
            store.save(st)  # mise à jour en place
        with tenant_scope(B):
            assert store.get("ag_aaaaaaaaaaaa") is None and "ag_aaaaaaaaaaaa" not in store.ids()
            store.delete("ag_aaaaaaaaaaaa")  # sans effet hors de son locataire
        store2 = PgWatchStore(pg["app"])  # « redémarrage »
        try:
            with tenant_scope(A):
                got = store2.get("ag_aaaaaaaaaaaa")
                assert (got.uidvalidity, got.last_uid, got.pending, got.filter) == (41, 9, 0, "from:billing@example.com")
                assert (got.inflight_run_id, got.inflight_uid) == ("ar_0123456789abcdef", 12)
                assert "ag_aaaaaaaaaaaa" in store2.ids()
                store2.delete("ag_aaaaaaaaaaaa")
                assert store2.get("ag_aaaaaaaaaaaa") is None
        finally:
            store2.close()
    finally:
        store.close()


# ─────────────── équipe, suite (037) : dépenses d'un projet, journal, restauration, livraison du brief ───────────────
# Quatre locataires à part (E, F, G, H, I) pour ne pas croiser les tests génériques sur A et B : le propriétaire (E)
# partage un projet avec un maintainer (F), un membre (G) et un viewer (I) ; H n'est membre de rien.

E, F, G, H, I = "tn_eeeeeeeeeeee", "tn_ffffffffffff", "tn_gggggggggggg", "tn_hhhhhhhhhhhh", "tn_iiiiiiiiiiii"


class _Acc:
    def __init__(self, tenant_id: str, account_id: str, email: str) -> None:
        self.tenant_id, self.account_id, self.email, self.status = tenant_id, account_id, email, "active"


OWN = _Acc(E, "us_owner037001", "owner037@example.org")
MAINT = _Acc(F, "us_maint037001", "maint037@example.org")
MEM = _Acc(G, "us_member37001", "mem037@example.org")
OUT = _Acc(H, "us_outsider371", "out037@example.org")
VIEW = _Acc(I, "us_viewer37001", "view037@example.org")


class _Accounts:
    def __init__(self, *accs: _Acc) -> None:
        self.by_id = {a.account_id: a for a in accs}

    def account(self, account_id):
        return self.by_id.get(account_id)

    def account_by_email(self, email):
        return next((a for a in self.by_id.values() if a.email == email), None)


@pytest.fixture(scope="module")
def team037(pg):
    from nokker.team.service import Team
    from nokker.team.store import PgTeamStore
    from nokker.tenancy.context import tenant_scope

    team = Team(store=PgTeamStore(pg["app"], migrate_now=False), tenancy=_Accounts(OWN, MAINT, MEM, OUT, VIEW),
                mirror_allowed=lambda acc: True)
    with tenant_scope(E):
        pid = team.create(OWN, {"name": "Plateforme"})["id"]
        team.patch(OWN, pid, {"mirror": True})
        invites = {who.account_id: team.invite(OWN, pid, {"email": who.email, "role": role})["id"]
                   for who, role in ((MAINT, "maintainer"), (MEM, "member"), (VIEW, "viewer"))}
    for who in (MAINT, MEM, VIEW):
        with tenant_scope(who.tenant_id):
            team.accept(who, invites[who.account_id])
    return {"team": team, "pid": pid}


def _raw037(dsn, actor, sql, params=()):
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false), set_config('app.account_id', %s, false)",
                     (actor.tenant_id, actor.account_id))
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if cur.description else cur.rowcount
        conn.commit()
        return rows


@requires_pg
def test_project_spending_is_read_across_tenants_by_members_only(pg, pod_env, team037):
    """Le budget se lit dans le ledger : les lignes attribuées au projet, de tous les locataires, pour un membre du
    projet ; jamais une ligne hors projet d'un autre locataire, jamais rien pour un non-membre ; et personne
    n'attribue au projet une dépense qu'il n'a pas le droit d'y mettre."""
    from datetime import UTC, datetime, timedelta

    import psycopg

    from nokker.ledger.attribution import SpendTag, spend_tag
    from nokker.ledger.costs import PostgresLedger
    from nokker.ledger.prices import load_from_yaml
    from nokker.team.budget import ProjectBudget
    from nokker.tenancy.context import tenant_scope
    from tests.unit.team.test_budget import call

    team, pid = team037["team"], team037["pid"]
    ledger = PostgresLedger(pg["app"], prices=load_from_yaml(), migrate=False)
    since = datetime.now(UTC) - timedelta(days=1)
    with tenant_scope(G):
        with spend_tag(SpendTag(pid, E, MEM.account_id)):
            ledger.record("th_g_proj", 0, call(60))
        ledger.record("th_g_free", 0, call(500))  # hors projet : reste chez G
    with tenant_scope(F), spend_tag(SpendTag(pid, E, MAINT.account_id)):
        ledger.record("th_f_proj", 0, call(30))
    with tenant_scope(E):
        rows = ledger.fetch_project_calls(pid, E, since=since, actor=OWN.account_id)
        assert sorted((r.thread_id, r.account) for r in rows) == [("th_f_proj", MAINT.account_id),
                                                                  ("th_g_proj", MEM.account_id)]
        assert ledger.fetch_calls(thread_id="th_g_proj") == []  # sans compte posé : rien d'un autre locataire
    with tenant_scope(H):
        assert ledger.fetch_project_calls(pid, E, since=since, actor=OUT.account_id) == []
    # SQL brut au nom du membre : ses lignes, celles du projet ; jamais la ligne hors projet de F
    seen = {r[0] for r in _raw037(pg["app"], MAINT, "SELECT thread_id FROM ledger_entries")}
    assert {"th_g_proj", "th_f_proj"} <= seen and "th_g_free" not in seen
    assert {r[0] for r in _raw037(pg["app"], OUT, "SELECT thread_id FROM ledger_entries")} == set()
    # attribuer au projet : un non-membre, un viewer, ou au nom d'un autre compte → refusé en base
    for who, acc in ((OUT, OUT.account_id), (VIEW, VIEW.account_id), (MEM, MAINT.account_id)):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _raw037(pg["app"], who, "INSERT INTO ledger_entries (thread_id, turn_index, tier, model, project_id, "
                                    "project_tenant, account) VALUES ('th_x', 0, 'ship', 'm', %s, %s, %s)",
                    (pid, E, acc))
    with tenant_scope(H), spend_tag(SpendTag(pid, E, OUT.account_id)), \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        ledger.record("th_h", 0, call(10))
    # la vue du budget sur Postgres : total du projet, lignes par membre pour l'owner, la sienne pour un membre
    budget = ProjectBudget(team=team, ledger=ledger, prices=ledger.prices)
    with tenant_scope(E):
        team.put_budget(OWN, pid, {"daily": 100})
        v = budget.view(OWN, pid)
    assert v["today"]["spent"] == pytest.approx(0.9)
    assert {m["account_id"]: m["today"] for m in v["members"] if m["today"]} == {
        MEM.account_id: pytest.approx(0.6), MAINT.account_id: pytest.approx(0.3)}
    with tenant_scope(G):
        mine = budget.view(MEM, pid)
        assert [m["account_id"] for m in mine["members"]] == [MEM.account_id]
        assert budget.check_project(pid, MEM.account_id).verdict is None


@requires_pg
def test_project_journal_and_quarantine_restore_across_tenants(pg, pod_env, team037):
    """Journal du projet : écrit et lu par owner et maintainer seulement (même d'un autre locataire) ; la restauration
    d'une version de la quarantaine par un maintainer d'un autre locataire passe, un membre ne touche à rien."""
    from nokker.tenancy.context import tenant_scope

    team, pid = team037["team"], team037["pid"]
    note = {"name": "fact-ci", "kind": "fact", "title": "CI", "locality": "both"}
    with tenant_scope(G):
        team.push_notes(MEM, pid, "mc_g", [{**note, "text": "La CI tourne sur la forge."}])
    with tenant_scope(F):
        team.push_notes(MAINT, pid, "mc_f", [{**note, "text": "La CI tourne ailleurs."}])
        [q] = [x for x in team.quarantined(MAINT, pid)["quarantine"] if x["name"] == "fact-ci"]
        got = team.restore_note(MAINT, pid, q["id"])["note"]
        assert got["text"] == "La CI tourne sur la forge."
        rows = team.quarantined(MAINT, pid)["quarantine"]
        assert [r["restored_by"] for r in rows if r["id"] == q["id"]] == [MAINT.account_id]
        events = [e["event"] for e in team.audit(MAINT, pid)["events"]]
        assert "note_restored" in events
    # le membre : ni lecture du journal, ni écriture, ni marque sur la quarantaine
    assert _raw037(pg["app"], MEM, "SELECT * FROM team_audit") == []
    assert _raw037(pg["app"], MEM, "UPDATE team_note_quarantine SET restored_by = 'x'") == 0
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _raw037(pg["app"], MEM, "INSERT INTO team_audit (project_id, tenant_id, at, actor_account, event) "
                                "VALUES (%s, %s, 1, %s, 'budget_set')", (pid, E, MEM.account_id))
    # le maintainer n'écrit pas au nom d'un autre
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _raw037(pg["app"], MAINT, "INSERT INTO team_audit (project_id, tenant_id, at, actor_account, event) "
                                  "VALUES (%s, %s, 1, %s, 'budget_set')", (pid, E, OWN.account_id))
    assert _raw037(pg["app"], OUT, "SELECT * FROM team_audit") == []


@requires_pg
def test_brief_delivery_choice_roundtrips_and_stays_in_its_tenant(pg, pod_env):
    from nokker.plan.store import PgPlan
    from nokker.tenancy.context import tenant_scope

    store = PgPlan(dsn=pg["app"], migrate_now=False)
    try:
        with tenant_scope(A):
            store.put_delivery("us_aaaa", {"telegram": True, "email": "a@example.com", "email_armed": "f" * 64,
                                           "arm_approval_id": None, "armed_by": "us_aaaa", "armed_at": 1.0,
                                           "last": {"at": "2026-10-26T06:30:00Z", "telegram": "sent", "email": "sent"},
                                           "updated_at": 2.0})
            store.put_delivery("us_aaaa", {**store.get_delivery("us_aaaa"), "telegram": False, "updated_at": 3.0})
            got = store.get_delivery("us_aaaa")
            assert got["telegram"] is False and got["email_armed"] == "f" * 64 and got["last"]["email"] == "sent"
        with tenant_scope(B):
            assert store.get_delivery("us_aaaa") is None
    finally:
        store.close()


@requires_pg
def test_mail_watch_031_adds_the_inflight_columns_to_an_older_table(pg):
    """031 de la première version (sans curseur « en vol ») déjà appliquée : rejouée, elle ajoute les deux colonnes,
    et une seconde fois ne touche à rien."""
    import psycopg

    from nokker.agents.mailwatch import MIGRATION
    from nokker.store.conn import migrate

    with psycopg.connect(pg["owner"], autocommit=True) as c:
        c.execute("ALTER TABLE nokker_mail_watch DROP COLUMN IF EXISTS inflight_run_id, "
                  "DROP COLUMN IF EXISTS inflight_uid")
    sql = MIGRATION.read_text(encoding="utf-8")
    assert migrate(pg["owner"], sql, rls=False, strict=True)
    assert migrate(pg["owner"], sql, rls=False, strict=True)
    with psycopg.connect(pg["owner"]) as c:
        cols = {r[0] for r in c.execute("SELECT column_name FROM information_schema.columns "
                                        "WHERE table_name = 'nokker_mail_watch'").fetchall()}
    assert {"inflight_run_id", "inflight_uid"} <= cols


@requires_pg
def test_web_packs_and_agent_origin_roundtrip_under_rls(pg, pod_env):
    """Migration 036 : les packs d'un compte (`PgPackStore`) restent dans son locataire, l'origine d'un agent créé
    depuis la carte d'un pack survit à Postgres, et la migration se rejoue sans danger."""
    from nokker.agents.pg import PgAgents
    from nokker.store.pg import apply_migrations
    from nokker.tenancy.context import tenant_scope
    from nokker.webpacks.pg import PgPackStore
    from nokker.webpacks.service import PackInstall

    assert apply_migrations(pg["owner"], strict=True)
    store = PgPackStore(pg["app"])
    rec = PackInstall(account="us_aaaa", pack="mail", version=1,
                      skills=[{"family": "mail", "name": "mail-triage", "sha256": "ab" * 32}],
                      templates=["inbox-triage"], machine={"skills": [], "agents": []}, bind={"secret:X": "Y"},
                      proposed=[{"template": "inbox-triage", "bubble_id": "bu_1"}], installed_at=1.5)
    store.put(A, rec)
    rec.version = 2
    store.put(A, rec)  # même (compte, pack) : mis à jour, pas dupliqué
    got = store.get(A, "us_aaaa", "mail")
    assert got == rec and [r.pack for r in store.list(A, "us_aaaa")] == ["mail"]
    assert store.get(B, "us_aaaa", "mail") is None and store.list(B, "us_aaaa") == []
    assert store.delete(B, "us_aaaa", "mail") is False and store.get(A, "us_aaaa", "mail") is not None
    assert store.delete(A, "us_aaaa", "mail") is True and store.get(A, "us_aaaa", "mail") is None
    agents = PgAgents(dsn=pg["app"])
    try:
        with tenant_scope(A):
            a = agents.create(name="Tri", role="r", model="", tools=["mail_search"], created_by="us_aaaa",
                              origin={"pack": "mail", "template": "inbox-triage"})
            assert agents.get(a.id).origin == {"pack": "mail", "template": "inbox-triage"}
            b = agents.create(name="Libre", role="r", model="", tools=[], created_by="us_aaaa")
            assert agents.get(b.id).origin is None
        with tenant_scope(B):
            assert agents.get(a.id) is None
    finally:
        agents.close()
