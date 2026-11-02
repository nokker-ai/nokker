"""Lexique de prononciation du foyer en base (migration 060, docs/VOICE.md §6) : vrai Postgres jetable, rôles de
deploy/postgres/roles.sql, store connecté en `nokker_app`. Ignoré sans `NOKKER_TEST_PG_DSN`.

Prouve : un foyer ne lit, n'écrit ni ne supprime le lexique d'un autre (store et SQL brut) ; sans locataire, rien ;
une entrée se remplace par sa clé (casse ignorée) ; la migration se rejoue."""
from __future__ import annotations

import pytest

from tests.integration.test_rls import A, B, pg, pod_env, requires_pg  # noqa: F401 — fixtures (base neuve, rôles)

pytestmark = pytest.mark.integration


@requires_pg
def test_household_lexicon_isolated(pg, pod_env):  # noqa: F811
    import psycopg

    from nokker.store.conn import connect
    from nokker.store.pg import apply_migrations
    from nokker.tenancy.context import tenant_scope
    from nokker.voice.speak_lexicon import PgLexiconStore, SpeakLexicon, SpeakScope

    sa, sb = SpeakScope(A, "hh_a"), SpeakScope(B, "hh_b")
    store = PgLexiconStore(pg["app"])
    store.put(sa, "Léa", "Lé-a", "us_a")
    store.put(sa, "léa", "Lé-ah", "us_a")  # même entrée
    store.put(sb, "Léa", "Lia", "us_b")
    assert store.entries(sa) == {"léa": "Lé-ah"} and store.entries(sb) == {"léa": "Lia"}
    assert SpeakLexicon(store=store).prepare("Léa dort", "fr", sa) == "Lé-ah dort"
    assert store.entries(SpeakScope(A, "hh_autre")) == {}  # même locataire, autre foyer
    assert store.entries(SpeakScope(B, "hh_a")) == {}  # le foyer de A lu dans le locataire de B : rien
    assert store.delete(sb, "Léa") is True and store.delete(sb, "Léa") is False
    assert store.entries(sa) == {"léa": "Lé-ah"}  # la suppression de B n'a pas touché A
    with tenant_scope(B), connect(pg["app"], autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM household_lexicon").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO household_lexicon (tenant_id, household_id, word_key, word, say) "
                         "VALUES (%s, 'hh_a', 'x', 'x', 'y')", (A,))
    with psycopg.connect(pg["app"], autocommit=True) as conn:  # sans locataire : rien
        assert conn.execute("SELECT count(*) FROM household_lexicon").fetchone()[0] == 0
    assert apply_migrations(pg["owner"]) is not False  # rejouable
    assert store.entries(sa) == {"léa": "Lé-ah"}
