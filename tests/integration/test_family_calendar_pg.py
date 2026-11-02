"""Calendrier familial sur un vrai Postgres (migration 061) : RLS du locataire sur les deux tables, révision en
comparer-et-poser, brouillon d'un autre foyer introuvable, rôle applicatif sans locataire = aucune ligne.
Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401 — fixture (base neuve, rôles, migrations)

ACC = {A: "us_aaaaaaaaaaaa", B: "us_bbbbbbbbbbbb"}
EVENT = {"title": "Tournoi", "date": "2026-11-14", "start": "09:00", "source": "14 novembre 2026, 09h00"}


@requires_pg
def test_family_calendar_tables_are_isolated(pg):  # noqa: F811
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import psycopg

    from nokker.household.famcal.service import FamcalError, FamilyCalendar
    from nokker.household.famcal.store import PgFamcalStore
    from nokker.tenancy.context import tenant_scope

    now = datetime(2026, 11, 2, 9, tzinfo=ZoneInfo("Europe/Zurich")).timestamp()
    svc = FamilyCalendar(store=PgFamcalStore(pg["app"]), account_of=ACC.get, clock=lambda: now)
    with tenant_scope(A):
        svc.put_settings({"people": ["Alice", "Léo"]})
        svc.put_settings({"timezone": "Europe/Zurich"})  # upsert : les personnes restent
        assert svc.settings().people == ["Alice", "Léo"]
        d = svc.propose({"events": [EVENT]})
        assert svc.edit(d.draft_id, 1, "reject").revision == 2
        assert svc.store.save(svc.draft(d.draft_id), expected=1) is False  # révision dépassée : rien ne change
        assert [x.draft_id for x in svc.drafts()] == [d.draft_id]
    with tenant_scope(B):
        assert svc.drafts() == [] and svc.settings().people == []
        try:
            svc.draft(d.draft_id)
            raise AssertionError("brouillon d'un autre foyer lu")
        except FamcalError as e:
            assert e.status == 404
    with psycopg.connect(pg["su"]) as conn:
        assert conn.execute("SELECT tenant_id, account_id, revision FROM family_calendar_drafts").fetchall() == \
            [(A, ACC[A], 2)]
    with psycopg.connect(pg["app"]) as conn:  # aucune ligne sans locataire courant (RLS forcée)
        assert conn.execute("SELECT count(*) FROM family_calendar_drafts").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM family_calendar_settings").fetchone() == (0,)
