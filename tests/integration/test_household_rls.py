"""Foyer (lot X23) en base : vrai Postgres jetable, rôles de deploy/postgres/roles.sql, service connecté en
`nokker_app` (ni propriétaire, ni BYPASSRLS). Ignoré sans `NOKKER_TEST_PG_DSN`.

Le payeur P (locataire P) a un foyer avec un membre adulte M et un profil `teen` K, chacun dans SON locataire ; O n'est
membre de rien ; Q a son propre foyer. Prouve, en SQL brut quand le code se tromperait :
  - personne ne lit les fils, connecteurs, consentements ni la mémoire d'un autre membre ;
  - un foyer par compte, 6 membres au plus, le profil de l'invitation (pas celui que l'invité choisirait) ;
  - un compte mineur ne peut pas accorder de consentement d'entraînement (trigger) ;
  - le quota se lit dans le ledger : P lit les lignes du FOYER venues de M, rien d'autre ; O n'attribue rien au foyer ;
  - `nokker erase` d'un membre, d'un mineur, du payeur.
"""
from __future__ import annotations

import pytest

from tests.integration.test_rls import pg, pod_env, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

pytestmark = pytest.mark.integration

TP, TM, TK, TO, TQ, TR = (f"tn_{c * 12}" for c in "pmkoqr")


class Acc:
    def __init__(self, tenant_id: str, account_id: str, email: str) -> None:
        self.tenant_id, self.account_id, self.email, self.status = tenant_id, account_id, email, "active"


P = Acc(TP, "us_payeur00001", "payeur@example.com")
M = Acc(TM, "us_membre00001", "membre@example.com")
K = Acc(TK, "us_ado00000001", "ado@example.com")
O = Acc(TO, "us_voisin00001", "voisin@example.com")
Q = Acc(TQ, "us_autre000001", "autre@example.com")
R = Acc(TR, "us_enfant00001", "enfant@example.com")
ALL = (P, M, K, O, Q, R)


class Accounts:
    def __init__(self, *accs: Acc) -> None:
        self.by_id = {a.account_id: a for a in accs}

    def account(self, account_id):
        return self.by_id.get(account_id)

    def account_by_email(self, email):
        return next((a for a in self.by_id.values() if a.email == email), None)

    def account_for_tenant(self, tenant_id):
        return next((a for a in self.by_id.values() if a.tenant_id == tenant_id), None)


def _att():
    from nokker.household.model import consent_text

    return {"version": consent_text("fr").sha256, "lang": "fr", "legal_guardian": True}


@pytest.fixture(scope="module")
def fam(pg):  # noqa: F811
    import psycopg

    from nokker.household.model import Settings
    from nokker.household.service import Household
    from nokker.household.store import PgHouseholdStore
    from nokker.onboarding.consents import PgConsents
    from nokker.team.service import Team
    from nokker.tenancy.context import tenant_scope

    with psycopg.connect(pg["su"], autocommit=True) as su:  # comptes réels : `nokker erase` les cherche
        for a in ALL:
            su.execute("INSERT INTO tenants (id) VALUES (%s) ON CONFLICT DO NOTHING", (a.tenant_id,))
            su.execute("INSERT INTO accounts (id, tenant_id, email) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                       (a.account_id, a.tenant_id, a.email))
    store = PgHouseholdStore(pg["app"], migrate_now=False)
    hh = Household(team=Team(store=store, tenancy=Accounts(*ALL)), settings=Settings(minors=True),
                   consents=PgConsents(pg["app"]))
    with tenant_scope(TP):
        hid = hh.create(P, {"name": "Maison"})["id"]
        iv_m = hh.invite(P, {"email": M.email})
        iv_k = hh.invite(P, {"email": K.email, "profile": "teen", "attestation": _att()})
    with tenant_scope(TM):
        hh.accept(M, iv_m["id"])
    with tenant_scope(TK):
        hh.accept(K, iv_k["id"])
    with tenant_scope(TQ):
        qid = hh.create(Q, {"name": "Ailleurs"})["id"]
    return {"hh": hh, "hid": hid, "qid": qid, "pg": pg}


def _raw(dsn, actor, sql, params=(), *, account=True):
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


@requires_pg
def test_members_see_the_household_rows_but_nothing_else_of_each_other(fam):
    dsn = fam["pg"]["app"]
    # M voit les adhésions du foyer (profil, état, plafond : aucun contenu), jamais les attestations
    assert {r[0] for r in _raw(dsn, M, "SELECT account_id FROM household_members")} == \
        {P.account_id, M.account_id, K.account_id}
    assert _raw(dsn, M, "SELECT * FROM parental_consents") == []
    assert len(_raw(dsn, P, "SELECT * FROM parental_consents")) == 1
    # sans compte posé, ou hors du foyer : rien
    assert _raw(dsn, M, "SELECT * FROM household_members", account=False) == []
    assert {r[0] for r in _raw(dsn, O, "SELECT account_id FROM household_members")} == set()
    # chacun garde son locataire : un fil, un consentement, un connecteur de M ne sont jamais lisibles par P
    import psycopg

    with psycopg.connect(fam["pg"]["su"], autocommit=True) as su:
        su.execute("INSERT INTO threads (thread_id, tenant_id) VALUES ('tn_mmmmmmmmmmmm:th-prive', %s)", (TM,))
        su.execute("INSERT INTO data_consents (tenant_id, account_id, scope, granted, source) "
                   "VALUES (%s, %s, 'training', false, 'signup')", (TM, M.account_id))
    for table in ("threads", "data_consents"):
        assert _raw(dsn, P, f"SELECT * FROM {table} WHERE tenant_id = %s", (TM,)) == [], table
        assert _raw(dsn, K, f"SELECT * FROM {table} WHERE tenant_id = %s", (TM,)) == [], table
    # une ligne ne change ni de foyer ni de locataire
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _raw(dsn, P, "UPDATE household_members SET household_id = 'pj_000000000000' WHERE account_id = %s",
             (M.account_id,))


@requires_pg
def test_one_household_per_account_and_invitation_profile_enforced_in_db(fam):
    import psycopg

    from nokker.team.model import TeamError
    from nokker.tenancy.context import tenant_scope

    hh, dsn = fam["hh"], fam["pg"]["app"]
    with tenant_scope(TQ):
        iv = hh.invite(Q, {"email": M.email})
    with tenant_scope(TM), pytest.raises(TeamError) as e:
        hh.accept(M, iv["id"])
    assert (e.value.status, e.value.code) == (409, "already_in_household")
    # SQL brut : M ne glisse pas une seconde adhésion, même avec une invitation en attente
    with pytest.raises((psycopg.errors.UniqueViolation, psycopg.errors.InsufficientPrivilege)):
        _raw(dsn, M, "INSERT INTO household_members (household_id, tenant_id, account_id, profile, added_at) "
                     "VALUES (%s, %s, %s, 'adult', 1)", (fam["qid"], TQ, M.account_id))
    # R est invité comme `child` : il ne peut pas entrer en adulte ni en payeur, même en SQL brut
    with tenant_scope(TQ):
        iv_r = hh.invite(Q, {"email": R.email, "profile": "child", "attestation": _att()})
    for profile, payer in (("adult", False), ("child", True)):
        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT set_config('app.tenant_id', %s, false), set_config('app.account_id', %s, false), "
                         "set_config('app.account_email', %s, false)", (TR, R.account_id, R.email))
            conn.execute("INSERT INTO team_access (project_id, tenant_id, account_id, role, added_at) "
                         "VALUES (%s, %s, %s, 'member', 1)", (fam["qid"], TQ, R.account_id))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("INSERT INTO household_members (household_id, tenant_id, account_id, profile, payer, "
                             "added_at) VALUES (%s, %s, %s, %s, %s, 1)", (fam["qid"], TQ, R.account_id, profile, payer))
            conn.rollback()
    with tenant_scope(TR):
        hh.accept(R, iv_r["id"])
    assert hh.store.minor(R.account_id)["profile"] == "child"


@requires_pg
def test_at_most_six_members_in_db_even_past_the_service(fam):
    import psycopg

    dsn = fam["pg"]["app"]
    # Q (payeur, son locataire) écrit en direct : la 7e ligne est refusée par le trigger
    n = len(_raw(dsn, Q, "SELECT 1 FROM household_members WHERE household_id = %s", (fam["qid"],)))
    for i in range(n, 6):
        _raw(dsn, Q, "INSERT INTO household_members (household_id, tenant_id, account_id, added_at) "
                     "VALUES (%s, %s, %s, 1)", (fam["qid"], TQ, f"us_fictif{i:05d}"))
    with pytest.raises(psycopg.errors.CheckViolation):
        _raw(dsn, Q, "INSERT INTO household_members (household_id, tenant_id, account_id, added_at) "
                     "VALUES (%s, %s, 'us_septieme0001', 1)", (fam["qid"], TQ))
    _raw(dsn, Q, "DELETE FROM household_members WHERE account_id LIKE 'us_fictif%%'")


@requires_pg
def test_a_minor_never_grants_a_training_consent_in_db(fam):
    import psycopg

    from nokker.tenancy.context import tenant_scope

    dsn = fam["pg"]["app"]
    with pytest.raises(psycopg.errors.CheckViolation):
        _raw(dsn, K, "INSERT INTO data_consents (account_id, scope, granted, source) "
                     "VALUES (%s, 'training', true, 'account')", (K.account_id,))
    assert _raw(dsn, K, "INSERT INTO data_consents (account_id, scope, granted, source) "
                        "VALUES (%s, 'training', false, 'account')", (K.account_id,)) == 1
    with tenant_scope(TK):
        assert fam["hh"].consents.current(K.account_id, "training") is False


@requires_pg
def test_household_quota_is_read_from_the_ledger_across_tenants_by_members_only(fam, pod_env):  # noqa: F811
    from datetime import UTC, datetime, timedelta

    import psycopg

    from nokker.household.quota import HouseholdQuota
    from nokker.ledger.attribution import SpendTag, spend_tag
    from nokker.ledger.costs import PostgresLedger
    from nokker.ledger.prices import load_from_yaml
    from nokker.tenancy.context import tenant_scope
    from tests.unit.team.test_budget import call

    hh, hid, dsn = fam["hh"], fam["hid"], fam["pg"]["app"]
    ledger = PostgresLedger(dsn, prices=load_from_yaml(), migrate=False)
    with tenant_scope(TM):
        with spend_tag(SpendTag(None, None, M.account_id, household_id=hid, household_tenant=TP)):
            ledger.record("tn_mmmmmmmmmmmm:th1", 0, call(1))
            ledger.record("tn_mmmmmmmmmmmm:th1", 0, call(1))  # même tour
            ledger.record("tn_mmmmmmmmmmmm:th1/sa-1", 0, call(1))  # sous-agent : même tour
        ledger.record("tn_mmmmmmmmmmmm:th-hors", 0, call(1))  # hors foyer : reste chez M
    with tenant_scope(TK), spend_tag(SpendTag(None, None, K.account_id, household_id=hid, household_tenant=TP)):
        ledger.record("tn_kkkkkkkkkkkk:th1", 0, call(1))
    quota = HouseholdQuota(household=hh, ledger=ledger)
    hh.quota = quota
    with tenant_scope(TP):
        v = hh.view(P)
    assert v["quota"]["today"]["used"] == 2
    assert {x["account_id"]: x["turns"]["today"] for x in v["members"]} == {P.account_id: 0, M.account_id: 1,
                                                                           K.account_id: 1}
    since = datetime.now(UTC) - timedelta(days=1)
    with tenant_scope(TO):
        assert ledger.fetch_household_calls(hid, TP, since=since, actor=O.account_id) == []
    seen = {r[0] for r in _raw(dsn, P, "SELECT thread_id FROM ledger_entries")}
    assert "tn_mmmmmmmmmmmm:th1" in seen and "tn_mmmmmmmmmmmm:th-hors" not in seen
    for who, acc in ((O, O.account_id), (M, K.account_id)):  # un non-membre, ou au nom d'un autre
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _raw(dsn, who, "INSERT INTO ledger_entries (thread_id, turn_index, tier, model, household_id, "
                           "household_tenant, account) VALUES ('th_x', 0, 'ship', 'm', %s, %s, %s)", (hid, TP, acc))
    with tenant_scope(TM):
        assert quota.check("tn_mmmmmmmmmmmm:th2", M.account_id).verdict is None


@requires_pg
def test_erase_a_member_a_minor_and_the_payer(fam):
    import psycopg

    from nokker.admin.erase import erase_account

    su, app, hid = fam["pg"]["su"], fam["pg"]["app"], fam["hid"]

    def count(sql, params=()):
        with psycopg.connect(su) as conn:
            return conn.execute(sql, params).fetchone()[0]

    erase_account(app, account_id=M.account_id, apply=True, confirm=M.account_id)
    assert count("SELECT count(*) FROM household_members WHERE account_id = %s", (M.account_id,)) == 0
    assert count("SELECT count(*) FROM team_access WHERE account_id = %s", (M.account_id,)) == 0
    assert count("SELECT count(*) FROM household_members WHERE household_id = %s", (hid,)) == 2
    assert count("SELECT count(*) FROM parental_consents WHERE minor_email = %s", (K.email,)) == 1
    erase_account(app, account_id=K.account_id, apply=True, confirm=K.account_id)
    assert count("SELECT count(*) FROM household_members WHERE account_id = %s", (K.account_id,)) == 0
    assert count("SELECT count(*) FROM parental_consents WHERE minor_email = %s", (K.email,)) == 0
    assert count("SELECT count(*) FROM account_minors WHERE account_id = %s", (K.account_id,)) == 0
    assert count("SELECT count(*) FROM household_members WHERE household_id = %s", (hid,)) == 1  # le payeur reste
    r = erase_account(app, account_id=P.account_id, apply=True, confirm=P.account_id)
    assert r.applied and not r.unscoped
    for t in ("household_members", "parental_consents", "team_projects"):
        assert count(f"SELECT count(*) FROM {t} WHERE tenant_id = %s", (TP,)) == 0, t
