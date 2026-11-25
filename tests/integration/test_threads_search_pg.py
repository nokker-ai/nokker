"""`PgStore.search_turns` (`GET /v1/threads/search`, docs/LOKKAL-PHONE.md §1.2 point 4) sur un vrai Postgres sous RLS,
connexion `nokker_app` : seulement les fils donnés et les copies du miroir du préfixe, sans casse, `%` et `_` pris à la
lettre, jamais un tour d'un autre locataire (même en nommant son fil)."""
from __future__ import annotations

from nokker.core.turn import InputCheck, Turn
from nokker.store.pg import PgStore
from nokker.tenancy.context import scoped_thread_id, tenant_scope
from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401  (pg : fixture)


def _t(thread, i, text, reply="ok"):
    return Turn(thread_id=thread, index=i, input=InputCheck(text=text), channel="api", reply=reply)


@requires_pg
def test_pg_search_turns_scoped_literal_and_tenant_isolated(pg):  # noqa: F811
    store = PgStore(pg["app"])
    a1, a2 = scoped_thread_id(A, "s-a1"), scoped_thread_id(A, "s-a2")
    copy = scoped_thread_id(A, "lokkal-mc_0123456789abcdef-s1")
    b1 = scoped_thread_id(B, "s-b1")
    with tenant_scope(A):
        store.append(_t(a1, 0, "Le SITE de la maison"))
        store.append(_t(a1, 1, "rien", reply="réponse sur le site"))
        store.append(_t(a2, 0, "100% du site_web"))
        store.append(_t(copy, 0, "le site sur atelier"))
        store.append(_t(a2, 1, "rendez-vous au Café de Léa", reply="noté"))
    with tenant_scope(B):
        store.append(_t(b1, 0, "le site de B"))
    with tenant_scope(A):
        got = store.search_turns([a1], "site", 10)
        assert [(r[0], r[1]) for r in got] == [(a1, 1), (a1, 0)] and got[0][2] is not None  # récent d'abord, daté
        assert {r[0] for r in store.search_turns([a1, a2], "site", 10)} == {a1, a2}
        assert [r[0] for r in store.search_turns([a1, a2], "100%", 10)] == [a2]
        assert store.search_turns([a1, a2], "s_t", 10) == []  # `_` à la lettre, pas « un caractère » (« sit »)
        assert [r[0] for r in store.search_turns([a1, a2], "site_web", 10)] == [a2]
        assert store.search_turns([a1, a2, b1], "de B", 10) == []  # le fil de B nommé : la RLS le cache
        assert [r[0] for r in store.search_turns([], "atelier", 10, prefix=scoped_thread_id(A, "lokkal-"))] == [copy]
        assert len(store.search_turns([a1, a2], "site", 1)) == 1
        assert [(r[0], r[1]) for r in store.search_turns([a1, a2], "CAFE DE LEA", 10)] == [(a2, 1)]  # accents
        assert [(r[0], r[1]) for r in store.search_turns([a1, a2], "café de léa", 10)] == [(a2, 1)]
        assert store.search_turns([], "site", 10) == []
    with tenant_scope(B):
        assert store.search_turns([], "site", 10, prefix=scoped_thread_id(A, "lokkal-")) == []
        assert [r[0] for r in store.search_turns([b1, a1], "site", 10)] == [b1]
