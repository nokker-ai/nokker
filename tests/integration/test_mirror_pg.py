"""Miroir web ⇄ Lokkal sur un vrai Postgres sous RLS : la poussée d'un locataire et le tirage d'un autre ne se voient
jamais ; le curseur avance ; un renvoi n'ajoute rien."""
from __future__ import annotations

import httpx

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


@requires_pg
def test_machine_worker_sets_its_tenant_and_keeps_a_durable_cursor(pg):  # noqa: F811
    """Côté machine, sur Postgres sous RLS : le fil de fond du miroir n'a pas de requête autour de lui. Sans locataire
    posé par lui-même, le magasin refusait tout (NoTenant) ; le curseur d'envoi est durable (état), pas une file."""
    from nokker.local.mirror import MirrorWorker

    store = PgStore(pg["app"])
    with tenant_scope(A):
        store.append(_t("th_l", 0, "tour local"))
    sent: list[dict] = []

    class Http:
        def post(self, url, headers=None, json=None):
            sent.extend(json["turns"])
            return httpx.Response(200, request=httpx.Request("POST", url), json={"accepted": len(json["turns"])})

        def get(self, url, headers=None, params=None):
            body = {"turns": [{"thread_id": "web1", "turn": turn_to_dict(_t("web1", 0, "tour web"))}],
                    "cursor": "c1"}
            return httpx.Response(200, request=httpx.Request("GET", url), json=body)

    state: dict = {}
    w = MirrorWorker(store=store, base_url="http://cloud.invalid", token=lambda: "mt", state=state, save=lambda: None,
                     tenant=A, client=Http())
    n = w.push_once()  # les tours de A (la fixture en a déjà), aucun de B
    assert n == len(sent) and "th_l" in {x["thread_id"] for x in sent} and state["mirror_push_cursor"]
    with tenant_scope(B):
        theirs = {t for _, t, _, _ in store.turns_after(("-infinity", "", -1), 100)}
    assert not theirs & {x["thread_id"] for x in sent}
    assert w.pull_once() == 1
    with tenant_scope(A):
        assert [t.reply for t in store.history("mirror-web-web1")] == ["tour web"]
    assert w.push_once() == 0 and len(sent) == n  # la copie web ne repart pas, rien de neuf après le curseur
    with tenant_scope(B):
        assert store.history("mirror-web-web1") == []  # rangée chez A seulement



@requires_pg
def test_mirror_erase_reaches_copies_only_and_turns_stay_append_only(pg):  # noqa: F811
    """Migration 015 : `nokker_mirror_erase` efface les copies d'une machine chez son locataire, rien d'autre — ni les
    fils maison, ni un autre locataire, ni un préfixe quelconque ; un DELETE ordinaire sur `turns` échoue toujours."""
    import psycopg
    import pytest

    store = PgStore(pg["app"])
    item = {"thread_id": "th_e", "turn": turn_to_dict(_t("th_e", 0, "machine de A"))}
    mirror.push(store, A, "mc_e1", [item])
    mirror.push(store, B, "mc_e2", [{"thread_id": "th_e", "turn": item["turn"]}])
    with tenant_scope(A):
        store.append(_t(scoped_thread_id(A, "web-e"), 0, "web de A (e)"))
    assert mirror.erase(store, A, "mc_e1") == 1
    with tenant_scope(A):
        assert store.history(mirror.mirror_thread(A, "mc_e1", "th_e")) == []
        assert [t.reply for t in store.history(scoped_thread_id(A, "web-e"))] == ["web de A (e)"]
    with tenant_scope(B):
        assert len(store.history(mirror.mirror_thread(B, "mc_e2", "th_e"))) == 1
    for bad in (scoped_thread_id(A, "web-"), scoped_thread_id(A, ""), "lokkal-", scoped_thread_id(B, "lokkal-mc_e2-")):
        with tenant_scope(A), pytest.raises(psycopg.Error):
            store.erase_mirror_copies(bad)
    with psycopg.connect(pg["app"]) as conn, pytest.raises(psycopg.Error):
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (A,))
        conn.execute("DELETE FROM turns WHERE thread_id = %s", (scoped_thread_id(A, "web-e"),))
