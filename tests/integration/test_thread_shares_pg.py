"""Liens de conversation partagés sur un vrai Postgres (migration 044) : instantané sous la RLS du locataire, empreinte
du jeton hors RLS, révocation immédiate des deux côtés, expiration, jamais le jeton en clair en base. Ignoré sans
`NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import pytest

from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401 — fixture (base neuve, rôles, migrations)


class _Clock:
    def __init__(self) -> None:
        self.t = 1_900_000_000.0

    def __call__(self) -> float:
        return self.t


@requires_pg
def test_shares_isolated_hashed_revoked_and_expired(pg):  # noqa: F811
    import psycopg

    from nokker.tenancy.context import tenant_scope
    from nokker.threads.shares import PgShares, ShareNotFound, ThreadShares, token_sha256

    clock = _Clock()
    svc = ThreadShares(PgShares(pg["app"]), clock=clock)
    snap = {"format": "nokker-thread-export/1", "thread": {"title": "Bilan"}, "messages": [{"role": "user",
                                                                                         "content": "de A"}]}
    with tenant_scope(A):
        meta, token = svc.create(f"{A}:s-1", "us_aaaaaaaaaaaa", snap, expires_in=3600, include_tools=True)
        assert [m.share_id for m in svc.list(f"{A}:s-1")] == [meta.share_id]
    with tenant_scope(B):
        assert svc.list(f"{A}:s-1") == []  # la RLS : B ne voit pas le partage de A
        with pytest.raises(ShareNotFound):
            svc.revoke(meta.share_id)
        with pytest.raises(ShareNotFound):
            svc.get(meta.share_id)
    # jeton jamais en clair : ni dans l'instantané ni dans l'index
    with psycopg.connect(pg["su"]) as conn:
        rows = conn.execute("SELECT token_sha256, tenant_id FROM thread_share_tokens").fetchall()
        dump = str(conn.execute("SELECT * FROM thread_shares").fetchall()) + str(rows)
    assert rows == [(token_sha256(token), A)] and token not in dump
    # lecture publique : sans locataire courant, l'instantané de A et rien d'autre
    pub = svc.public(token)
    assert pub is not None and pub["messages"] == snap["messages"] and pub["share"]["expires_at"]
    assert svc.public(token[:-1] + ("A" if token[-1] != "A" else "B")) is None
    # expiration
    clock.t += 3601
    assert svc.public(token) is None
    clock.t -= 3601
    # B ne révoque pas ; A révoque : 404 immédiat, et l'empreinte porte la révocation
    with tenant_scope(A):
        assert svc.revoke(meta.share_id).revoked_at is not None
    assert svc.public(token) is None
    with psycopg.connect(pg["su"]) as conn:
        assert conn.execute("SELECT revoked_at IS NOT NULL FROM thread_share_tokens").fetchone() == (True,)
    # suppression du fil : tous ses partages révoqués
    with tenant_scope(A):
        _, t2 = svc.create(f"{A}:s-2", "us_aaaaaaaaaaaa", snap)
        assert svc.public(t2) is not None
        assert svc.revoke_thread(f"{A}:s-2") == 1
    assert svc.public(t2) is None
