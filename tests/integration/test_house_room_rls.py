"""Salle Maison (migration 057, docs/HOUSE-ROOM.md §4) en base : vrai Postgres jetable, rôles de
deploy/postgres/roles.sql, service connecté en `nokker_app` (ni propriétaire, ni BYPASSRLS). Ignoré sans
`NOKKER_TEST_PG_DSN`.

Le payeur P a un foyer avec un adulte M et un ado K, chacun dans SON locataire ; O n'est membre de rien ; Q a son
propre foyer. Prouve, en SQL brut quand le code se tromperait :
  - chaque membre actif lit la salle de son foyer, l'ado jamais un message `adults` ; O et Q rien, même en posant le
    compte d'un membre (le locataire, lui, est posé par le noyau depuis l'identité authentifiée : docs/TENANCY.md §2) ;
  - un membre écrit à SON nom seulement (ni au nom d'un autre, ni un événement, ni dans un autre foyer) ;
  - le journal est append-only (même pour le payeur) ; un départ ferme la salle au message suivant ;
  - l'effacement d'un membre retire ses messages ; la suppression du foyer retire sa salle.
"""
from __future__ import annotations

import pytest

from tests.integration.test_household_rls import Acc, Accounts, _att
from tests.integration.test_rls import pg, pod_env, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

pytestmark = pytest.mark.integration

TP, TM, TK, TO, TQ = (f"tn_{c * 12}" for c in "pmkoq")
P = Acc(TP, "us_payeur00001", "payeur@example.com")
M = Acc(TM, "us_membre00001", "membre@example.com")
K = Acc(TK, "us_ado00000001", "ado@example.com")
O = Acc(TO, "us_voisin00001", "voisin@example.com")
Q = Acc(TQ, "us_autre000001", "autre@example.com")
ALL = (P, M, K, O, Q)


@pytest.fixture(scope="module")
def room(pg):  # noqa: F811
    import psycopg

    from nokker.house.store import PgRoomStore, RoomMessage
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
        iv_k = hh.invite(P, {"email": K.email, "profile": "teen", "attestation": _att()})
    with tenant_scope(TM):
        hh.accept(M, iv_m["id"])
    with tenant_scope(TK):
        hh.accept(K, iv_k["id"])
    with tenant_scope(TQ):
        qid = hh.create(Q, {"name": "Ailleurs"})["id"]
    store = PgRoomStore(pg["app"], migrate_now=False)

    def msg(kind, text, author=None, audience="all", agent=None):
        return RoomMessage(id=0, household_id=hid, tenant_id=TP, kind=kind, text=text, at=0.0,
                           author_account=author.account_id if author else None, agent=agent, audience=audience)

    with tenant_scope(TM):
        store.append(msg("member", "CANARI-SALLE bonjour", M), actor=M.account_id)
        store.append(msg("agent", "CANARI-ADULTES facture", M, audience="adults", agent="courrier"),
                     actor=M.account_id)
    with tenant_scope(TK):
        store.append(msg("member", "salut de l'ado", K), actor=K.account_id)
    with tenant_scope(TP):
        store.append(msg("event", "CANARI-EVENT caméra", agent="veille"))
    return {"hh": hh, "hid": hid, "qid": qid, "pg": pg, "store": store, "msg": msg}


def _raw(dsn, actor, sql, params=(), *, tenant=None, account=None):
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant or actor.tenant_id,))
        conn.execute("SELECT set_config('app.account_id', %s, false)", (account or actor.account_id,))
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if cur.description else cur.rowcount
        conn.commit()
        return rows


def _texts(dsn, actor, **kw):
    return {r[0] for r in _raw(dsn, actor, "SELECT text FROM house_room_messages", **kw)}


@requires_pg
def test_members_read_their_room_teen_never_adults_outsiders_nothing(room):
    dsn = room["pg"]["app"]
    assert _texts(dsn, P) == _texts(dsn, M) == {"CANARI-SALLE bonjour", "CANARI-ADULTES facture",
                                               "salut de l'ado", "CANARI-EVENT caméra"}
    assert _texts(dsn, K) == {"CANARI-SALLE bonjour", "salut de l'ado", "CANARI-EVENT caméra"}
    for actor in (O, Q):
        assert _texts(dsn, actor) == set()
    assert _texts(dsn, O, account=M.account_id) == set()  # poser le compte d'un membre depuis un autre locataire
    # le magasin : même règle (adults filtré en plus côté requête)
    from nokker.tenancy.context import tenant_scope

    with tenant_scope(TK):
        rows = room["store"].list(room["hid"], TP, adults=False, actor=K.account_id)
    assert "CANARI-ADULTES facture" not in {r.text for r in rows}
    with tenant_scope(TO):
        assert room["store"].list(room["hid"], TP, adults=True, actor=O.account_id) == []


@requires_pg
def test_writes_only_in_ones_own_name_and_never_change(room):
    import psycopg

    dsn, hid = room["pg"]["app"], room["hid"]
    ins = ("INSERT INTO house_room_messages (household_id, tenant_id, kind, text, author_account) "
           "VALUES (%s, %s, %s, %s, %s)")
    assert _raw(dsn, M, ins, (hid, TP, "member", "à mon nom", M.account_id)) == 1
    for actor, params, audience in ((M, (hid, TP, "member", "au nom du payeur", P.account_id), "all"),
                                    (O, (hid, TP, "member", "intrus", O.account_id), "all"),
                                    (Q, (hid, TP, "agent", "autre foyer", Q.account_id), "all"),
                                    (M, (room["qid"], TQ, "member", "dans le foyer de Q", M.account_id), "all"),
                                    (K, (hid, TP, "agent", "ado vers adultes", K.account_id), "adults")):
        with pytest.raises(psycopg.Error):
            _raw(dsn, actor, ins.replace("author_account)", "author_account, audience)").replace("%s)", "%s, %s)"),
                 (*params, audience))
    with pytest.raises(psycopg.Error):  # un événement n'a pas d'auteur : seul le locataire du foyer l'écrit
        _raw(dsn, M, "INSERT INTO house_room_messages (household_id, tenant_id, kind, text) VALUES (%s, %s, 'event', 'x')",
             (hid, TP))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # append-only, même pour le payeur
        _raw(dsn, P, "UPDATE house_room_messages SET text = 'réécrit'")
    assert _raw(dsn, M, "DELETE FROM house_room_messages") == 0  # un membre n'efface rien


@requires_pg
def test_leaving_closes_the_room_and_erase_and_household_delete_clean_up(room):
    from nokker.tenancy.context import tenant_scope

    dsn, hh = room["pg"]["app"], room["hh"]
    with tenant_scope(TM):
        hh.remove(M, M.account_id)
    assert _texts(dsn, M) == set()
    # effacement de l'ado (nokker erase pose ces réglages) : ses messages partent, les autres restent
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true), set_config('app.account_id', %s, true), "
                     "set_config('nokker.erasing_tenant', %s, true)", (TK, K.account_id, TK))
        n = conn.execute("DELETE FROM house_room_messages WHERE author_account = %s", (K.account_id,)).rowcount
    assert n == 1
    assert "salut de l'ado" not in _texts(dsn, P)
    with tenant_scope(TP):
        hh.delete(P)
    assert _texts(dsn, P) == set()
