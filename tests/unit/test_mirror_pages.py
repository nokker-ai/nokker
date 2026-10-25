"""Pages servies depuis le miroir quand la machine est éteinte (docs/LOKKAL.md §6.6, §7.23 ; docs/GATEWAY.md) : la
machine copie chez nokker.ai les pages dont l'utilisateur a demandé la copie (jamais une page `local`), nokker.ai les
garde sous la RLS du locataire avec un plafond par compte, et les rend au relais (clé du relais) avec un bandeau
« copie du <date>, machine hors ligne ». Purge à la suppression, à l'effacement de la machine et à la révocation."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import httpx
import pytest

from nokker.api.server import make_handler
from nokker.cloud import http as cloud_http
from nokker.cloud import mirror_pages as mp
from nokker.cloud.http import CloudLink
from nokker.cloud.link import LinkService, Signer
from nokker.pages.store import PageStore
from tests.unit.api.test_server import make_kernel

PAGE = b"<!doctype html><html><head><title>Bilan</title></head><body><h1>Bilan</h1></body></html>"


def _body(content: bytes, **kw) -> dict:
    return {"title": "Bilan", "version": 1, "sha256": hashlib.sha256(content).hexdigest(), "locality": "both",
            "html_b64": base64.b64encode(content).decode(), "updated_at": 1_800_000_000.0, **kw}


# ─────────────────────────────── nokker.ai, sans HTTP ───────────────────────────────


def test_banner_goes_inside_body_and_says_when():
    out = mp.with_banner(PAGE, 1_800_000_000.0).decode()
    assert out.index("<body>") < out.index("data-nokker-mirror") < out.index("<h1>")
    assert "Copie du 2027-01-15 08:00 UTC, machine hors ligne" in out
    bare = mp.with_banner(b"<p>sans body</p>", 1_800_000_000.0).decode()
    assert bare.startswith("<div data-nokker-mirror") and bare.endswith("<p>sans body</p>")
    head = mp.with_banner(b"<!DOCTYPE html>\n<p>x</p>", 1_800_000_000.0).decode()
    assert head.startswith("<!DOCTYPE html><div data-nokker-mirror")  # jamais avant le doctype (mode quirks)


def test_push_validates_locality_integrity_size_and_quota():
    store = mp.MemoryMirrorPages()
    ok = mp.push_page(store, "tn_a", "mc_a", "pg_0123456789abcdef", _body(PAGE), quota=1000, now=1.0)
    assert ok == (200, {"stored": True, "used": len(PAGE), "quota": 1000, "public": False})
    assert mp.push_page(store, "tn_a", "mc_a", "pg_0123456789abcdef", _body(PAGE), quota=1000, now=2.0)[1]["stored"]
    assert store.used("tn_a") == len(PAGE)  # même page remplacée : pas comptée deux fois
    # règle d'or, seconde barrière : une page qui se dit `local` (ou ne dit rien) n'est jamais gardée
    st, err = mp.push_page(store, "tn_a", "mc_a", "pg_1111111111111111", _body(PAGE, locality="local"), 1000, 1.0)
    assert st == 403 and err["error"]["code"] == "local_page"
    silent = {k: v for k, v in _body(PAGE).items() if k != "locality"}
    assert mp.push_page(store, "tn_a", "mc_a", "pg_1111111111111111", silent, 1000, 1.0)[0] == 403
    assert mp.push_page(store, "tn_a", "mc_a", "pg_1111111111111111", _body(PAGE, sha256="0" * 64), 1000, 1.0)[0] == 400
    assert mp.push_page(store, "tn_a", "mc_a", "../etc/passwd", _body(PAGE), 1000, 1.0)[0] == 404
    big = b"<p>" + b"x" * (mp.MAX_PAGE_BYTES + 1)
    assert mp.push_page(store, "tn_a", "mc_a", "pg_2222222222222222", _body(big), 10 ** 9, 1.0)[0] == 413
    st, err = mp.push_page(store, "tn_a", "mc_b", "pg_3333333333333333", _body(PAGE * 20), quota=1000, now=1.0)
    assert st == 413 and err["error"]["code"] == "mirror_quota" and err["used"] == len(PAGE)
    assert store.used("tn_b") == 0 and store.of_machine("tn_b", "mc_a") == []  # par locataire


def test_copy_for_relay_checks_name_tenant_machine_and_subscription():
    links = LinkService()
    dev = links.device("atelier")
    links.approve(dev["user_code"], "us_p", "tn_p")
    links.token(dev["device_code"])
    m = links.machines_of("us_p")[0]
    store = mp.MemoryMirrorPages()
    mp.push_page(store, "tn_p", m.id, "pg_0123456789abcdef", _body(PAGE), 10 ** 6, now=1_800_000_000.0)
    allowed = {"us_p"}

    def get(**kw):
        args = {"host": m.host, "tenant": "tn_p", "machine": None, "page_id": "pg_0123456789abcdef", **kw}
        return mp.copy_for_relay(store, links, lambda acc: acc in allowed, **args)

    got = get()
    assert got["machine_id"] == m.id and "machine hors ligne" in base64.b64decode(got["page"]["html_b64"]).decode()
    assert get(machine=m.id)["page"] is not None
    assert get(tenant="tn_autre") is None  # cookie d'un autre compte sur ce nom
    assert get(machine="mc_0000000000000000") is None
    assert get(host="autre.nokker.app") is None
    assert get(page_id="pg_ffffffffffffffff") == {"machine_id": m.id, "name": m.name, "page": None}
    assert get(page_id=None)["page"] is None  # contrôle de la machine seul (entrée /_auth)
    allowed.clear()
    assert get() is None  # abonnement échu : plus servie
    allowed.add("us_p")
    links.revoke_machine("us_p", m.id)
    assert get() is None


# ─────────────────────────────── de bout en bout : machine → nokker.ai → relais ───────────────────────────────


def _call(port, method, path, body=None, token=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=h, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def cloud(tmp_path):
    from nokker.store.pg import MemoryStore

    links = LinkService()
    pages_store = mp.MemoryMirrorPages()
    cloud_http.configure(CloudLink(
        links=links, signer=Signer.generate(), identity=lambda h: "us_p" if h.headers.get("Authorization") == "Bearer sess" else None,
        tenant_of=lambda a: "tn_p" if a == "us_p" else None, router_base="http://127.0.0.1:1", router_key=lambda: "x",
        store=MemoryStore(), mirror_allowed=lambda acc: acc == "us_p", mirror_pages=pages_store,
        mirror_pages_quota=3 * len(PAGE) + 10, relay_key="k" * 32))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    dev = _call(port, "POST", "/v1/link/device", {"machine": "atelier"})[1]
    _call(port, "POST", "/v1/link/approve", {"user_code": dev["user_code"]}, token="sess")
    mt = _call(port, "POST", "/v1/link/token", {"device_code": dev["device_code"]})[1]["machine_token"]
    machine = links.machines_of("us_p")[0]
    yield port, mt, machine, pages_store, links, tmp_path
    cloud_http.configure(None)
    httpd.shutdown()


def _worker(port, mt, pages, state):
    from nokker.local.mirror import MirrorWorker
    from nokker.store.pg import MemoryStore

    return MirrorWorker(store=MemoryStore(), base_url=f"http://127.0.0.1:{port}", token=lambda: mt, state=state,
                        save=lambda: None, pages=pages)


def _relay_get(port, machine, page_id, key="k" * 32, tenant="tn_p"):
    return _call(port, "POST", "/v1/gateway/mirror/page",
                 {"host": machine.host, "tenant": tenant, "page_id": page_id}, headers={"X-Nokker-Relay-Key": key})


def test_only_pages_the_user_asked_for_are_copied_and_served_offline(cloud):
    port, mt, machine, store, _, tmp = cloud
    pages = PageStore(tmp / "pages")
    asked = pages.create("th_1", "bilan.html", "Bilan", PAGE)
    private = pages.create("th_1", "brouillon.html", "Brouillon", b"<p>ne sort jamais</p>")
    state: dict = {}
    w = _worker(port, mt, pages, state)
    assert w.push_pages_once() == 0 and store.of_machine("tn_p", machine.id) == []  # rien par défaut
    assert w.set_page_mirror(asked.page_id, True) and not w.set_page_mirror("pg_ffffffffffffffff", True)
    assert w.push_pages_once() == 1
    assert [r["page_id"] for r in store.of_machine("tn_p", machine.id)] == [asked.page_id]
    assert w.push_pages_once() == 0  # inchangée : rien ne repart
    st, got = _relay_get(port, machine, asked.page_id)
    html = base64.b64decode(got["page"]["html_b64"]).decode()
    assert st == 200 and "<h1>Bilan</h1>" in html and "machine hors ligne" in html
    assert _relay_get(port, machine, private.page_id) == (200, {"machine_id": machine.id, "name": machine.name,
                                                                 "page": None})
    assert _relay_get(port, machine, asked.page_id, key="mauvaise-cle-" * 3)[0] == 404  # sans la clé du relais
    assert _relay_get(port, machine, asked.page_id, tenant="tn_x")[0] == 404
    # une nouvelle version repart ; la source qui se trompe (choix posé, page `local` dans la liste) ne part pas
    pages.snapshot(asked.page_id, PAGE.replace(b"Bilan</h1>", b"Bilan v2</h1>"))
    assert w.push_pages_once() == 1
    assert "Bilan v2" in base64.b64decode(_relay_get(port, machine, asked.page_id)[1]["page"]["html_b64"]).decode()
    w.page_locality = lambda pid: "local"  # la source ment : le filtre à l'envoi retient tout
    w.push_pages_once()
    assert "ne sort jamais" not in json.dumps(store.of_machine("tn_p", machine.id))
    del w.page_locality


def test_purge_on_unchoose_delete_erase_and_revoke(cloud):
    port, mt, machine, store, _, tmp = cloud
    pages = PageStore(tmp / "pages")
    a = pages.create("th_1", "a.html", "A", PAGE)
    b = pages.create("th_1", "b.html", "B", PAGE)
    state: dict = {}
    w = _worker(port, mt, pages, state)
    for p in (a, b):
        w.set_page_mirror(p.page_id, True)
    assert w.push_pages_once() == 2
    w.set_page_mirror(a.page_id, False)  # l'utilisateur retire la copie
    assert w.push_pages_once() == 1 and [r["page_id"] for r in store.of_machine("tn_p", machine.id)] == [b.page_id]
    assert pages.delete(b.page_id)  # la page est supprimée sur la machine
    assert w.push_pages_once() == 1 and store.of_machine("tn_p", machine.id) == []
    c = pages.create("th_1", "c.html", "C", PAGE)
    w.set_page_mirror(c.page_id, True)
    w.push_pages_once()
    assert _call(port, "POST", "/v1/mirror/erase", {}, token=mt)[1]["erased_pages"] == 1
    w.state.pop("mirror_paused", None)
    w.state["mirror_pages_sent"] = {}
    w.push_pages_once()
    assert store.of_machine("tn_p", machine.id)
    assert _call(port, "POST", f"/v1/link/machines/{machine.id}/revoke", {}, token="sess")[0] == 200
    assert store.of_machine("tn_p", machine.id) == []


def test_quota_is_per_account_and_visible(cloud):
    port, mt, _, _, _, tmp = cloud
    pages = PageStore(tmp / "pages")
    state: dict = {}
    w = _worker(port, mt, pages, state)
    made = [pages.create("th_1", f"p{i}.html", f"P{i}", PAGE.replace(b"Bilan", f"Page {i}".encode()))
            for i in range(4)]
    for p in made:
        w.set_page_mirror(p.page_id, True)
    assert w.push_pages_once() == 3  # la 4e dépasse le plafond du compte
    view = {p["page_id"]: p for p in w.pages_view()["pages"]}
    assert sorted(v["state"] for v in view.values()) == ["quota", "sent", "sent", "sent"]
    assert w.push_pages_once() == 0  # refusée pour cette version : pas renvoyée à chaque passage
    st, listing = _call(port, "GET", "/v1/mirror/pages", token=mt)
    assert st == 200 and len(listing["pages"]) == 3 and listing["used"] <= listing["quota"]
    assert "html_b64" not in json.dumps(listing)
    w.set_page_mirror(made[0].page_id, False)
    assert w.push_pages_once() == 2  # retrait d'abord, place libérée : la 4e part dans le même passage
    assert sorted(v["state"] for v in w.pages_view()["pages"]) == ["off", "sent", "sent", "sent"]


def test_routes_need_machine_token_and_subscription(cloud):
    port, mt, machine, _, _, _ = cloud
    assert _call(port, "PUT", "/v1/mirror/pages/pg_0123456789abcdef", _body(PAGE))[0] == 401
    assert _call(port, "POST", "/v1/gateway/mirror/page", {"host": machine.host, "tenant": "tn_p"})[0] == 404
    cloud_http._cloud.mirror_allowed = lambda acc: False
    st, err = _call(port, "PUT", "/v1/mirror/pages/pg_0123456789abcdef", _body(PAGE), token=mt)
    assert st == 403 and err["error"]["code"] == "mirror_subscription"
    # retirer une copie reste un droit, abonnement échu compris
    assert _call(port, "DELETE", "/v1/mirror/pages/pg_0123456789abcdef", token=mt)[0] == 200


def test_page_store_delete(tmp_path):
    pages = PageStore(tmp_path / "pages")
    p = pages.create("th_1", "a.html", "A", PAGE)
    assert pages.delete(p.page_id) and pages.get(p.page_id) is None and not (tmp_path / "pages" / p.page_id).exists()
    assert not pages.delete(p.page_id) and not pages.delete("../x")
    assert PageStore(tmp_path / "pages").get(p.page_id) is None  # rien ne revient au redémarrage


def test_worker_network_cut_keeps_the_page_pending(tmp_path):
    from nokker.local.mirror import MirrorWorker
    from nokker.store.pg import MemoryStore

    pages = PageStore(tmp_path / "pages")
    p = pages.create("th_1", "a.html", "A", PAGE)
    down = {"on": True}
    seen = []

    def cloud(req: httpx.Request) -> httpx.Response:
        if down["on"]:
            raise httpx.ConnectError("coupé", request=req)
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={"stored": True, "used": 1, "quota": 10})

    state: dict = {}
    w = MirrorWorker(store=MemoryStore(), base_url="https://app.nokker.ai", token=lambda: "mt", state=state,
                     save=lambda: None, pages=pages, client=httpx.Client(transport=httpx.MockTransport(cloud)))
    w.set_page_mirror(p.page_id, True)
    assert w.push_pages_once() == 0 and not state.get("mirror_pages_sent")
    assert w.status()["channels"]["pages"]["failures"] == 1
    down["on"] = False
    assert w.push_pages_once() == 1 and seen == [("PUT", f"/v1/mirror/pages/{p.page_id}")]
    assert w.status()["channels"]["pages"]["failures"] == 0
