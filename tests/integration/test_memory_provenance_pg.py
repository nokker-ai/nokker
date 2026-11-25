"""Provenance de la mémoire (migration 080, nokker/memory/provenance.py) sur un vrai Postgres, stores connectés en
`nokker_app` (sous RLS). Ignoré sans `NOKKER_TEST_PG_DSN`.

Base d'avant 080 simulée : la colonne est retirée, des lignes sont écrites telles qu'avant (épisode, note longue, fait
en attente), puis les migrations repassent. Prouve : l'existant prend `unknown_legacy`, reste lisible par chaque store
et ressort au rappel EXACTEMENT comme avant ; une nouvelle ligne garde sa provenance (aller-retour) ; un fait de tiers
ressort marqué ; la migration est rejouable."""
from __future__ import annotations

import pytest

from tests.integration.test_rls import pg, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

pytestmark = pytest.mark.integration

T = "tn_" + "v" * 12
ACC = "us_provenance01"
TABLES = ("nokker_episodes", "nokker_long_memory", "nokker_memory_pending")


@requires_pg
def test_pg_provenance_migration_keeps_existing_readable_and_new_rows_roundtrip(pg):  # noqa: F811
    from dataclasses import dataclass

    import psycopg

    from nokker.context.pg import PgEpisodeStore
    from nokker.context.types import Episode
    from nokker.learning.pg import PgLearningStore
    from nokker.learning.store import PendingNote
    from nokker.memory import provenance as P
    from nokker.memory.consolidation.stores import PgConsolidationStore
    from nokker.memory.tenant_long import LongMemory, LongMemoryPort, PgLongMemoryStore
    from nokker.store.pg import apply_migrations
    from nokker.tenancy.context import tenant_scope

    with psycopg.connect(pg["su"], autocommit=True) as su:
        su.execute("INSERT INTO tenants (id) VALUES (%s) ON CONFLICT DO NOTHING", (T,))
        su.execute("INSERT INTO accounts (id, tenant_id, email) VALUES (%s, %s, 'prov@example.com') "
                   "ON CONFLICT DO NOTHING", (ACC, T))
        for t in TABLES:  # base d'avant 080
            su.execute(f"ALTER TABLE {t} DROP COLUMN IF EXISTS provenance")
        su.execute("INSERT INTO nokker_long_memory (tenant_id, account_id, name, scope, kind, title, body, "
                   "author_account, origin, created_at, updated_at) VALUES (%s, %s, 'fact-velo-aaaaaa', 'user', "
                   "'fact', 'Vélo', 'Mon vélo est bleu', %s, 'propose:th_1', 1.0, 1.0)", (T, ACC, ACC))
        su.execute("INSERT INTO nokker_episodes (tenant_id, id, thread_id, seq, first_turn, last_turn, title, "
                   "summary, closed_at) VALUES (%s, 'ep_legacy0001', 'th_legacy', 0, 0, 3, 'Ancien', 'Résumé', 2.0)",
                   (T,))
        su.execute("INSERT INTO nokker_memory_pending (tenant_id, account_id, id, source, kind, title, body, at) "
                   "VALUES (%s, %s, 'mp_legacy0001', 'propose', 'fact', 'Ancien fait', 'Ancien fait', "
                   "'2026-11-01T00:00:00+00:00')", (T, ACC))
    assert apply_migrations(pg["owner"], strict=True)
    assert apply_migrations(pg["owner"], strict=True)  # rejouée à chaque démarrage, sans effet
    with psycopg.connect(pg["su"], autocommit=True) as su:
        for t in TABLES:
            row = su.execute("SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                             "WHERE table_name = %s AND column_name = 'provenance'", (t,)).fetchone()
            assert row == ("text", "NO", "'unknown_legacy'::text"), (t, row)

    longs = PgLongMemoryStore(pg["app"], migrate_now=False)
    svc = LongMemory(store=longs)
    port = LongMemoryPort(svc, ACC)
    episodes = PgEpisodeStore(pg["app"])
    learning = PgLearningStore(pg["app"])
    cons = PgConsolidationStore(pg["app"], migrate_now=False)

    @dataclass(frozen=True)
    class Actor:
        account_id: str
        tenant_id: str

    with tenant_scope(T):
        # l'existant : unknown_legacy, lisible, rappelé comme avant
        (r,) = svc.recall(ACC, "mon vélo est bleu", 5)
        assert r.provenance == P.UNKNOWN_LEGACY
        assert P.recall_line(r, 1200) == f"- [{r.note}] (score {r.score:.2f}) {r.text[:1200]}"
        ep = episodes.get("ep_legacy0001")
        assert ep is not None and ep.provenance == P.UNKNOWN_LEGACY and ep.summary == "Résumé"
        held = learning.get_pending(ACC, "mp_legacy0001")
        assert held is not None and held.provenance == P.UNKNOWN_LEGACY
        with cons.transaction(Actor(ACC, T)) as tx:
            assert tx.note("fact-velo-aaaaaa")["provenance"] == P.UNKNOWN_LEGACY

        # du neuf : la provenance fait l'aller-retour, un fait de tiers ressort marqué
        port.propose("th_2", ["Le code du portail est 7731"], provenance="third_party:attachment:file")
        got = [x for x in svc.recall(ACC, "code du portail", 5) if "7731" in x.text]
        assert got and got[0].provenance == "third_party:attachment:file"
        assert " (tiré d'un document joint) Le code du portail est 7731" in P.recall_line(got[0], 1200)
        port.propose("th_2", ["Un fait sans provenance connue"])
        assert [x.provenance for x in svc.recall(ACC, "fait sans provenance connue", 5)
                if "sans provenance" in x.text] == [P.THIRD_PARTY_UNKNOWN]
        new_ep = Episode(id="ep_new000001", thread_id="th_new", seq=0, first_turn=0, last_turn=1, title="Neuf",
                         summary="Résumé", closed_at=3.0, provenance="third_party:mail")
        episodes.save(new_ep)
        assert episodes.get("ep_new000001").provenance == "third_party:mail"
        new_ep.provenance = P.ASSISTANT
        episodes.save(new_ep)  # mise à jour
        assert episodes.get("ep_new000001").provenance == P.ASSISTANT
        learning.put_pending(ACC, PendingNote(id="mp_new000001", source="propose", kind="fact", title="t",
                                              text="t", at="2026-11-06T00:00:00+00:00",
                                              provenance="third_party:web:page"))
        assert learning.get_pending(ACC, "mp_new000001").provenance == "third_party:web:page"
        with cons.transaction(Actor(ACC, T)) as tx:
            snap = tx.note("fact-velo-aaaaaa")
            tx.update({**snap, "provenance": "third_party:mail", "updated_at": 5.0})
        with cons.transaction(Actor(ACC, T)) as tx:
            assert tx.note("fact-velo-aaaaaa")["provenance"] == "third_party:mail"
