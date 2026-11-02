"""Fréquences lexicales (`lex_df`, IDF) de la base CortHeXis par locataire (nokker/context/corthexis_tenancy.py,
docs/MEMORY-TENANCY.md §5.2) : vrai Postgres pgvector jetable, VRAI paquet `corthexis`, rôle propriétaire non super
sans BYPASSRLS. Ignoré sans `NOKKER_TEST_PG_DSN` ou sans le paquet.

Canal fermé : `lex_df` globale faisait dépendre les poids lexicaux (log(1 + N/df)) d'un locataire des notes des autres
— un autre locataire qui ajoute des notes portant un mot rare change le classement observé (et dit que le mot existe
ailleurs). Prouve : (1) base d'AVANT (`lex_df` sans locataire) → `ensure` reconstruit un df PAR locataire depuis ses
notes, rejouée sans rien changer ; (2) le classement (noms ET scores) de B ne bouge pas quand A ajoute puis retire des
notes au mot rare ; (3) sous B, aucune ligne `lex_df` de A n'est lisible ; (4) le df tenu au fil des écritures égale le
df recalculé. Mode `external` (`migrate_schema`, `verify`, pod qui indexe) : tests/integration/test_corthexis_tenancy_pg.py."""
from __future__ import annotations

import os
import secrets
import uuid

import pytest

pytestmark = pytest.mark.integration
DSN = os.environ.get("NOKKER_TEST_PG_DSN")
requires_pg = pytest.mark.skipif(not DSN, reason="NOKKER_TEST_PG_DSN non posé — pas de Postgres jetable")

TA, TB = "tn_aaaaaaaaaaaa", "tn_bbbbbbbbbbbb"
QUERY = "quokka xylophone"
B_NOTES = {"ep-bbbb0001": "Le quokka du parc a mangé les feuilles du jardin ce matin",
           "ep-bbbb0002": "Le xylophone de la salle de musique est accordé pour samedi",
           "ep-bbbb0003": "La réunion du comité est déplacée à jeudi soir"}


def _note(root, project: str, name: str, body: str) -> None:
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\nname: {name}\ndescription: \"souvenir\"\ntype: project\n---\n# Souvenir\n\n"
                                  f"{body}\n", encoding="utf-8")


def _index(store, root, project: str) -> None:
    from corthexis.indexer import IndexConfig, Indexer

    from nokker.memory.tenant_long import HashEmbedder

    cfg = IndexConfig.from_env(memory_dir=root / project, project=project, normalize=False, write_index=False,
                               chunk_target=300)
    Indexer(store, HashEmbedder(dim=768), cfg).run()


def _ranking(store, projects: tuple[str, ...] | None = None) -> list[tuple[str, float]]:
    from nokker.memory.tenant_long import HashEmbedder

    q = HashEmbedder(dim=768).embed_query(QUERY)
    return [(h.note_name, round(h.score, 9)) for h in store.search(q, QUERY, k=10, projects=projects)]


def _df_by_tenant(su_dsn: str) -> dict[tuple[str, str], int]:
    import psycopg

    with psycopg.connect(su_dsn) as su:
        return {(r[0], r[1]): r[2] for r in su.execute("SELECT tenant_id, token, df FROM lex_df").fetchall()}


def _df_recomputed(su_dsn: str) -> dict[tuple[str, str], int]:
    import psycopg

    with psycopg.connect(su_dsn) as su:
        rows = su.execute("SELECT tenant_id, t, count(*) FROM notes, unnest(lex_tokens) AS t GROUP BY 1, 2 "
                          "UNION ALL SELECT tenant_id, '', count(*) FROM notes GROUP BY 1").fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


@pytest.fixture()
def cx(tmp_path):
    if not DSN:
        pytest.skip("NOKKER_TEST_PG_DSN non posé")
    pytest.importorskip("corthexis.indexer", reason="paquet corthexis absent")
    import psycopg
    from psycopg.conninfo import make_conninfo

    db, role, pw = f"cxdf_{uuid.uuid4().hex[:8]}", f"nk_cxdf_{uuid.uuid4().hex[:6]}", secrets.token_hex(12)
    with psycopg.connect(DSN, autocommit=True) as su:
        su.execute(f'CREATE DATABASE "{db}"')
        su.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{pw}' NOSUPERUSER NOBYPASSRLS")
    su_db = make_conninfo(DSN, dbname=db)
    with psycopg.connect(su_db, autocommit=True) as su:
        su.execute("CREATE EXTENSION IF NOT EXISTS vector")
        su.execute(f"ALTER SCHEMA public OWNER TO {role}")
    yield {"app": make_conninfo(DSN, dbname=db, user=role, password=pw), "su": su_db, "root": tmp_path}
    with psycopg.connect(DSN, autocommit=True) as su:
        su.execute(f'DROP DATABASE "{db}" WITH (FORCE)')
        su.execute(f"DROP ROLE {role}")


@requires_pg
def test_ranking_of_a_tenant_does_not_move_when_another_adds_a_rare_word(cx):
    import psycopg
    from corthexis.store import Store

    from nokker.context import corthexis_tenancy as ct
    from nokker.context.corthexis_index import project_of
    from nokker.tenancy.context import tenant_scope

    root = cx["root"]
    pa, pb = project_of(f"{TA}:th-1"), project_of(f"{TB}:th-1")
    pa2 = project_of(f"{TA}:th-2")
    # 1. base d'AVANT : notes de B et une note de A, `lex_df` globale (sans locataire)
    for name, body in B_NOTES.items():
        _note(root, pb, name, body)
    _note(root, pa, "ep-aaaa0001", "Le menu de la cantine change lundi")
    store = Store(cx["app"], max_size=2)
    try:
        for p in (pa, pb):
            _index(store, root, p)
        assert ct.ensure(cx["app"], lambda: {pa: TA, pb: TB})
        assert ct.ensure(cx["app"], dict)  # rejouée
        ct.guard_store(store)
        with tenant_scope(TB):
            base = _ranking(store)
        assert [n for n, _ in base][:2] and {n for n, _ in base} <= set(B_NOTES)
        # 2. A ajoute des notes portant le mot rare « xylophone » (et l'autre mot de la requête une fois). Avant le
        #    correctif, le poids de « xylophone » baissait chez B et l'ordre de B s'inversait : B apprenait que le
        #    mot abonde ailleurs.
        for i in range(2, 14):
            _note(root, pa2, f"ep-aaaa{i:04d}", f"Note {i} : le xylophone de l'école est en métal")
        _note(root, pa2, "ep-aaaa0099", "Le quokka est un petit marsupial d'Australie")
        with tenant_scope(TA):
            _index(store, root, pa2)
        with tenant_scope(TB):
            after = _ranking(store)
        assert after == base, (base, after)
        # 3. reconstruction à la pose (avant les ajouts de A) : un compte de notes PAR locataire, rejouée sans effet
        df = _df_by_tenant(cx["su"])
        assert df[(TB, "")] == 3 and df[(TA, "")] == 14 and df[(TB, "xylophone")] == 1
        assert ct.ensure(cx["app"], dict) and _df_by_tenant(cx["su"]) == df
        # sous B : aucune ligne `lex_df` de A, le df de « xylophone » est celui de B seul
        with psycopg.connect(cx["app"]) as c:
            c.execute("SELECT set_config('app.tenant_id', %s, false)", (TB,))
            assert {r[0] for r in c.execute("SELECT tenant_id FROM lex_df").fetchall()} == {TB}
            assert c.execute("SELECT df FROM lex_df WHERE token = 'xylophone'").fetchone()[0] == 1
            c.execute("SELECT set_config('app.tenant_id', '', false)")
            assert c.execute("SELECT count(*) FROM lex_df").fetchone()[0] == 0  # hors locataire : rien
        # 4. le df tenu au fil des écritures (ajouts, puis suppressions par l'indexeur) = df recalculé
        assert _df_by_tenant(cx["su"]) == _df_recomputed(cx["su"])
        for p in (root / pa2).iterdir():
            p.unlink()
        with tenant_scope(TA):
            _index(store, root, pa2)
        with tenant_scope(TB):
            assert _ranking(store) == base
        assert _df_by_tenant(cx["su"]) == _df_recomputed(cx["su"])
        assert _df_by_tenant(cx["su"]).get((TA, "xylophone")) is None
    finally:
        store.close()


@requires_pg
def test_single_tenant_ranking_is_identical_before_and_after_the_rebuild(cx):
    """Lokkal mono-utilisateur (un seul locataire, le locataire interne) : le df par locataire est le df global, le
    classement (noms ET scores) est identique avant et après la pose — d'où l'absence de drapeau."""
    from corthexis.store import Store

    from nokker.context import corthexis_tenancy as ct
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    root = cx["root"]
    for name, body in B_NOTES.items():
        _note(root, "p-0123456789abcdef", name, body)
    for i in range(4):
        _note(root, "u-local", f"pref-{i}", f"Préférence {i} : le xylophone se range au salon")
    store = Store(cx["app"], max_size=2)
    try:
        for p in ("p-0123456789abcdef", "u-local"):
            _index(store, root, p)
        base = _ranking(store)  # base d'avant : ni locataire ni RLS, `lex_df` globale
        assert ct.ensure(cx["app"], dict)
        ct.guard_store(store)
        with tenant_scope(INTERNAL_TENANT):
            assert _ranking(store) == base and len(base) >= 3
        assert _df_by_tenant(cx["su"]) == _df_recomputed(cx["su"])
    finally:
        store.close()


@requires_pg
def test_search_under_a_role_that_bypasses_rls_reads_only_the_current_tenant_counts(cx):
    """Rôle qui passe outre la RLS (superutilisateur : `POSTGRES_USER` du compose, banc bench/keys) : la RLS ne borne
    plus `lex_df`. Avant le correctif, `SELECT df FROM lex_df WHERE token = ''` rendait une ligne PAR locataire dès le
    2e locataire indexé → `CardinalityViolation`, recherche coupée, rappel sémantique nul. Après : les lectures de la
    recherche sont bornées au locataire courant, classement (noms ET scores) identique à celui du rôle du pod."""
    from corthexis.store import Store

    from nokker.context import corthexis_tenancy as ct
    from nokker.context.corthexis_index import project_of
    from nokker.tenancy.context import tenant_scope

    root = cx["root"]
    pa, pb = project_of(f"{TA}:th-1"), project_of(f"{TB}:th-1")
    for name, body in B_NOTES.items():
        _note(root, pb, name, body)
    _note(root, pa, "ep-aaaa0001", "Le xylophone de la cantine change lundi")
    app = Store(cx["app"], max_size=2)
    su = None
    try:
        for p in (pa, pb):
            _index(app, root, p)
        assert ct.ensure(cx["app"], lambda: {pa: TA, pb: TB})
        ct.guard_store(app)
        with tenant_scope(TB):  # le noyau cherche toujours dans le projet du fil (corthexis_index.search)
            base = _ranking(app, (pb,))  # rôle du pod, sous RLS
        assert [n for n, _ in base][:2] and {n for n, _ in base} <= set(B_NOTES)
        su = ct.guard_store(Store(cx["su"], max_size=2, migrate=False))
        with tenant_scope(TB):
            assert _ranking(su, (pb,)) == base
        with tenant_scope(TA):
            assert [n for n, _ in _ranking(su, (pa,))] == ["ep-aaaa0001"]
    finally:
        app.close()
        if su is not None:
            su.close()


def test_lexdf_reads_of_the_package_are_the_ones_scoped():
    """Les deux lectures de `lex_df` remplacées existent telles quelles dans la recherche du paquet épinglé (sinon le
    correctif ne s'appliquerait pas et la seule RLS bornerait)."""
    store_mod = pytest.importorskip("corthexis.store", reason="paquet corthexis absent")
    from nokker.context.corthexis_tenancy import LEXDF_READS

    for old, new in LEXDF_READS:
        assert store_mod.Store._Q_CTES.count(old) == 1
        assert "tenant_id = current_setting('app.tenant_id', true)" in new
