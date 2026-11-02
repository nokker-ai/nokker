"""Déclencheurs de forge sur un vrai Postgres (migration 052, RLS) : hooks, sessions de PR et livraisons cloisonnés
par locataire ; rejeu refusé. Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

from nokker.code_sessions.forge_triggers import ForgeHook, PgForgeStore, PrSession
from tests.integration.test_rls import A, B, pg, pod_env, requires_pg  # noqa: F401  (fixtures)


@requires_pg
def test_pg_forge_store_isolation_and_replay(pg, pod_env, monkeypatch):  # noqa: F811
    from nokker.tenancy.context import tenant_scope

    monkeypatch.setenv("NOKKER_PG_OWNER_DSN", pg["owner"])
    store = PgForgeStore(pg["app"])
    try:
        with tenant_scope(A):
            store.put_hook(ForgeHook("fh_0000000000000001", A, "us_a", "github", "https://git.example.org/t/app",
                                     None, "nokker", "raw:x", "default", 1.0))
            store.put_pr(PrSession(A, "fh_0000000000000001", 7, "cs_1", A + ":cs_1", "fix/a", "abc1234", "main",
                                   "open", 1.0, 1.0))
            assert store.seen("fh_0000000000000001", "d:1", 1.0) is False
            assert store.seen("fh_0000000000000001", "d:1", 2.0) is True
            assert store.pr_by_thread(A + ":cs_1").number == 7
        with tenant_scope(B):
            assert store.get_hook("fh_0000000000000001") is None
            assert store.get_pr("fh_0000000000000001", 7) is None
            assert store.seen("fh_0000000000000001", "d:1", 3.0) is False  # même clé, autre locataire
    finally:
        store.close()
