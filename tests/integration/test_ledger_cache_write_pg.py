"""Ledger Postgres : `cache_write_tokens` et `call_id` (migration 090) sur un vrai Postgres, connexion `nokker_app`
(RLS forcée). Le montage `pg` crée le schéma par `migration_files()`. Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import pytest

from tests.integration.test_rls import pg, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

pytestmark = pytest.mark.integration

T1, T2 = "tn_wwwwwwwwwwww", "tn_vvvvvvvvvvvv"


def _call(*, write: int = 0, call_id: str = ""):
    from nokker.core.turn import LLMCall, Tier, Usage

    kw = {"call_id": call_id} if call_id else {}
    return LLMCall(tier=Tier.SHIP, model="m", reasoning=False, prefix_sha256="x", layer_tokens={},
                   usage=Usage(prompt=1000, completion=10, cached=400, cached_reported=True, cache_write=write),
                   latency_s=0.1, route_reason="test", **kw)


@requires_pg
def test_pg_migration_090_is_idempotent_and_ledger_roundtrips_cache_write(pg):  # noqa: F811
    import psycopg

    from nokker.ledger.costs import PostgresLedger
    from nokker.store.conn import migrate
    from nokker.store.pg import migration_files
    from nokker.tenancy.context import tenant_scope

    sql = next(p for p in migration_files() if p.name == "090_ledger_cache_write.sql").read_text(encoding="utf-8")
    assert migrate(pg["owner"], sql, rls=False, strict=True) and migrate(pg["owner"], sql, rls=False, strict=True)
    with psycopg.connect(pg["su"], autocommit=True) as su:
        cols = {r[0]: (r[1], r[2], r[3]) for r in su.execute(
            "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'ledger_entries' AND column_name IN ('cache_write_tokens', 'call_id')")}
        assert cols["cache_write_tokens"][:2] == ("integer", "NO") and cols["cache_write_tokens"][2] == "0"
        assert cols["call_id"][:2] == ("text", "YES")
        assert su.execute("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'ledger_entries'").fetchone()[0]
    ledger = PostgresLedger(pg["app"], migrate=False)  # le chemin de production : colonnes posées par les migrations
    with tenant_scope(T1):
        ledger.record("th_cw", 0, _call(write=300, call_id="nc_00112233445566aa"))
        ledger.record("th_cw", 1, _call())
    with tenant_scope(T2):
        ledger.record("th_cw_autre", 0, _call(write=50))
    with tenant_scope(T1):
        rows = sorted(ledger.fetch_calls(thread_id="th_cw"), key=lambda r: r.turn_index)
        assert [(r.cached_tokens, r.cache_write_tokens, r.new_tokens) for r in rows] == [(400, 300, 600), (400, 0, 600)]
        ids = [r.call_id for r in rows]
        assert ids[0] == "nc_00112233445566aa" and ids[1].startswith("nc_") and ids[1] != ids[0]  # posé, puis généré
        assert ledger.fetch_calls(thread_id="th_cw_autre") == []  # la RLS n'a pas bougé
