"""Apprentissage en base (migration 046, docs/LEARNING.md §5) : vrai Postgres jetable, rôles de
deploy/postgres/roles.sql, store connecté en `nokker_app` (ni propriétaire, ni BYPASSRLS). Ignoré sans
`NOKKER_TEST_PG_DSN`.

Prouve : skills appris, notes en attente et journal d'un locataire n'existent pas pour un autre (par le store ET en SQL
brut) ; une écriture qui force le locataire d'un autre est refusée ; sans locataire, rien ; deux comptes d'un même
locataire ne se voient pas par le store ; une version déjà posée ne se repose pas (conflit) ; une note ne se décide
qu'une fois."""
from __future__ import annotations

import pytest

from tests.integration.test_rls import A, B, pg, pod_env, requires_pg  # noqa: F401 — fixtures (base neuve, rôles)

pytestmark = pytest.mark.integration


@requires_pg
def test_learning_tables_are_isolated_by_rls(pg, pod_env):  # noqa: F811
    from types import SimpleNamespace

    import psycopg

    import nokker.tools.skills as sk
    from nokker.learning.pending import PendingMemory
    from nokker.learning.pg import PgLearningStore
    from nokker.learning.skills import LearnedSkills
    from nokker.learning.store import LearningConflict, SkillVersion
    from nokker.store.conn import connect
    from nokker.tenancy.context import NoTenant, tenant_scope

    res = SimpleNamespace(verdict="safe", summary="ok")
    old = sk._guard_module
    sk._guard_module = lambda: SimpleNamespace(scan_skill=lambda p, **kw: res)
    try:
        store = PgLearningStore(pg["app"])
        skills = LearnedSkills(store=store)
        accounts = {f"{A}:t": "us_aaaaaaaaaaa1", f"{B}:t": "us_bbbbbbbbbbb1"}
        pending = PendingMemory(store=store, account_of_thread=accounts.get)
        args = {"name": "publier-docs", "description": "quand publier", "body": "# étapes\n1. make docs\n"}
        ua, ua2, ub = "us_aaaaaaaaaaa1", "us_aaaaaaaaaaa2", "us_bbbbbbbbbbb1"
        with tenant_scope(A):
            skills.create(args, ua, None)
            skills.create({**args, "body": "v2", "reason": "r"}, ua, None, update=True)
            [held] = pending.hold_facts(f"{A}:t", ["fait de A"])
            with pytest.raises(LearningConflict):
                store.add_skill_version(ua, SkillVersion(name="publier-docs", version=2, content="x", sha256="x",
                                                         op="update", at="2026-11-01T00:00:00+00:00"))
            assert store.skill_names(ua2) == [] and store.list_pending(ua2) == []  # autre compte, même locataire
        with tenant_scope(B):
            skills.create(args, ub, None)
            assert [v.version for v in store.skill_versions(ub, "publier-docs")] == [1]
            assert store.skill_versions(ua, "publier-docs") == [] and store.list_pending(ua) == []
            assert store.journal(ua) == [] and store.get_pending(ua, held.id) is None
            assert store.decide_pending(ua, held.id, "approved", ub, "t") is None  # rien à toucher chez A
        with tenant_scope(A):
            assert [v.version for v in store.skill_versions(ua, "publier-docs")] == [1, 2]
            assert pending.decide(ua, held.id, approve=False, by=ua).status == "rejected"
            assert store.decide_pending(ua, held.id, "approved", ua, "t") is None  # une seule décision
            kinds = [e.kind for e in store.journal(ua)]
            assert kinds == ["memory.rejected", "memory.pending", "skill.updated", "skill.created"]
        for table in ("nokker_learned_skills", "nokker_memory_pending", "nokker_learning_journal"):
            for tid, other in ((A, B), (B, A)):
                with tenant_scope(tid), connect(pg["app"], autocommit=True) as conn:
                    assert conn.execute(f"SELECT count(*) FROM {table} WHERE tenant_id <> %s",
                                        (tid,)).fetchone()[0] == 0
        with tenant_scope(B), connect(pg["app"], autocommit=True) as conn, \
                pytest.raises(psycopg.errors.InsufficientPrivilege):  # forcer le locataire d'un autre : refusé
            conn.execute("INSERT INTO nokker_learning_journal (tenant_id, account_id, id, kind, subject, at) "
                         "VALUES (%s, 'us_x', 'lj_forced', 'skill.created', 's', 't')", (A,))
        with pytest.raises(NoTenant):
            store.skill_names(ua)
        with psycopg.connect(pg["app"], autocommit=True) as conn:  # sans app.tenant_id : rien
            assert conn.execute("SELECT count(*) FROM nokker_learned_skills").fetchone()[0] == 0
    finally:
        sk._guard_module = old
