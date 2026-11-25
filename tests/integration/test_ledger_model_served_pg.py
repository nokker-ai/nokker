"""Ledger Postgres : `model_served` (migration 093) sur un vrai Postgres, connexion `nokker_app` (RLS forcée). Migration
rejouée, colonne nullable, RLS toujours active, une ligne avec et une sans modèle servi. Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import pytest

from tests.integration.test_rls import pg, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

pytestmark = pytest.mark.integration

T1, T2 = "tn_mmmmmmmmmmmm", "tn_nnnnnnnnnnnn"


def _call(served: str | None = None):
    from nokker.core.turn import LLMCall, Tier, Usage

    kw = {"model_served": served} if served is not None else {}
    return LLMCall(tier=Tier.SHIP, model="qwen3.8-27b", reasoning=False, prefix_sha256="x", layer_tokens={},
                   usage=Usage(prompt=100, completion=10), latency_s=0.1, route_reason="test", **kw)


@requires_pg
def test_pg_migration_093_is_idempotent_nullable_rls_intact_and_roundtrips(pg):  # noqa: F811
    import psycopg

    from nokker.ledger.costs import PostgresLedger
    from nokker.store.conn import migrate
    from nokker.store.pg import migration_files
    from nokker.tenancy.context import tenant_scope

    names = [p.name for p in migration_files()]
    assert "093_ledger_model_served.sql" in names and len([n for n in names if n.startswith("093_")]) == 1
    sql = next(p for p in migration_files() if p.name == "093_ledger_model_served.sql").read_text(encoding="utf-8")
    assert migrate(pg["owner"], sql, rls=False, strict=True) and migrate(pg["owner"], sql, rls=False, strict=True)  # ×2
    with psycopg.connect(pg["su"], autocommit=True) as su:
        col = su.execute("SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                         "WHERE table_name = 'ledger_entries' AND column_name = 'model_served'").fetchone()
        assert col == ("text", "YES", None)
        rls = su.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                         "WHERE relname = 'ledger_entries'").fetchone()
        assert rls == (True, True)
        assert su.execute("SELECT count(*) FROM pg_policies WHERE tablename = 'ledger_entries'").fetchone()[0] >= 1
    # Ledger migré seul (chemin `PostgresLedger.migrate`) : rejoue aussi la 093 sans erreur.
    PostgresLedger(pg["owner"], migrate=True)
    ledger = PostgresLedger(pg["app"], migrate=False)  # le chemin de production
    with tenant_scope(T1):
        ledger.record("th_ms", 0, _call("vendor/fallback-397b"))
        ledger.record("th_ms", 1, _call())
    with tenant_scope(T2):
        ledger.record("th_ms_autre", 0, _call("vendor/other"))
    with tenant_scope(T1):
        rows = sorted(ledger.fetch_calls(thread_id="th_ms"), key=lambda r: r.turn_index)
        assert [(r.model, r.model_served) for r in rows] == [("qwen3.8-27b", "vendor/fallback-397b"),
                                                             ("qwen3.8-27b", None)]
        assert ledger.fetch_calls(thread_id="th_ms_autre") == []  # la RLS n'a pas bougé
    with psycopg.connect(pg["su"], autocommit=True) as su:  # ligne d'avant la colonne : NULL, jamais le modèle du palier
        su.execute("INSERT INTO ledger_entries (tenant_id, thread_id, turn_index, tier, model, reasoning, prefix_sha256, "
                   "layer_tokens, prompt_tokens, cached_tokens, completion_tokens, reasoning_tokens, cached_reported, "
                   "latency_s, route_reason, chf, cost_source) VALUES (%s,'th_old',0,'ship','m',false,'x','{}',1,0,1,0,"
                   "false,0.1,'t',0,'catalog')", (T1,))
    with tenant_scope(T1):
        assert [r.model_served for r in ledger.fetch_calls(thread_id="th_old")] == [None]
