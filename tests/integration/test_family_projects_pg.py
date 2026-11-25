"""Projets du foyer sur un vrai Postgres (migration 084), sous le rôle applicatif (RLS) : brouillon chez l'auteur
(révision en comparer-et-poser, réservation à l'écriture), projet et tâches dans le locataire du FOYER, lus par une
enfant d'un autre locataire SEULEMENT par le service (ses tâches), coche, relance (une par jour), replanification,
départ d'un membre, réglage ; rôle applicatif sans locataire = aucune ligne. Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401 — fixture (base neuve, rôles, migrations)

ACC = {A: "us_aaaaaaaaaaaa", B: "us_bbbbbbbbbbbb"}
HID = "pj_household0001"


@requires_pg
def test_family_projects_tables_under_rls(pg):  # noqa: F811
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import psycopg

    from nokker.household.projects.review import Person
    from nokker.household.projects.service import FamilyProjects, Home, ProjectError
    from nokker.household.projects.store import PgProjectStore
    from nokker.tenancy.context import tenant_scope

    people = (Person(ACC[A], "Nina"), Person(ACC[B], "Léa", "child"))
    clock = {"now": datetime(2026, 11, 7, 10, tzinfo=ZoneInfo("Europe/Zurich")).timestamp()}

    def home_of(acc: str) -> Home | None:
        me = next((p for p in people if p.account_id == acc), None)
        return Home(HID, A, me, people) if me else None

    svc = FamilyProjects(store=PgProjectStore(pg["app"]), account_of=ACC.get, home_of=home_of,
                         clock=lambda: clock["now"])
    plan = {"title": "Rentrée", "deadline": "2026-12-20", "members": ["Léa"], "budget": {"amount": 80},
            "lines": [{"kind": "milestone", "title": "Fournitures", "due": "2026-11-20"},
                      {"title": "Couvrir les cahiers", "assignee": "Léa", "due": "2026-11-14"},
                      {"title": "Acheter le sac", "assignee": "Nina", "due": "2026-11-09"}]}
    with tenant_scope(A):
        d = svc.propose(plan)
        assert svc.edit(d.draft_id, [3], "update", {"due": "2026-11-10"}).revision == 2
        assert svc.store.save_draft(svc.draft(d.draft_id), expected=1) is False  # révision dépassée : rien
        try:
            svc.commit(d.draft_id, 1)
            raise AssertionError("carte d'une révision dépassée écrite")
        except ProjectError as e:
            assert e.status == 409
        res = svc.commit(d.draft_id, 2)
        assert res["tasks"] == 2 and res["milestones"] == 1
        try:
            svc.commit(d.draft_id, 2)
            raise AssertionError("brouillon écrit deux fois")
        except ProjectError as e:
            assert e.status == 409
        pid = res["project_id"]
        svc.put_settings({"overdue_by_member": True})
        svc.put_settings({"overdue_by_member": False})  # upsert
        svc.put_settings({"wall_ha": ["sensor.salon"]})
        svc.put_settings({"wall_place": {"label": "Maison", "lat": 46.2, "lon": 6.1, "stops": ["8501120"]}})
        assert svc.store.settings(HID).wall_place == {"label": "Maison", "lat": 46.2, "lon": 6.1, "stops": ["8501120"]}
        st = svc.store.settings(HID)
        st.guest_until, st.shopping_done = 123.0, ["sh_aaaaaaaaaaaa"]
        svc.store.put_settings(st)  # colonnes du mur (F1)
        got = svc.store.settings(HID)
        assert (got.overdue_by_member, got.guest_until, got.shopping_done, got.wall_ha) == \
            (False, 123.0, ["sh_aaaaaaaaaaaa"], ["sensor.salon"])
    with tenant_scope(B):  # l'enfant, dans SON locataire : rien en direct, ses tâches par le service
        assert svc.store.projects(HID) == [] and svc.store.tasks(HID) == []
        mine = svc.member_tasks()["tasks"]
        assert [t["title"] for t in mine] == ["Couvrir les cahiers"]
        assert svc.set_done(mine[0]["id"])["state"] == "done"
        assert svc.settings().overdue_by_member is False
    with tenant_scope(A):
        sac = next(t for t in svc.store.tasks(HID) if t.assignee == ACC[A])
        lea = next(t for t in svc.store.tasks(HID) if t.assignee == ACC[B])
        assert svc.store.nudge(HID, sac.task_id, ACC[A], "rappel", clock["now"], 3600) is not None
        assert svc.store.nudge(HID, sac.task_id, ACC[A], "encore", clock["now"] + 60, 3600) is None
        assert svc.store.nudge(HID, lea.task_id, ACC[A], "fait", clock["now"], 3600) is None  # déjà faite
        clock["now"] += 3 * 86400  # 10.11 : le sac (10.11) n'est pas encore en retard ; 11.11 oui
        clock["now"] += 86400
        r = svc.replan(pid, 2)
        res = svc.commit(r.draft_id, 1)
        assert res["updated"] == 1 and res["skipped_done_meanwhile"] == 0
        assert svc.store.task(HID, sac.task_id).due == "2026-11-13"
        assert svc.store.task(HID, lea.task_id).status == "done"
        assert svc.member_gone(HID, A, ACC[B]) == 1
        [p] = svc.store.projects(HID)
        assert ACC[B] not in p.members and p.budget == {"amount": "80.00", "currency": "CHF"}
    with psycopg.connect(pg["su"]) as conn:
        assert conn.execute("SELECT DISTINCT tenant_id FROM household_tasks").fetchall() == [(A,)]
        assert conn.execute("SELECT DISTINCT tenant_id FROM household_project_drafts").fetchall() == [(A,)]
    with psycopg.connect(pg["app"]) as conn:  # aucune ligne sans locataire courant (RLS forcée)
        for t in ("household_project_drafts", "household_projects", "household_tasks", "household_project_settings"):
            assert conn.execute(f"SELECT count(*) FROM {t}").fetchone() == (0,), t
