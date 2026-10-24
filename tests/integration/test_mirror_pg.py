"""Miroir web ⇄ Lokkal sur un vrai Postgres sous RLS : la poussée d'un locataire et le tirage d'un autre ne se voient
jamais ; le curseur avance ; un renvoi n'ajoute rien."""
from __future__ import annotations

from nokker.cloud import mirror
from nokker.core.turn import InputCheck, Turn
from nokker.store.pg import PgStore, turn_to_dict
from nokker.tenancy.context import scoped_thread_id, tenant_scope
from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401  (pg : fixture)


def _t(thread, i, text):
    return Turn(thread_id=thread, index=i, input=InputCheck(text=text), channel="api", reply=text)


@requires_pg
def test_mirror_is_isolated_by_tenant(pg):  # noqa: F811
    store = PgStore(pg["app"])
    item = {"thread_id": "th_l", "turn": turn_to_dict(_t("th_l", 0, "machine de A"))}
    assert mirror.push(store, A, "mc_a", [item]) == {"accepted": 1, "skipped": 0}
    assert mirror.push(store, A, "mc_a", [item]) == {"accepted": 0, "skipped": 1}
    with tenant_scope(A):
        store.append(_t(scoped_thread_id(A, "web-a"), 0, "web de A"))
    with tenant_scope(B):
        store.append(_t(scoped_thread_id(B, "web-b"), 0, "web de B"))
    got_a = mirror.pull(store, A, None)
    assert [x["turn"]["reply"] for x in got_a["turns"]] == ["web de A"]  # ni la copie lokkal-*, ni le fil de B
    assert mirror.pull(store, A, got_a["cursor"])["turns"] == []
    assert [x["turn"]["reply"] for x in mirror.pull(store, B, None)["turns"]] == ["web de B"]
