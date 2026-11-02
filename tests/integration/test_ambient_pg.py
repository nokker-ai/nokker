"""Écrans e-ink en base (migration 054, docs/AMBIENT.md §3) : vrai Postgres jetable, rôles de
deploy/postgres/roles.sql, store connecté en `nokker_app`. Ignoré sans `NOKKER_TEST_PG_DSN`.

Prouve : le tableau d'un foyer n'existe pas pour un autre locataire (store et SQL brut) ; la table des écrans ne garde
que l'empreinte du jeton ; un jeton révoqué n'ouvre plus rien ; l'appairage TRMNL ne sert qu'une fois ; la migration se
rejoue sans erreur."""
from __future__ import annotations

import datetime as dt

import pytest

from tests.integration.test_rls import A, B, pg, pod_env, requires_pg  # noqa: F401 — fixtures (base neuve, rôles)

pytestmark = pytest.mark.integration


@requires_pg
def test_ambient_displays_and_boards(pg, pod_env):  # noqa: F811
    import psycopg

    from nokker.ambient.displays import Displays, Membership, PgDisplayStore, sha
    from nokker.store.conn import connect
    from nokker.store.pg import apply_migrations
    from nokker.tenancy.context import tenant_scope

    alice = Membership(account_id="us_aaaaaaaaaaa1", household_id="tp_a", household_tenant=A, profile="adult")
    bob = Membership(account_id="us_bbbbbbbbbbb1", household_id="tp_b", household_tenant=B, profile="adult")
    d = Displays(store=PgDisplayStore(pg["app"]), now_local=lambda: dt.datetime(2026, 11, 2, 7, 30))
    ta = d.register(alice, {"model": "trmnl"})["token"]
    tb = d.register(bob, {"model": "waveshare_4in2"})["token"]
    d.put_board(alice, {"brief": "Foyer A", "courses": ["Lait"]})
    assert d.put_board(alice, {"brief": "Foyer A v2"})["board"]["version"] == 2
    assert d.board(bob)["board"]["brief"] == ""
    assert d.board(alice)["board"]["brief"] == "Foyer A v2"
    with tenant_scope(B), connect(pg["app"], autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM ambient_boards").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO ambient_boards (tenant_id, household_id) VALUES (%s, 'tp_a')", (A,))
    with psycopg.connect(pg["app"], autocommit=True) as conn:  # sans locataire : rien (écrans sous RLS depuis 058)
        assert conn.execute("SELECT count(*) FROM ambient_displays").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM ambient_boards").fetchone()[0] == 0
    for tid, tok in ((A, ta), (B, tb)):  # chaque foyer ne voit que son écran
        with tenant_scope(tid), connect(pg["app"], autocommit=True) as conn:
            assert [r[0] for r in conn.execute("SELECT token_sha256 FROM ambient_displays").fetchall()] == [sha(tok)]
    from nokker.store.conn import connect_key

    with connect_key(pg["app"], sha(ta), autocommit=True) as conn:  # le jeton de A n'ouvre que l'écran de A
        assert [r[0] for r in conn.execute("SELECT tenant_id FROM ambient_displays").fetchall()] == [A]
    assert {x.tenant_id for x in d.store.active()} == {A, B}  # publication de fond : locataire par locataire
    assert [x["id"] for x in d.list(bob)["displays"]] == [d.resolve(tb).id]
    assert d.frame(d.resolve(ta)).png != d.frame(d.resolve(tb)).png
    d.revoke(alice, d.resolve(ta).id)
    assert d.resolve(ta) is None and d.resolve(tb) is not None
    d.register(alice, {"model": "trmnl", "mac": "AA:BB:CC:DD:EE:04"})
    tok = d.trmnl_setup("AA:BB:CC:DD:EE:04")
    assert tok and d.resolve(tok) is not None and d.trmnl_setup("AA:BB:CC:DD:EE:04") is None
    # vue glance (même table, kind = 'glance') : code à usage unique, empreintes seules, révocation par le titulaire
    from nokker.ambient.glance import Glance

    g = Glance(store=d.store)
    offer = g.offer(alice.account_id, A, "tp_a")
    got = g.claim(offer["code"])
    assert g.resolve(got["token"]).kind == "glance" and d.resolve(got["token"]) is None
    with pytest.raises(Exception):
        g.claim(offer["code"])
    with psycopg.connect(pg["app"], autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM ambient_displays WHERE token_sha256=%s OR pair_sha256=%s",
                            (got["token"], offer["code"].replace("-", ""))).fetchone()[0] == 0
    assert [x["kind"] for x in g.devices(alice.account_id)["devices"]] == ["glance"]
    assert all(x.kind == "eink" for x in d.store.active())
    assert not d.store.revoke_owned(bob.account_id, got["device"]["id"], 1.0)
    g.revoke(alice.account_id, got["device"]["id"])
    assert g.resolve(got["token"]) is None
    assert apply_migrations(pg["owner"], strict=True)  # rejouée sans erreur


@requires_pg
def test_erase_removes_glance_devices_and_token_gets_401(pg, pod_env, monkeypatch):  # noqa: F811
    """`nokker erase` d'un compte efface ses vues glance (y compris celle rattachée au foyer d'un AUTRE locataire, par
    la colonne account_id) ; le jeton nkg_ présenté au vrai serveur prend alors 401. Les vues d'un autre compte
    restent."""
    import json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from nokker.admin.erase import erase_account
    from nokker.ambient import http as ambient_http
    from nokker.ambient.displays import PgDisplayStore
    from nokker.ambient.glance import Glance
    from nokker.api.server import make_handler
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.tenancy.store import PgTenancy
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals

    tenancy = PgTenancy(pg["app"])
    ana = tenancy.create_account("ana-glance@example.org")
    cam = tenancy.create_account("cam-glance@example.org")
    g = Glance(store=PgDisplayStore(pg["app"]))
    t_own = g.claim(g.offer(ana.account_id, ana.tenant_id)["code"])["token"]
    t_house = g.claim(g.offer(ana.account_id, cam.tenant_id, "tp_foyer_cam")["code"])["token"]  # foyer payé par cam
    t_cam = g.claim(g.offer(cam.account_id, cam.tenant_id)["code"])["token"]

    ambient_http.configure(ambient_http.Ambient(glance=g))
    kernel, _ = make_kernel_with_approvals(InMemoryApprovals())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {}, approvals=InMemoryApprovals(),
                                                               tenancy=tenancy, open_api=False,
                                                               internal_tenant=False))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def view(token):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/glance", headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    try:
        assert view(t_own)[0] == 200 and view(t_house)[0] == 200
        r = erase_account(pg["app"], account_id=ana.account_id, apply=True, confirm=ana.account_id)
        assert r.applied
        for tok in (t_own, t_house):
            st, out = view(tok)
            assert st == 401 and out["error"]["code"] == "bad_glance_token"
        assert view(t_cam)[0] == 200  # l'autre compte n'est pas touché
        assert g.devices(ana.account_id)["devices"] == []
        assert len(g.devices(cam.account_id)["devices"]) == 1
    finally:
        httpd.shutdown()
        ambient_http.configure(None)
