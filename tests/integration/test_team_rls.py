"""Équipe (lot X17) en base : vrai Postgres jetable, rôles de deploy/postgres/roles.sql, le service connecté en
`nokker_app` (ni propriétaire, ni BYPASSRLS). Ignoré sans `NOKKER_TEST_PG_DSN`.

Le service vérifie les rôles (tests/unit/team) ; ici, on prouve que la BASE tient la même ligne quand le code se
trompe : SQL brut avec le locataire et le compte d'un acteur. Le propriétaire (locataire A) partage un projet avec un
membre (B) et un viewer (C) de deux autres locataires ; D n'est membre de rien.
"""
from __future__ import annotations

import time

import pytest

from tests.integration.test_rls import pg, pod_env, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

pytestmark = pytest.mark.integration


class Actor:
    def __init__(self, tenant_id: str, account_id: str, email: str) -> None:
        self.tenant_id, self.account_id, self.email, self.status = tenant_id, account_id, email, "active"


class Accounts:
    def __init__(self, *actors: Actor) -> None:
        self.by_id = {a.account_id: a for a in actors}

    def account(self, account_id):
        return self.by_id.get(account_id)

    def account_by_email(self, email):
        return next((a for a in self.by_id.values() if a.email == email), None)


OWNER = Actor("tn_aaaaaaaaaaaa", "us_owner000001", "owner@example.org")
MEM = Actor("tn_bbbbbbbbbbbb", "us_member00001", "mem@example.org")
VIEW = Actor("tn_cccccccccccc", "us_viewer00001", "view@example.org")
OUT = Actor("tn_dddddddddddd", "us_outsider001", "out@example.org")
TEAM_TABLES = ["team_projects", "team_access", "team_members", "team_invites", "team_skills", "team_notes",
               "team_note_quarantine"]


@pytest.fixture(scope="module")
def world(pg):  # noqa: F811
    from nokker.team.service import Team
    from nokker.team.store import PgTeamStore
    from nokker.tenancy.context import tenant_scope

    store = PgTeamStore(pg["app"], migrate_now=False)  # migrations déjà faites en propriétaire par la fixture
    team = Team(store=store, tenancy=Accounts(OWNER, MEM, VIEW, OUT), mirror_allowed=lambda acc: True)
    with tenant_scope(OWNER.tenant_id):
        pid = team.create(OWNER, {"name": "Plateforme", "repos": ["github.com/acme/api"]})["id"]
        team.patch(OWNER, pid, {"mirror": True})
        team.put_skill(OWNER, pid, "lint", {"source": "official/dev/lint"})
        team.put_secret(OWNER, pid, "DB_URL", {"description": "préprod"})
        inv_m = team.invite(OWNER, pid, {"email": MEM.email, "role": "member"})
        inv_v = team.invite(OWNER, pid, {"email": VIEW.email, "role": "viewer"})
    with tenant_scope(MEM.tenant_id):
        team.accept(MEM, inv_m["id"])
    with tenant_scope(VIEW.tenant_id):
        team.accept(VIEW, inv_v["id"])
    return {"team": team, "pid": pid, "pg": pg}


def _raw(dsn, actor, sql, params=(), *, account=True):
    """SQL brut en `nokker_app`, au nom de l'acteur : son locataire, et (sauf `account=False`) son compte."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (actor.tenant_id,))
        if account:
            conn.execute("SELECT set_config('app.account_id', %s, false), set_config('app.account_email', %s, false)",
                         (actor.account_id, actor.email))
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if cur.description else cur.rowcount
        conn.commit()
        return rows


def _refused(dsn, actor, sql, params=()):
    import psycopg

    try:
        n = _raw(dsn, actor, sql, params)
    except psycopg.errors.InsufficientPrivilege:  # WITH CHECK d'une politique RLS
        return True
    return n == 0  # UPDATE/DELETE sans ligne visible : rien n'est touché


@requires_pg
def test_members_of_other_tenants_read_the_project_through_the_service(world):
    from nokker.tenancy.context import tenant_scope

    t, pid = world["team"], world["pid"]
    for who, role in ((MEM, "member"), (VIEW, "viewer")):
        with tenant_scope(who.tenant_id):
            assert t.get(who, pid)["role"] == role
            assert len(t.members(who, pid)["members"]) == 3
            assert [s["name"] for s in t.skills(who, pid)["skills"]] == ["lint"]
            assert t.secrets(who, pid)["secrets"] == [{"name": "DB_URL", "description": "préprod"}]
    with tenant_scope(OUT.tenant_id):
        assert t.list_projects(OUT)["projects"] == []
        with pytest.raises(Exception) as e:
            t.get(OUT, pid)
        assert getattr(e.value, "status", None) == 404


@requires_pg
def test_outsider_sees_no_team_row_at_all(world):
    dsn = world["pg"]["app"]
    for table in TEAM_TABLES:
        assert _raw(dsn, OUT, f"SELECT * FROM {table}") == [], table
        # sans compte posé, même un membre ne voit rien hors de son locataire
        assert _raw(dsn, MEM, f"SELECT * FROM {table}", account=False) == [], table
        # compte vide : aucune politique d'équipe ne s'ouvre (NULLIF)
        assert _raw(dsn, Actor(MEM.tenant_id, "", ""), f"SELECT * FROM {table}") == [], table
    # le membre lit le projet, mais jamais le reste du locataire du propriétaire
    assert len(_raw(dsn, MEM, "SELECT * FROM team_projects")) == 1
    # des invitations du projet, le membre ne voit que celle qui lui était adressée
    assert _raw(dsn, MEM, "SELECT email, state FROM team_invites") == [(MEM.email, "accepted")]


@requires_pg
def test_viewer_cannot_write_even_in_raw_sql(world):
    dsn, pid = world["pg"]["app"], world["pid"]
    a = OWNER.tenant_id
    now = time.time()
    assert _refused(dsn, VIEW, "UPDATE team_projects SET name = 'pris' WHERE project_id = %s", (pid,))
    assert _refused(dsn, VIEW, "INSERT INTO team_skills (project_id, tenant_id, name, source, added_at) "
                               "VALUES (%s, %s, 'x', 'official/a/b', %s)", (pid, a, now))
    assert _refused(dsn, VIEW, "DELETE FROM team_skills WHERE project_id = %s", (pid,))
    assert _refused(dsn, VIEW, "INSERT INTO team_notes (project_id, tenant_id, name, kind, updated_at) "
                               "VALUES (%s, %s, 'fact-x', 'fact', %s)", (pid, a, now))
    assert _refused(dsn, VIEW, "UPDATE team_access SET role = 'owner' WHERE project_id = %s", (pid,))
    assert _refused(dsn, VIEW, "UPDATE team_members SET role = 'maintainer' WHERE project_id = %s", (pid,))
    assert _refused(dsn, VIEW, "DELETE FROM team_members WHERE project_id = %s AND account_id <> %s",
                    (pid, VIEW.account_id))
    assert _raw(dsn, OWNER, "SELECT name FROM team_projects WHERE project_id = %s", (pid,)) == [("Plateforme",)]


@requires_pg
def test_member_writes_notes_but_cannot_promote_or_join_elsewhere(world):
    from nokker.tenancy.context import tenant_scope

    dsn, pid, t = world["pg"]["app"], world["pid"], world["team"]
    a, now = OWNER.tenant_id, time.time()
    with tenant_scope(MEM.tenant_id):
        r = t.push_notes(MEM, pid, "mc_mem", [{"name": "fact-ci", "kind": "fact", "title": "CI", "text": "Forgejo",
                                               "updated_at": now, "locality": "both"}])
    assert r["accepted"] == 1
    with tenant_scope(VIEW.tenant_id):
        assert [n["name"] for n in t.notes(VIEW, pid, None)["notes"]] == ["fact-ci"]
    # la note vit au locataire du projet, pas à celui du membre
    assert _raw(dsn, OWNER, "SELECT tenant_id, author_account FROM team_notes") == [(a, MEM.account_id)]
    # member : pas le catalogue, pas de promotion, pas d'entrée sans invitation
    assert _refused(dsn, MEM, "INSERT INTO team_skills (project_id, tenant_id, name, source, added_at) "
                              "VALUES (%s, %s, 'y', 'official/a/b', %s)", (pid, a, now))
    assert _refused(dsn, MEM, "UPDATE team_access SET role = 'maintainer' WHERE account_id = %s", (MEM.account_id,))
    assert _refused(dsn, OUT, "INSERT INTO team_access (project_id, tenant_id, account_id, role, added_at) "
                              "VALUES (%s, %s, %s, 'member', %s)", (pid, a, OUT.account_id, now))
    assert _refused(dsn, OUT, "INSERT INTO team_members (project_id, tenant_id, account_id, role, added_at) "
                              "VALUES (%s, %s, %s, 'member', %s)", (pid, a, OUT.account_id, now))
    # se faire entrer à la place d'un autre compte, même avec une invitation à soi : refusé
    assert _refused(dsn, MEM, "INSERT INTO team_access (project_id, tenant_id, account_id, role, added_at) "
                              "VALUES (%s, %s, %s, 'member', %s)", (pid, a, OUT.account_id, now))
    # un projet ne change pas de locataire par un membre (trigger : les politiques permissives se cumulent)
    assert _refused(dsn, MEM, "UPDATE team_notes SET tenant_id = %s WHERE project_id = %s", (MEM.tenant_id, pid))
    assert _raw(dsn, OWNER, "SELECT tenant_id FROM team_notes") == [(a,)]
    # une ligne glissée dans SON locataire (sa `tenant_isolation` l'accepte) ne se mêle jamais au projet
    _raw(dsn, MEM, "INSERT INTO team_skills (project_id, tenant_id, name, source, added_at) "
                   "VALUES (%s, %s, 'forgee', 'official/a/b', %s)", (pid, MEM.tenant_id, now))
    with tenant_scope(MEM.tenant_id):
        assert [s["name"] for s in t.skills(MEM, pid)["skills"]] == ["lint"]
    with tenant_scope(VIEW.tenant_id):
        assert [s["name"] for s in t.skills(VIEW, pid)["skills"]] == ["lint"]
    assert _raw(dsn, VIEW, "SELECT name FROM team_skills") == [("lint",)]


@requires_pg
def test_invitation_is_the_only_way_in_and_must_match_the_role(world):
    from nokker.tenancy.context import tenant_scope

    dsn, pid, t = world["pg"]["app"], world["pid"], world["team"]
    a, now = OWNER.tenant_id, time.time()
    late = Actor("tn_eeeeeeeeeeee", "us_latecomer01", "late@example.org")
    with tenant_scope(a):
        inv = t.invite(OWNER, pid, {"email": late.email, "role": "viewer"})  # compte inconnu à l'invitation
    assert _raw(dsn, late, "SELECT project_name, role FROM team_invites") == [("Plateforme", "viewer")]
    # l'invitation est « viewer » : entrer « maintainer » est refusé en base
    assert _refused(dsn, late, "INSERT INTO team_access (project_id, tenant_id, account_id, role, added_at) "
                               "VALUES (%s, %s, %s, 'maintainer', %s)", (pid, a, late.account_id, now))
    t.tenancy.by_id[late.account_id] = late
    with tenant_scope(late.tenant_id):
        assert t.accept(late, inv["id"])["role"] == "viewer"
    # acceptée : elle ne sert plus une seconde fois (ni à un autre rôle)
    assert _raw(dsn, late, "DELETE FROM team_access WHERE account_id = %s", (late.account_id,)) == 1  # quitter
    assert _raw(dsn, late, "DELETE FROM team_members WHERE account_id = %s", (late.account_id,)) == 1
    assert _refused(dsn, late, "INSERT INTO team_access (project_id, tenant_id, account_id, role, added_at) "
                               "VALUES (%s, %s, %s, 'viewer', %s)", (pid, a, late.account_id, now))
    # expirée : refusée en base aussi
    with tenant_scope(a):
        inv2 = t.invite(OWNER, pid, {"email": late.email, "role": "viewer"})
    _raw(dsn, OWNER, "UPDATE team_invites SET expires_at = 1 WHERE invite_id = %s", (inv2["id"],))
    assert _refused(dsn, late, "INSERT INTO team_access (project_id, tenant_id, account_id, role, added_at) "
                               "VALUES (%s, %s, %s, 'viewer', %s)", (pid, a, late.account_id, now))


@requires_pg
def test_erasing_a_member_removes_his_memberships_in_other_tenants(world):
    import psycopg

    from nokker.admin.erase import erase_account
    from nokker.tenancy.context import tenant_scope

    db, pid, t = world["pg"], world["pid"], world["team"]
    gone = Actor("tn_ffffffffffff", "us_erased00001", "gone@example.org")
    with psycopg.connect(db["su"], autocommit=True) as su:
        su.execute("INSERT INTO tenants (id) VALUES (%s)", (gone.tenant_id,))
        su.execute("INSERT INTO accounts (id, tenant_id, email) VALUES (%s, %s, %s)",
                   (gone.account_id, gone.tenant_id, gone.email))
    t.tenancy.by_id[gone.account_id] = gone
    with tenant_scope(OWNER.tenant_id):
        inv = t.invite(OWNER, pid, {"email": gone.email, "role": "member"})
        pending = t.invite(OWNER, pid, {"email": "other@example.org", "role": "member"})
    with tenant_scope(gone.tenant_id):
        t.accept(gone, inv["id"])
    with tenant_scope(OWNER.tenant_id):
        t.invite(OWNER, pid, {"email": "gone2@example.org", "role": "viewer"})
    report = erase_account(db["app"], account_id=gone.account_id, apply=True, confirm=gone.account_id)
    assert report.applied and report.deleted["team_access"] == 1 and report.deleted["team_members"] == 1
    with tenant_scope(OWNER.tenant_id):
        emails = {m["email"] for m in t.members(OWNER, pid)["members"]}
        assert gone.email not in emails and MEM.email in emails
        assert t.get(OWNER, pid)["name"] == "Plateforme"  # le projet du propriétaire reste entier
        assert pending["id"] in {i["id"] for i in t.invites(OWNER, pid)["invitations"]}
