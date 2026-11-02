"""Base CortHeXis sous locataire et RLS forcée (nokker/context/corthexis_tenancy.py, docs/MEMORY-TENANCY.md §5) : vrai
Postgres pgvector jetable, VRAI paquet `corthexis` (schéma, indexeur, recherche), rôle propriétaire des tables sans
superutilisateur ni BYPASSRLS (comme `nokker_app` sur la base `corthexis` de la bêta), plongements de 768 dimensions.
Ignoré sans `NOKKER_TEST_PG_DSN` ou sans le paquet `corthexis`.

Prouve : des notes d'AVANT (sans locataire) sont rattachées au locataire de leur fil ; ensuite, une recherche
vectorielle d'un texte IDENTIQUE ne remonte jamais une note ni un chunk d'un autre locataire (API du paquet ET SQL brut
sur la partition) ; hors d'un locataire, rien ; une nouvelle génération (partition) naît sous RLS ; l'effacement d'un
locataire ne touche que ses lignes."""
from __future__ import annotations

import os
import secrets
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
DSN = os.environ.get("NOKKER_TEST_PG_DSN")
requires_pg = pytest.mark.skipif(not DSN, reason="NOKKER_TEST_PG_DSN non posé — pas de Postgres jetable")

TA, TB = "tn_aaaaaaaaaaaa", "tn_bbbbbbbbbbbb"
TEXT = "Le code du portail de l'immeuble est 8812B, la clé de la cave est chez la gardienne"


def _note(root, project: str, name: str, body: str) -> None:
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\nname: {name}\ndescription: \"souvenir\"\ntype: project\n---\n# Souvenir\n\n"
                                  f"{body}\n", encoding="utf-8")


@pytest.fixture(scope="module")
def cx(tmp_path_factory):
    if not DSN:
        pytest.skip("NOKKER_TEST_PG_DSN non posé")
    pytest.importorskip("corthexis.indexer", reason="paquet corthexis absent")
    import psycopg
    from psycopg.conninfo import make_conninfo

    db, role, pw = f"cx_{uuid.uuid4().hex[:8]}", f"nk_cx_{uuid.uuid4().hex[:6]}", secrets.token_hex(12)
    with psycopg.connect(DSN, autocommit=True) as su:
        su.execute(f'CREATE DATABASE "{db}"')
        su.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{pw}' NOSUPERUSER NOBYPASSRLS")
    su_db = make_conninfo(DSN, dbname=db)
    with psycopg.connect(su_db, autocommit=True) as su:
        su.execute("CREATE EXTENSION IF NOT EXISTS vector")
        su.execute(f"ALTER SCHEMA public OWNER TO {role}")
    app = make_conninfo(DSN, dbname=db, user=role, password=pw)
    yield {"app": app, "su": su_db, "root": tmp_path_factory.mktemp("session-memory")}
    with psycopg.connect(DSN, autocommit=True) as su:
        su.execute(f'DROP DATABASE "{db}" WITH (FORCE)')
        su.execute(f"DROP ROLE {role}")


def _index(store, root, project: str) -> None:
    from corthexis.indexer import IndexConfig, Indexer

    from nokker.memory.tenant_long import HashEmbedder

    cfg = IndexConfig.from_env(memory_dir=root / project, project=project, normalize=False, write_index=False,
                               chunk_target=300)
    Indexer(store, HashEmbedder(dim=768), cfg).run()


def _vec(text: str) -> list[float]:
    from nokker.memory.tenant_long import HashEmbedder

    return HashEmbedder(dim=768).embed_query(text)


@requires_pg
def test_corthexis_rows_are_tenant_scoped_even_for_an_identical_vector(cx):
    import psycopg
    from corthexis.store import Store

    from nokker.context import corthexis_tenancy as ct
    from nokker.context.corthexis_erase import erase_session_memory
    from nokker.context.corthexis_index import project_of
    from nokker.tenancy.context import tenant_scope

    root = cx["root"]
    th_a, th_b = f"{TA}:th-1", f"{TB}:th-1"
    pa, pb = project_of(th_a), project_of(th_b)
    # 1. base d'AVANT : notes et chunks sans locataire (le même texte chez A et chez B), une ligne d'un fil inconnu
    _note(root, pa, "ep-aaaa0001", TEXT)
    _note(root, pb, "ep-bbbb0001", TEXT)
    _note(root, "s-0123456789abcdef", "ep-cccc0001", TEXT)
    store = Store(cx["app"], max_size=2)
    try:
        for p in (pa, pb, "s-0123456789abcdef"):
            _index(store, root, p)
        # 2. locataire et RLS posés, lignes rattachées au locataire de leur fil
        assert ct.ensure(cx["app"], lambda: {pa: TA, pb: TB})
        assert ct.ensure(cx["app"], dict)  # rejouée : rien ne change
        with psycopg.connect(cx["su"]) as su:
            assert dict(su.execute("SELECT name, tenant_id FROM notes").fetchall()) == {
                "ep-aaaa0001": TA, "ep-bbbb0001": TB, "ep-cccc0001": ct.ORPHAN}
            assert {r[0] for r in su.execute("SELECT DISTINCT c.tenant_id = n.tenant_id FROM chunks c "
                                             "JOIN notes n ON n.id = c.note_id").fetchall()} == {True}
            rls = su.execute("SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
                             "WHERE c.relname IN ('notes', 'chunks', 'chunks_g1', 'note_versions', 'links')").fetchall()
            assert len(rls) == 5 and all(r[1] and r[2] for r in rls), rls
            part = su.execute("SELECT relname FROM pg_class WHERE relname LIKE 'chunks_g%' AND relkind = 'r'").fetchall()
        ct.guard_store(store)
        # 3. API du paquet : B cherche le texte IDENTIQUE (plus proche voisin exact) → SA note, jamais celle de A
        q = _vec(TEXT)
        with tenant_scope(TB):
            hits = store.search(q, TEXT, k=10)
            assert [h.note_name for h in hits] == ["ep-bbbb0001"]
            assert store.search(q, TEXT, k=10, projects=(pa,)) == []  # même en visant le projet de A
        with tenant_scope(TA):
            assert [h.note_name for h in store.search(q, TEXT, k=10)] == ["ep-aaaa0001"]
        assert store.search(q, TEXT, k=10) == []  # hors d'un locataire : rien
        # 4. SQL brut sur la partition, rôle propriétaire (FORCE) : jamais un chunk d'un autre locataire
        lit = "[" + ",".join(f"{x:.6g}" for x in q) + "]"
        for tid in (TA, TB):
            with psycopg.connect(cx["app"]) as c:
                c.execute("SELECT set_config('app.tenant_id', %s, false)", (tid,))
                for (t,) in part:
                    rows = c.execute(f"SELECT tenant_id FROM {t} ORDER BY embedding <=> %s::halfvec LIMIT 50",
                                     (lit,)).fetchall()
                    assert {r[0] for r in rows} == {tid}, (t, tid, rows)
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute("UPDATE notes SET tenant_id = %s", (TA if tid == TB else TB,))
        with psycopg.connect(cx["app"]) as c:  # sans locataire posé, ou vide : rien
            assert c.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
            c.execute("SELECT set_config('app.tenant_id', '', false)")
            assert c.execute("SELECT count(*) FROM notes").fetchone()[0] == 0
        # 5. écriture après la pose : le locataire du tour, posé par la connexion empruntée
        _note(root, project_of(f"{TA}:th-2"), "ep-aaaa0002", "Le rendez-vous chez le notaire est vendredi")
        with tenant_scope(TA):
            _index(store, root, project_of(f"{TA}:th-2"))
        with psycopg.connect(cx["su"]) as su:
            assert su.execute("SELECT tenant_id FROM notes WHERE name = 'ep-aaaa0002'").fetchone()[0] == TA
        with tenant_scope(TB):
            assert store.search(_vec("notaire vendredi"), "notaire vendredi", k=10) == [] or all(
                h.note_name.startswith("ep-bbbb") for h in store.search(_vec("notaire vendredi"), "notaire vendredi"))
        # 6. une nouvelle génération (partition) naît sous RLS, avant d'être remplie
        g = store.create_generation("hash-768-bis", 768)
        with psycopg.connect(cx["su"]) as su:
            assert su.execute("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname = %s",
                              (g.table,)).fetchone()[0]
        store.drop_generation(g.id)
        # 7. effacement du locataire A (nokker erase) : ses lignes seulement
        os.environ["NK_CX_TEST_DSN"] = cx["app"]
        try:
            erase_session_memory({"database_url_env": "NK_CX_TEST_DSN", "root_dir": str(root)},
                                 [th_a, f"{TA}:th-2"], apply=True)
        finally:
            os.environ.pop("NK_CX_TEST_DSN", None)
        with psycopg.connect(cx["su"]) as su:
            assert sorted(r[0] for r in su.execute("SELECT DISTINCT tenant_id FROM notes").fetchall()) == \
                sorted([TB, ct.ORPHAN])
            assert su.execute("SELECT count(*) FROM chunks WHERE tenant_id = %s", (TA,)).fetchone()[0] == 0
            assert su.execute("SELECT count(*) FROM chunks WHERE tenant_id = %s", (TB,)).fetchone()[0] > 0
    finally:
        store.close()


# ─────────────── propriété au rôle de migration (`corthexis_schema: external`, docs/MEMORY-TENANCY.md §5.1) ───────────────
# Rôles comme en bêta : propriétaire non super, sans BYPASSRLS (fait `nokker migrate`) ; rôle du pod propriétaire de
# rien ; rôle de sauvegarde BYPASSRLS en lecture. Noms tirés au hasard (le cluster de test peut être partagé) ; le
# fichier de reprise deploy/postgres/corthexis-owner.sql est rejoué avec ces noms.

REOWN_SQL = Path(__file__).resolve().parents[2] / "deploy" / "postgres" / "corthexis-owner.sql"


@pytest.fixture()
def roles():
    if not DSN:
        pytest.skip("NOKKER_TEST_PG_DSN non posé")
    pytest.importorskip("corthexis.indexer", reason="paquet corthexis absent")
    import psycopg

    tag = uuid.uuid4().hex[:6]
    out = {k: (f"nk_{k}_{tag}", secrets.token_hex(12)) for k in ("owner", "app", "backup")}
    with psycopg.connect(DSN, autocommit=True) as su:
        su.execute(f"CREATE ROLE {out['owner'][0]} LOGIN PASSWORD '{out['owner'][1]}' NOSUPERUSER NOBYPASSRLS")
        su.execute(f"CREATE ROLE {out['app'][0]} LOGIN PASSWORD '{out['app'][1]}' NOSUPERUSER NOBYPASSRLS")
        su.execute(f"CREATE ROLE {out['backup'][0]} LOGIN PASSWORD '{out['backup'][1]}' NOSUPERUSER BYPASSRLS")
    dbs: list[str] = []
    yield out, dbs
    with psycopg.connect(DSN, autocommit=True) as su:
        for db in dbs:
            su.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        for name, _ in out.values():
            su.execute(f"DROP OWNED BY {name}")  # dans la base `postgres` : droits par défaut éventuels
            su.execute(f"DROP ROLE {name}")


def _db(roles, owner_key: str):
    """Base neuve, base + schéma public au rôle `owner_key`, pgvector posé par l'administrateur. Rend les DSN."""
    import psycopg
    from psycopg.conninfo import make_conninfo

    out, dbs = roles
    db = f"cxo_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as su:
        su.execute(f'CREATE DATABASE "{db}" OWNER {out[owner_key][0]}')
    dbs.append(db)
    with psycopg.connect(make_conninfo(DSN, dbname=db), autocommit=True) as su:
        su.execute("CREATE EXTENSION IF NOT EXISTS vector")
        su.execute(f"ALTER SCHEMA public OWNER TO {out[owner_key][0]}")
        su.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    dsn = {k: make_conninfo(DSN, dbname=db, user=n, password=p) for k, (n, p) in out.items()}
    dsn["su"] = make_conninfo(DSN, dbname=db)
    return dsn


@pytest.fixture()
def external_pod(monkeypatch):
    """Pod en `external` + `require_rls`. Le paquet ne doit plus jamais migrer : un `Store` ouvert avec la migration
    au premier usage lève (`migrate_schema` n'ouvre pas de `Store`, il appelle `Store.migrate` seul)."""
    from corthexis import service as cx_service
    from corthexis.store import Store

    from nokker.context import corthexis_tenancy as ct

    real_init = Store.__init__

    def _init(self, *args, **kwargs):
        if kwargs.get("migrate", True):
            raise AssertionError("le pod a ouvert un Store CortHeXis qui migre au premier usage")
        real_init(self, *args, **kwargs)

    def start(app_dsn: str):
        monkeypatch.setattr(Store, "__init__", _init)
        monkeypatch.setenv("CORTHEXIS_DATABASE_URL", app_dsn)
        cx_service.reset()
        return ct.guarded_store()

    ct.set_mode(ct.EXTERNAL)
    ct.set_strict(True)
    yield start
    monkeypatch.undo()
    cx_service.reset()
    ct.set_mode(ct.MANAGED)
    ct.set_strict(False)


def _assert_pod_locked_out(app_dsn: str) -> None:
    import psycopg

    for stmt in ("ALTER TABLE notes DISABLE ROW LEVEL SECURITY", "ALTER TABLE notes NO FORCE ROW LEVEL SECURITY",
                 "ALTER TABLE chunks_g1 DISABLE ROW LEVEL SECURITY", "DROP POLICY tenant_isolation ON notes",
                 "DROP TABLE notes", "CREATE TABLE nk_evil (x int)", "TRUNCATE notes"):
        with psycopg.connect(app_dsn) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(stmt)


def _rls_state(su_dsn: str) -> dict:
    import psycopg

    with psycopg.connect(su_dsn) as su:
        return {r[0]: r[1:] for r in su.execute(
            "SELECT c.relname, c.relrowsecurity AND c.relforcerowsecurity, pg_get_userbyid(c.relowner), "
            "EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'tenant_id') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')").fetchall()}


@requires_pg
def test_migrate_as_owner_then_external_pod_indexes_recalls_and_cannot_disable_rls(roles, external_pod, tmp_path):
    import psycopg

    from nokker.context import corthexis_tenancy as ct
    from nokker.context.corthexis_index import project_of
    from nokker.tenancy.context import tenant_scope

    out, _ = roles
    dsn = _db(roles, "owner")
    app, backup = out["app"][0], out["backup"][0]
    # 1. migrate en propriétaire : schéma du paquet, tenant_id + RLS forcée, fonctions, droits
    rep = ct.migrate_schema(dsn["owner"], app_role=app, backup_role=backup, project_tenants=dict)
    assert rep["applied"] and rep["backup"] is True
    state = _rls_state(dsn["su"])
    for t in (*ct.TABLES, *ct.EXTRA, *ct.STATS, "chunks"):
        assert state[t] == (True, out["owner"][0], True), (t, state[t])
    with psycopg.connect(dsn["su"]) as su:
        def priv(role, t, p):
            return su.execute("SELECT has_table_privilege(%s, %s, %s)", (role, t, p)).fetchone()[0]

        assert all(priv(app, "notes", p) for p in ("SELECT", "INSERT", "UPDATE", "DELETE"))
        assert not any(priv(app, "notes", p) for p in ("TRUNCATE", "REFERENCES", "TRIGGER"))
        assert su.execute("SELECT has_sequence_privilege(%s, 'notes_id_seq', 'USAGE')", (app,)).fetchone()[0]
        assert priv(backup, "notes", "SELECT") and not priv(backup, "notes", "INSERT")
        assert su.execute("SELECT has_database_privilege(%s, current_database(), 'CONNECT')", (backup,)).fetchone()[0]
    # 2. rejouer migrate : idempotent (aucune migration du paquet, rien ne change) ; sauvegarde absente : ignorée
    again = ct.migrate_schema(dsn["owner"], app_role=app, backup_role="nk_absent_role", project_tenants=dict)
    assert again["applied"] == [] and again["backup"] is False
    assert _rls_state(dsn["su"]) == state
    # 3. le pod (rôle app, external) : vérification au démarrage, aucune migration, indexe et rappelle sous locataire
    assert ct.verify(dsn["app"]) == []
    store = external_pod(dsn["app"])
    th_a, th_b = f"{TA}:th-1", f"{TB}:th-1"
    _note(tmp_path, project_of(th_a), "ep-aaaa0001", TEXT)
    _note(tmp_path, project_of(th_b), "ep-bbbb0001", TEXT)
    with tenant_scope(TA):
        _index(store, tmp_path, project_of(th_a))
    with tenant_scope(TB):
        _index(store, tmp_path, project_of(th_b))
    q = _vec(TEXT)
    with tenant_scope(TA):
        assert [h.note_name for h in store.search(q, TEXT, k=10)] == ["ep-aaaa0001"]
    with tenant_scope(TB):
        assert [h.note_name for h in store.search(q, TEXT, k=10)] == ["ep-bbbb0001"]
    assert store.search(q, TEXT, k=10) == []
    # fréquences lexicales écrites par le pod (`_apply_df` gardé) : un df et un nombre de notes PAR locataire
    with psycopg.connect(dsn["su"]) as su:
        df = {(r[0], r[1]): r[2] for r in su.execute("SELECT tenant_id, token, df FROM lex_df").fetchall()}
        assert su.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'lex_df_pkey'"
                          ).fetchone()[0] == "PRIMARY KEY (tenant_id, token)"
    assert df[(TA, "")] == df[(TB, "")] == 1 and df[(TA, "portail")] == df[(TB, "portail")] == 1
    # la génération créée par le pod : partition au propriétaire, RLS forcée, index HNSW, lisible par la sauvegarde
    state = _rls_state(dsn["su"])
    assert state["chunks_g1"] == (True, out["owner"][0], True)
    with psycopg.connect(dsn["su"]) as su:
        assert su.execute("SELECT index_built AND status = 'active' FROM index_generations WHERE id = 1").fetchone()[0]
        assert su.execute("SELECT to_regclass('chunks_g1_hnsw') IS NOT NULL").fetchone()[0]
    with psycopg.connect(dsn["backup"]) as b:
        assert b.execute("SELECT count(DISTINCT tenant_id) FROM chunks_g1").fetchone()[0] == 2
    # 4. le pod ne peut PAS lever la RLS, ni supprimer, ni créer
    _assert_pod_locked_out(dsn["app"])
    assert ct.verify(dsn["app"]) == []
    # pod membre du propriétaire (SET ROLE possible) : refusé comme s'il possédait les tables
    with psycopg.connect(DSN, autocommit=True) as su:
        su.execute(f"GRANT {out['owner'][0]} TO {app}")
        try:
            problems = ct.verify(dsn["app"])
        finally:
            su.execute(f"REVOKE {out['owner'][0]} FROM {app}")
    assert any("membre de son propriétaire" in p for p in problems) and any("schéma public" in p for p in problems)
    # 5. migrate rejoué APRÈS l'indexation : idempotent, partition comprise
    ct.migrate_schema(dsn["owner"], app_role=app, backup_role=backup, project_tenants=dict)
    assert _rls_state(dsn["su"]) == state


@requires_pg
def test_old_form_database_reowned_then_migrated_passes(roles, monkeypatch, tmp_path):
    """Base de l'ANCIENNE forme (bêta d'avant : base, schéma et tables à app, notes sans locataire) → fichier de
    reprise (administrateur) → migrate en propriétaire → pod external conforme."""
    import psycopg
    from corthexis.store import Store

    from nokker.context import corthexis_tenancy as ct
    from nokker.context.corthexis_index import project_of
    from nokker.tenancy.context import tenant_scope

    out, _ = roles
    dsn = _db(roles, "app")
    app, owner, backup = out["app"][0], out["owner"][0], out["backup"][0]
    th_a, th_b = f"{TA}:th-1", f"{TB}:th-1"
    pa, pb = project_of(th_a), project_of(th_b)
    _note(tmp_path, pa, "ep-aaaa0001", TEXT)
    _note(tmp_path, pb, "ep-bbbb0001", TEXT)
    old = Store(dsn["app"], max_size=2)  # mode managed : le paquet crée son schéma en app
    try:
        for p in (pa, pb):
            _index(old, tmp_path, p)
        # mode managed : ensure en app accorde aussi la lecture au rôle de sauvegarde
        assert ct.ensure(dsn["app"], lambda: {pa: TA, pb: TB}, backup_role=backup)
    finally:
        old.close()
    with psycopg.connect(dsn["su"]) as su:
        assert su.execute("SELECT has_table_privilege(%s, 'notes', 'SELECT')", (backup,)).fetchone()[0]
    problems = ct.verify(dsn["app"])
    assert any("appartient au rôle du pod" in p for p in problems) and any("schéma public" in p for p in problems)
    assert any(ct.ATTACH_FN in p for p in problems)
    # reprise par l'administrateur (noms de rôles du test à la place de nokker_owner / nokker_app)
    sql = REOWN_SQL.read_text(encoding="utf-8").replace("nokker_owner", owner).replace("nokker_app", app)
    for _ in range(2):  # rejouable
        with psycopg.connect(dsn["su"], autocommit=True) as su:
            su.execute(sql)
    with psycopg.connect(dsn["su"]) as su:
        assert su.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                          "WHERE n.nspname = 'public' AND pg_get_userbyid(c.relowner) = %s", (app,)).fetchone()[0] == 0
        assert su.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()"
                          ).fetchone()[0] == owner
    rep = ct.migrate_schema(dsn["owner"], app_role=app, backup_role=backup, project_tenants=dict)
    assert rep["applied"] == []
    assert ct.verify(dsn["app"]) == []
    with psycopg.connect(dsn["su"]) as su:
        assert dict(su.execute("SELECT name, tenant_id FROM notes").fetchall()) == {"ep-aaaa0001": TA,
                                                                                   "ep-bbbb0001": TB}
        # `lex_df` de l'ancienne forme (globale, 2 notes) reconstruite par locataire (1 note chacun)
        assert dict(su.execute("SELECT tenant_id, df FROM lex_df WHERE token = ''").fetchall()) == {TA: 1, TB: 1}
    _assert_pod_locked_out(dsn["app"])
    # pod external sur la base reprise : rappel sous locataire, nouvelle note indexée
    from corthexis import service as cx_service

    real_init = Store.__init__

    def _init(self, *args, **kwargs):
        assert kwargs.get("migrate", True) is False, "le pod a ouvert un Store CortHeXis qui migre"
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(Store, "__init__", _init)
    monkeypatch.setenv("CORTHEXIS_DATABASE_URL", dsn["app"])
    ct.set_mode(ct.EXTERNAL)
    ct.set_strict(True)
    cx_service.reset()
    try:
        store = ct.guarded_store()
        q = _vec(TEXT)
        with tenant_scope(TB):
            assert [h.note_name for h in store.search(q, TEXT, k=10)] == ["ep-bbbb0001"]
        _note(tmp_path, pa, "ep-aaaa0002", "Le rendez-vous chez le notaire est vendredi")
        with tenant_scope(TA):
            _index(store, tmp_path, pa)
            assert "ep-aaaa0002" in [h.note_name for h in store.search(_vec("notaire vendredi"), "notaire vendredi")]
    finally:
        cx_service.reset()
        ct.set_mode(ct.MANAGED)
        ct.set_strict(False)


@requires_pg
def test_external_pod_does_not_serve_an_unmigrated_database(roles, external_pod):
    """Strict (`require_rls`) : base sans schéma (job `nokker migrate` pas passé) → non servie, et rien n'est créé."""
    import psycopg

    from nokker.context import corthexis_tenancy as ct

    dsn = _db(roles, "owner")
    assert ct.verify(dsn["app"]) == [
        "schéma CortHeXis absent (job `nokker migrate` avec CORTHEXIS_OWNER_DATABASE_URL)"]
    with pytest.raises(RuntimeError, match="non servie"):
        external_pod(dsn["app"])
    with psycopg.connect(dsn["su"]) as su:
        assert su.execute("SELECT to_regclass('notes') IS NULL").fetchone()[0]
