"""Objectifs et notes du correcteur sur un vrai Postgres (migration 050) : persistés sous la RLS du locataire du fil,
invisibles d'un autre locataire, relus après « redémarrage » (nouveau magasin, nouvelle connexion). Ignoré sans
`NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401 — fixture (base neuve, rôles, migrations)


@requires_pg
def test_goals_isolated_and_persisted(pg):  # noqa: F811
    import psycopg

    from nokker.goals.service import GoalService
    from nokker.goals.store import PgGoals
    from nokker.tenancy.context import tenant_scope

    svc = GoalService(store=PgGoals(pg["app"]), outcomes_enabled=True, goals_enabled=True, submit=lambda *a: None)
    with tenant_scope(A):
        g = svc.start(f"{A}:s-1", "us_aaaaaaaaaaaa", "rapport de A", criteria=["le PDF existe"],
                      budget={"tokens": 5000})
        svc.store.add_grade(g.id, g.thread_id, 3, 0, {"total": 1, "max": 2}, 1.0)
    with tenant_scope(B):
        assert svc.current(f"{A}:s-1") is None and svc.store.get(g.id) is None
        assert svc.store.grades(g.id) == [] and svc.store.live_kernel() == []
        assert svc.stop(f"{A}:s-1") is None
    again = PgGoals(pg["app"])  # « redémarrage » : rien en mémoire
    with tenant_scope(A):
        got = again.current(f"{A}:s-1")
        assert got.text == "rapport de A" and got.budget["tokens"] == 5000 and got.rubric == ["le PDF existe"]
        assert [r["grade"]["total"] for r in again.grades(g.id)] == [1]
        assert [x.id for x in again.live_kernel()] == [g.id]
    with psycopg.connect(pg["su"]) as conn:
        rows = conn.execute("SELECT tenant_id, status FROM nokker_goals").fetchall()
        grades = conn.execute("SELECT tenant_id FROM nokker_goal_grades").fetchall()
    assert rows == [(A, "active")] and grades == [(A,)]
