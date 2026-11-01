"""Compte posé d'un AUTRE locataire (défense en profondeur des politiques d'équipe et de foyer) : vrai Postgres
jetable, rôle `nokker_app`. Ignoré sans `NOKKER_TEST_PG_DSN`.

Scénario : une connexion du noyau dans le locataire de O (non membre) pose `app.account_id` (et `app.account_email`)
d'un membre du foyer de P. Le noyau ne le fait jamais exprès (l'acteur vient de la session), mais un acteur mal passé
ne doit rien ouvrir : chaque politique qui lit `app.account_id` exige désormais que ce compte appartienne au locataire
posé (`nokker_actor()`, migration 050). Tables attaquées : mémoire longue du foyer (049), notes et adhésions d'équipe
(023), adhésions du foyer (039), lignes de dépense du foyer (039), invitations (023, par l'adresse)."""
from __future__ import annotations

import pytest

from tests.integration.test_household_rls import Acc, Accounts, _att
from tests.integration.test_rls import pg, pod_env, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

pytestmark = pytest.mark.integration

TP, TM, TK, TO = (f"tn_{c * 12}" for c in "pmko")
P = Acc(TP, "us_payeur00001", "payeur@example.com")
M = Acc(TM, "us_membre00001", "membre@example.com")
K = Acc(TK, "us_ado00000001", "ado@example.com")
O = Acc(TO, "us_voisin00001", "voisin@example.com")
ALL = (P, M, K, O)
SECRET = "CANARI-FOYER-" + "7719 code du casier"  # construit en deux morceaux : valeur factice, hors des règles gitleaks


@pytest.fixture(scope="module")
def fam(pg):  # noqa: F811
    import psycopg

    from nokker.household.model import Settings
    from nokker.household.service import Household
    from nokker.household.store import PgHouseholdStore
    from nokker.team.service import Team
    from nokker.tenancy.context import tenant_scope

    with psycopg.connect(pg["su"], autocommit=True) as su:
        for a in ALL:
            su.execute("INSERT INTO tenants (id) VALUES (%s) ON CONFLICT DO NOTHING", (a.tenant_id,))
            su.execute("INSERT INTO accounts (id, tenant_id, email) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                       (a.account_id, a.tenant_id, a.email))
    hh = Household(team=Team(store=PgHouseholdStore(pg["app"], migrate_now=False), tenancy=Accounts(*ALL)),
                   settings=Settings(minors=True))
    with tenant_scope(TP):
        hid = hh.create(P, {"name": "Maison"})["id"]
        iv_m = hh.invite(P, {"email": M.email})
        hh.invite(P, {"email": K.email, "profile": "teen", "attestation": _att()})  # reste en attente
    with tenant_scope(TM):
        hh.accept(M, iv_m["id"])
    with psycopg.connect(pg["su"], autocommit=True) as su:  # contenu du foyer, écrit tel que le noyau l'écrirait
        su.execute("INSERT INTO nokker_long_memory (tenant_id, account_id, name, scope, household_id, title, body, "
                   "author_account, created_at, updated_at) VALUES (%s, %s, 'casier', 'household', %s, 'casier', %s, "
                   "%s, 0, 0)", (TP, P.account_id, hid, SECRET, P.account_id))
        su.execute("INSERT INTO team_notes (project_id, tenant_id, name, title, text, updated_at) "
                   "VALUES (%s, %s, 'casier', 'casier', %s, 0)", (hid, TP, SECRET))
        su.execute("INSERT INTO ledger_entries (tenant_id, thread_id, turn_index, tier, model, household_id, "
                   "household_tenant, account) VALUES (%s, 'tn_mmmmmmmmmmmm:th', 0, 'ship', 'CANARI-MODELE', %s, %s, %s)",
                   (TM, hid, TP, M.account_id))
    return {"hh": hh, "hid": hid, "pg": pg}


def _raw(dsn, tenant, account, email, sql, params=()):
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false), set_config('app.account_id', %s, false), "
                     "set_config('app.account_email', %s, false)", (tenant, account, email))
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if cur.description else cur.rowcount
        conn.commit()
        return rows


READS = {
    "nokker_long_memory": "SELECT body FROM nokker_long_memory WHERE scope = 'household'",
    "team_notes": "SELECT text FROM team_notes",
    "team_projects": "SELECT name FROM team_projects",
    "team_access": "SELECT account_id FROM team_access",
    "team_members": "SELECT account_id FROM team_members",
    "team_invites": "SELECT email FROM team_invites",
    "household_members": "SELECT account_id FROM household_members",
    "ledger_entries": "SELECT model FROM ledger_entries WHERE household_id IS NOT NULL",
}


@requires_pg
def test_legit_member_still_reads_the_household(fam):
    dsn = fam["pg"]["app"]
    assert _raw(dsn, TM, M.account_id, M.email, READS["nokker_long_memory"]) == [(SECRET,)]
    assert _raw(dsn, TM, M.account_id, M.email, READS["team_notes"]) == [(SECRET,)]
    assert len(_raw(dsn, TM, M.account_id, M.email, READS["household_members"])) == 2
    assert _raw(dsn, TM, M.account_id, M.email, READS["ledger_entries"]) == [("CANARI-MODELE",)]
    assert _raw(dsn, TK, K.account_id, K.email, READS["team_invites"])  # K lit l'invitation qui lui est adressée


@requires_pg
@pytest.mark.parametrize("table", sorted(READS))
@pytest.mark.parametrize("victim", ["P", "M", "K"])
def test_an_account_of_another_tenant_opens_nothing(fam, table, victim):
    dsn = fam["pg"]["app"]
    v = {"P": P, "M": M, "K": K}[victim]
    assert _raw(dsn, TO, v.account_id, v.email, READS[table]) == []


@requires_pg
def test_an_account_of_another_tenant_cannot_join_or_write(fam):
    import psycopg

    dsn, hid = fam["pg"]["app"], fam["hid"]
    # K a une invitation en attente : O, en posant le compte et l'adresse de K, n'entre pas à sa place
    rls = psycopg.errors.InsufficientPrivilege  # « new row violates row-level security policy »
    with pytest.raises(rls):
        _raw(dsn, TO, K.account_id, K.email,
             "INSERT INTO team_access (project_id, tenant_id, account_id, role, added_at) "
             "VALUES (%s, %s, %s, 'member', 0)", (hid, TP, K.account_id))
    with pytest.raises(rls):
        _raw(dsn, TO, M.account_id, M.email,
             "INSERT INTO nokker_memory_pending (tenant_id, account_id, id, source, kind, title, body, scope, at, "
             "status, author_account) VALUES (%s, %s, 'mp_x', 'household', 'fact', 't', 'b', 'household', 'now', "
             "'pending', %s)", (TP, P.account_id, M.account_id))
