"""Mémoire longue multi-locataire (migration 049, docs/MEMORY-TENANCY.md) en base : vrai Postgres pgvector jetable,
rôles de deploy/postgres/roles.sql, service connecté en `nokker_app` (ni propriétaire, ni BYPASSRLS). Ignoré sans
`NOKKER_TEST_PG_DSN`.

Comptes : A et B (testeurs seuls) ; le payeur P avec un membre adulte M et un profil `teen` K, chacun dans SON
locataire ; O n'est membre de rien. Prouve, en SQL brut quand le code se tromperait :
  - la mémoire de profil ne traverse ni les comptes ni les locataires, même par similarité vectorielle d'un texte
    IDENTIQUE, même en posant l'identifiant de compte d'un autre ;
  - la mémoire du foyer est lue par chaque membre actif, plus après le départ, jamais par un non-membre ; une note
    « adultes » n'est jamais lue par un profil mineur ;
  - un membre propose (quarantaine 046, au nom du payeur), ne relit pas sa proposition, n'écrit jamais directement ;
    le payeur approuve par la quarantaine existante ;
  - l'effacement d'un compte retire sa mémoire, ses contributions au foyer et ses propositions ; la suppression du
    foyer retire ses notes communes.
Note : `nokker_long_memory` n'est pas dans CONTENT_TABLES de test_rls.py (ses lignes de test n'y portent pas de
compte) ; `test_every_table_is_covered` la couvre (RLS forcée, `tenant_isolation`, `tenant_id`).
"""
from __future__ import annotations

import pytest

from tests.integration.test_household_rls import Acc, Accounts, _att
from tests.integration.test_rls import pg, pod_env, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

pytestmark = pytest.mark.integration

TA, TB, TP, TM, TK, TO = (f"tn_{c * 12}" for c in "abpmko")
A = Acc(TA, "us_aaaaaaaaaaaa", "a@example.com")
B = Acc(TB, "us_bbbbbbbbbbbb", "b@example.com")
P = Acc(TP, "us_payeur00001", "payeur@example.com")
M = Acc(TM, "us_membre00001", "membre@example.com")
K = Acc(TK, "us_ado00000001", "ado@example.com")
O = Acc(TO, "us_voisin00001", "voisin@example.com")
ALL = (A, B, P, M, K, O)
SECRET = "Mon code de casier au club de tennis est 4471, côté vestiaire nord"


@pytest.fixture(scope="module")
def mem(pg):  # noqa: F811
    import psycopg

    from nokker.household.model import Settings as HSettings
    from nokker.household.service import Household
    from nokker.household.store import PgHouseholdStore
    from nokker.memory.tenant_long import (
        HashEmbedder,
        LongMemory,
        PgLongMemoryStore,
        household_membership,
    )
    from nokker.onboarding.consents import PgConsents
    from nokker.store.pg import apply_migrations
    from nokker.team.service import Team
    from nokker.tenancy.context import tenant_scope

    with psycopg.connect(pg["su"], autocommit=True) as su:
        had_vector = su.execute("SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = "
                                "'nokker_long_memory'::regclass AND attname = 'embedding')").fetchone()[0]
        for a in ALL:
            su.execute("INSERT INTO tenants (id) VALUES (%s) ON CONFLICT DO NOTHING", (a.tenant_id,))
            su.execute("INSERT INTO accounts (id, tenant_id, email) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                       (a.account_id, a.tenant_id, a.email))
        # l'administrateur de la base pose pgvector ; la migration rejouée ajoute la colonne (rien d'autre ne change)
        su.execute("CREATE EXTENSION IF NOT EXISTS vector")
    assert apply_migrations(pg["owner"], strict=True)
    accounts = Accounts(*ALL)
    hh = Household(team=Team(store=PgHouseholdStore(pg["app"], migrate_now=False), tenancy=accounts),
                   settings=HSettings(minors=True), consents=PgConsents(pg["app"]))
    with tenant_scope(TP):
        hid = hh.create(P, {"name": "Maison"})["id"]
        iv_m = hh.invite(P, {"email": M.email})
        iv_k = hh.invite(P, {"email": K.email, "profile": "teen", "attestation": _att()})
    with tenant_scope(TM):
        hh.accept(M, iv_m["id"])
    with tenant_scope(TK):
        hh.accept(K, iv_k["id"])
    store = PgLongMemoryStore(pg["app"], migrate_now=False)
    svc = LongMemory(store=store, membership=household_membership(hh), embedder=HashEmbedder(),
                     account_for_tenant=accounts.account_for_tenant, review=True)
    return {"svc": svc, "store": store, "hh": hh, "hid": hid, "pg": pg, "had_vector": had_vector}


def _raw(dsn, actor, sql, params=(), *, tenant=None, account=None):
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant or actor.tenant_id,))
        conn.execute("SELECT set_config('app.account_id', %s, false)", (actor.account_id if account is None else account,))
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if cur.description else cur.rowcount
        conn.commit()
        return rows


def _in(actor, fn, *args, **kw):
    from nokker.tenancy.context import tenant_scope

    with tenant_scope(actor.tenant_id):
        return fn(actor.account_id, *args, **kw)


@requires_pg
def test_vector_column_appears_once_pgvector_is_installed(mem):
    assert mem["store"].vector is True
    with __import__("psycopg").connect(mem["pg"]["su"]) as su:
        typ = su.execute("SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = "
                         "'nokker_long_memory'::regclass AND attname = 'embedding'").fetchone()[0]
    assert typ == "vector"


@requires_pg
def test_profile_memory_never_crosses_accounts_even_by_vector_similarity(mem):
    svc, dsn = mem["svc"], mem["pg"]["app"]
    a = _in(A, svc.write, scope="user", kind="fact", title="Casier tennis", text=SECRET)["note"]
    b = _in(B, svc.write, scope="user", kind="fact", title="Casier tennis", text=SECRET)["note"]
    got_a = _in(A, svc.recall, "quel est mon code de casier au tennis ?")
    assert [r.note for r in got_a] == [a["name"]]
    got_b = _in(B, svc.recall, SECRET)
    assert [r.note for r in got_b] == [b["name"]] and got_b[0].score >= 0.9  # le texte identique : SA note seulement
    assert all("foyer:" not in r.note for r in got_a + got_b)
    assert _in(O, svc.recall, SECRET) == []
    # SQL brut, plus proche voisin du plongement de A : B ne voit que sa propre ligne
    vec = _raw(dsn, A, "SELECT embedding::text FROM nokker_long_memory WHERE name = %s", (a["name"],))[0][0]
    rows = _raw(dsn, B, "SELECT tenant_id, account_id FROM nokker_long_memory ORDER BY embedding <=> %s::vector",
                (vec,))
    assert rows == [(TB, B.account_id)]
    # l'identifiant de compte de A posé sans son locataire, ou sans compte du tout : rien
    assert _raw(dsn, B, "SELECT count(*) FROM nokker_long_memory WHERE tenant_id = %s", (TA,),
                account=A.account_id)[0][0] == 0
    assert _raw(dsn, A, "SELECT count(*) FROM nokker_long_memory", account="")[0][0] == 0
    # ni écriture au nom d'un autre, ni déplacement
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _raw(dsn, B, "INSERT INTO nokker_long_memory (tenant_id, account_id, name, title, body, author_account, "
                     "created_at, updated_at) VALUES (%s, %s, 'x', 't', 'b', %s, 0, 0)", (TA, A.account_id, A.account_id))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _raw(dsn, B, "UPDATE nokker_long_memory SET account_id = %s", (A.account_id,))
    assert _raw(dsn, B, "DELETE FROM nokker_long_memory WHERE tenant_id = %s", (TA,)) == 0


@requires_pg
def test_household_memory_is_common_and_adults_only_hides_from_minors(mem):
    svc, dsn, hid = mem["svc"], mem["pg"]["app"], mem["hid"]
    _in(P, svc.write, scope="household", kind="fact", title="Dentiste", text="Le dentiste des enfants est le mardi 16h")
    _in(P, svc.write, scope="household", kind="fact", title="Cadeau", text="Le cadeau surprise est caché au garage",
        audience="adults")
    for who in (P, M, K):
        got = _in(who, svc.recall, "quand est le dentiste des enfants ?")
        assert [r.text for r in got] == ["[Mémoire du foyer] Dentiste — Le dentiste des enfants est le mardi 16h"], who
    assert _in(O, svc.recall, "quand est le dentiste des enfants ?") == []
    assert _in(M, svc.recall, "où est caché le cadeau surprise ?")
    assert not [r for r in _in(K, svc.recall, "où est caché le cadeau surprise ?") if "Cadeau" in r.text]
    # SQL brut : le profil mineur ne lit aucune note « adultes », le voisin rien du tout
    assert {r[0] for r in _raw(dsn, K, "SELECT audience FROM nokker_long_memory WHERE household_id = %s", (hid,))} \
        == {"all"}
    assert _raw(dsn, O, "SELECT count(*) FROM nokker_long_memory WHERE tenant_id = %s", (TP,))[0][0] == 0
    # un membre n'écrit jamais directement dans le foyer, ni ne change l'audience
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _raw(dsn, M, "INSERT INTO nokker_long_memory (tenant_id, account_id, name, scope, household_id, title, body, "
                     "author_account, created_at, updated_at) VALUES (%s, %s, 'y', 'household', %s, 't', 'b', %s, 0, 0)",
             (TP, P.account_id, hid, M.account_id))
    assert _raw(dsn, M, "UPDATE nokker_long_memory SET audience = 'all' WHERE tenant_id = %s", (TP,)) == 0


@requires_pg
def test_member_proposal_goes_through_the_payer_quarantine(mem):
    from nokker.learning.pending import PendingMemory
    from nokker.learning.pg import PgLearningStore
    from nokker.tenancy.context import tenant_scope

    svc, dsn, hid = mem["svc"], mem["pg"]["app"], mem["hid"]
    out = _in(M, svc.write, scope="household", kind="rule", title="Courses", text="Les courses se font le samedi matin")
    pid = out["pending"]["id"]
    assert _raw(dsn, M, "SELECT count(*) FROM nokker_memory_pending WHERE id = %s", (pid,))[0][0] == 0  # pas relue
    assert _in(P, svc.recall, "quand se font les courses ?") == []
    # un membre ne propose pas au nom d'un autre auteur, ni chez un autre payeur
    import psycopg

    for account_id, author in ((M.account_id, M.account_id), (P.account_id, K.account_id)):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _raw(dsn, M, "INSERT INTO nokker_memory_pending (tenant_id, account_id, id, source, kind, title, body, "
                         "scope, project, at, author_account) VALUES (%s, %s, 'mp_x', 'household', 'fact', 't', 'b', "
                         "'household', %s, 'now', %s)", (TP, account_id, hid, author))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # le voisin n'est pas membre
        _raw(dsn, O, "INSERT INTO nokker_memory_pending (tenant_id, account_id, id, source, kind, title, body, scope, "
                     "project, at, author_account) VALUES (%s, %s, 'mp_y', 'household', 'fact', 't', 'b', 'household', "
                     "%s, 'now', %s)", (TP, P.account_id, hid, O.account_id))
    # le payeur la voit dans SA quarantaine (route existante) et l'approuve
    store = PgLearningStore(dsn)
    pending = PendingMemory(store=store, account_of_thread=lambda _t: P.account_id,
                            commit_fact=lambda note: svc.commit_pending(note))
    with tenant_scope(TP):
        assert [n.id for n in pending.list(P.account_id)] == [pid]
        done = pending.decide(P.account_id, pid, approve=True, by=P.account_id)
    assert done.status == "approved" and done.result
    got = _in(K, svc.recall, "quand se font les courses ?")
    assert got and got[0].note == "foyer:" + done.result
    assert _raw(dsn, P, "SELECT author_account FROM nokker_long_memory WHERE name = %s", (done.result,))[0][0] \
        == M.account_id


@requires_pg
def test_departure_closes_the_household_memory(mem):
    from nokker.tenancy.context import tenant_scope

    svc, dsn, hh = mem["svc"], mem["pg"]["app"], mem["hh"]
    assert _in(M, svc.recall, "quand est le dentiste des enfants ?")
    with tenant_scope(TM):
        hh.remove(M, M.account_id)  # un adulte quitte seul
    assert _in(M, svc.recall, "quand est le dentiste des enfants ?") == []
    assert _raw(dsn, M, "SELECT count(*) FROM nokker_long_memory WHERE tenant_id = %s", (TP,))[0][0] == 0
    # sa contribution (la note « Courses ») reste au foyer : elle a été relue et acceptée par le payeur
    assert _in(K, svc.recall, "quand se font les courses ?")


@requires_pg
def test_erase_propagates_to_contributions_and_proposals(mem):
    import psycopg

    from nokker.admin.erase import erase_account

    svc, app, su = mem["svc"], mem["pg"]["app"], mem["pg"]["su"]

    def count(sql, params=()):
        with psycopg.connect(su) as conn:
            return conn.execute(sql, params).fetchone()[0]

    # K (encore membre) propose ; K a aussi une note de profil et une contribution acceptée
    _in(K, svc.write, scope="household", kind="fact", title="Piano", text="Le cours de piano est le jeudi")
    _in(K, svc.write, scope="user", kind="fact", title="Perso", text="J'aime les mangas")
    from nokker.tenancy.context import tenant_scope

    with tenant_scope(TP):
        svc._put(svc.actor(P.account_id), "household", svc._member(P.account_id), "fact", "Bus", "Le bus part à 7h40",
                 "all", K.account_id, "pending:test")
    assert count("SELECT count(*) FROM nokker_long_memory WHERE author_account = %s", (K.account_id,)) == 2
    assert count("SELECT count(*) FROM nokker_memory_pending WHERE author_account = %s", (K.account_id,)) == 1
    r = erase_account(app, account_id=K.account_id, apply=True, confirm=K.account_id)
    assert r.applied and not r.unscoped
    assert count("SELECT count(*) FROM nokker_long_memory WHERE author_account = %s", (K.account_id,)) == 0
    assert count("SELECT count(*) FROM nokker_memory_pending WHERE author_account = %s", (K.account_id,)) == 0
    assert count("SELECT count(*) FROM nokker_long_memory WHERE tenant_id = %s", (TP,)) >= 2  # le reste du foyer
    # A effacé : sa mémoire de profil part, celle de B reste
    erase_account(app, account_id=A.account_id, apply=True, confirm=A.account_id)
    assert count("SELECT count(*) FROM nokker_long_memory WHERE tenant_id = %s", (TA,)) == 0
    assert count("SELECT count(*) FROM nokker_long_memory WHERE tenant_id = %s", (TB,)) == 1


@requires_pg
def test_household_deleted_drops_its_common_memory(mem):
    import psycopg

    from nokker.tenancy.context import tenant_scope

    with tenant_scope(TP):
        mem["hh"].delete(P)
    with psycopg.connect(mem["pg"]["su"]) as conn:
        assert conn.execute("SELECT count(*) FROM nokker_long_memory WHERE household_id = %s",
                            (mem["hid"],)).fetchone()[0] == 0
