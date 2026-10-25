"""Pages publiques (docs/LOKKAL.md §7.26, docs/BUBBLES.md §10.4) : publier sur carte forte, une version figée, un id
public distinct du `page_id`, jamais une page `local`, dépublier = l'URL meurt et la copie publique part ; nokker.ai
garde l'empreinte de l'id public avec la copie et la rend au relais machine éteinte ; registre partagé des jetons de
pages consommés."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from nokker.cloud import http as cloud_http
from nokker.cloud import mirror_pages as mp
from nokker.pages.store import PageError, PageStore, is_public_id
from tests.unit.bubbles.conftest import clock, hub, rec  # noqa: F401 — fixtures du hub
from tests.unit.test_mirror_pages import PAGE, _body, _call, _worker, cloud  # noqa: F401 — fixture

HTML = b"<!doctype html><title>t</title><body><p>v1</p></body>"


# ─────────────────────────────── magasin de la machine ───────────────────────────────


def test_publish_freezes_the_signed_version_and_unpublish_kills_the_url(tmp_path):
    s = PageStore(tmp_path / "pages")
    m = s.create("th_a", "r.html", "Rapport", HTML)
    sha1 = hashlib.sha256(HTML).hexdigest()
    with pytest.raises(PageError):
        s.publish(m.page_id, 1, "0" * 64)                       # empreinte de la carte ≠ version : refus
    pub = s.publish(m.page_id, 1, sha1, index=False)
    assert pub.public and pub.public_version == 1 and is_public_id(pub.public_id) and pub.public_id != m.page_id
    assert m.page_id[3:] not in pub.public_id
    assert s.by_public_id(pub.public_id).page_id == m.page_id and s.by_public_id(m.page_id) is None
    # une nouvelle version n'est pas publique tant qu'on ne republie pas ; republier garde l'URL
    s.snapshot(m.page_id, HTML.replace(b"v1", b"v2"))
    assert s.get(m.page_id).public_version == 1 and s.read(m.page_id, 1)[1] == HTML
    again = s.publish(m.page_id, 2, hashlib.sha256(HTML.replace(b"v1", b"v2")).hexdigest(), index=True)
    assert again.public_id == pub.public_id and again.public_version == 2 and again.public_index
    old_id = again.public_id
    s.unpublish(m.page_id)
    assert s.by_public_id(old_id) is None and not s.get(m.page_id).public
    third = s.publish(m.page_id, 2, hashlib.sha256(HTML.replace(b"v1", b"v2")).hexdigest())
    assert third.public_id != old_id                              # dépubliée : l'ancienne URL ne revit jamais
    # rechargée depuis le disque (méta d'avant ce lot comprise : champs absents = privée)
    fresh = PageStore(tmp_path / "pages")
    assert fresh.by_public_id(third.public_id).public_version == 2
    meta = json.loads((tmp_path / "pages" / m.page_id / "meta.json").read_text())
    for k in ("public_id", "public_version", "public_index", "locality"):
        meta.pop(k)
    meta["inconnu"] = 1
    (tmp_path / "pages" / m.page_id / "meta.json").write_text(json.dumps(meta))
    old = PageStore(tmp_path / "pages").get(m.page_id)
    assert old is not None and old.public_id is None


def test_a_local_page_never_becomes_public(tmp_path):
    s = PageStore(tmp_path / "pages")
    m = s.create("th_a", "r.html", "Rapport", HTML)
    pub = s.publish(m.page_id, 1, hashlib.sha256(HTML).hexdigest())
    pinned = s.set_locality(m.page_id, True)                       # épingler dépublie
    assert pinned.locality == "local" and not pinned.public and s.by_public_id(pub.public_id) is None
    with pytest.raises(PageError):
        s.publish(m.page_id, 1, hashlib.sha256(HTML).hexdigest())
    assert s.set_locality(m.page_id, False).locality is None
    assert "public_id" not in s.get(m.page_id).state()             # l'id public ne va pas dans la bulle


def test_publish_card_is_strong_bound_to_the_version_and_not_for_the_local_token(tmp_path):
    from nokker.approvals.errors import ProofInvalid
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.pages.public import PUBLISH_RULE, PUBLISH_TOOL, publish_handler, request_publish

    s = PageStore(tmp_path / "pages")
    m = s.create("th_a", "r.html", "Rapport", HTML)
    approvals = InMemoryApprovals()
    card = request_publish(approvals, s, m, index=False, by="local-owner")
    assert card.strength == "strong" and card.rule == PUBLISH_RULE and card.tool_name == PUBLISH_TOOL
    assert card.arguments["sha256"] == hashlib.sha256(HTML).hexdigest() and card.arguments["version"] == 1
    assert "PUBLIQUE" in card.reason and "noindex" in card.reason
    with pytest.raises(ProofInvalid):
        approvals.decide_local(card.id, "approve", "local-owner")  # jamais au jeton local
    assert approvals.get(card.id).status == "pending"
    # la décision (signée ailleurs) applique la version de la carte, même si la page a bougé depuis
    s.snapshot(m.page_id, HTML.replace(b"v1", b"v2"))
    seen = []
    handle = publish_handler(s, after=seen.append)
    card.status = "approved"
    assert "publique (version 1)" in handle(card)
    assert s.get(m.page_id).public_version == 1 and seen[0].page_id == m.page_id
    card.status = "rejected"
    s.unpublish(m.page_id)
    assert handle(card) == "" and not s.get(m.page_id).public
    # page épinglée `local` entre la carte et la décision : refus à l'exécution
    s.set_locality(m.page_id, True)
    card.status = "approved"
    with pytest.raises(PageError):
        handle(card)
    with pytest.raises(PageError):
        request_publish(approvals, s, s.get(m.page_id), index=False, by="local-owner")
    # refuser reste possible au jeton local
    s.set_locality(m.page_id, False)
    other = request_publish(approvals, s, s.get(m.page_id), index=True, by="local-owner")
    assert approvals.decide_local(other.id, "reject", "local-owner").status == "rejected"


# ─────────────────────────────── routes du noyau local ───────────────────────────────


class Link:
    """Lien de compte vu par les routes : droit de publier (nokker.ai), retrait de la copie publique."""

    def __init__(self, allowed=True):
        self.allowed, self.purged = allowed, []

    def pages_public(self, ttl_s=300.0):  # même signature que LocalLink (la publication relit, ttl_s=0)
        return self.allowed

    def purge_public(self, page_id):
        self.purged.append(page_id)
        return "done"

    def pages_token(self, page_id, embed=False, download=False):
        return {"url": "https://atelier--pages.nokker.app/_auth?token=t", "expires_at": 1}


def _http(port, method, path, token=None, body=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


@pytest.fixture
def lokkal(tmp_path):
    from nokker.api.server import make_handler
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.local.api import LOCAL_OWNER
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.approvals.conftest import telegram_init_data
    from tests.unit.test_local_api import _api

    bot = "123456:FAKE-BOT-TOKEN"
    approvals = InMemoryApprovals(telegram_bot_token=bot)
    kernel, _ = make_kernel_with_approvals(approvals)
    api = _api(tmp_path)
    api.state_dir = tmp_path / "state"
    api.link = Link()
    api.setup.state["relay_host"] = "atelier.nokker.app"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
        kernel, {api.token: LOCAL_OWNER, "key-alice": "alice"}, approvals=approvals, local_api=api,
        telegram_bot_token=bot))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield port, api, approvals, (bot, telegram_init_data)
    httpd.shutdown()


def _sign_with_phone(port, approvals, card_id, bot, init_data_of, decision="approve"):
    """Une décision signée par un appareil enrôlé (Mini App : clé + initData, accepté pour une carte forte)."""
    from tests.unit.api.test_approvals_routes import gen_keypair, sign_p1363

    priv, spki = gen_keypair()
    init = init_data_of(bot, 1000000001)
    st, _, raw = _http(port, "POST", "/v1/devices", "key-alice", {"public_key_spki": spki, "name": "tel",
                                                                   "init_data": init})
    device = json.loads(raw)
    approvals.confirm_device_cli(device["code"])
    ch = json.loads(_http(port, "POST", f"/v1/approvals/{card_id}/challenge", "key-alice", {})[2])
    sig = sign_p1363(priv, f"{ch['challenge']}.{decision}".encode())
    return _http(port, "POST", f"/v1/approvals/{card_id}/decision", "key-alice",
                 {"decision": decision, "device_id": device["id"], "challenge": ch["challenge"], "signature": sig,
                  "init_data": init})


def test_publish_route_card_phone_decision_public_url_and_unpublish(lokkal):
    port, api, approvals, (bot, init_of) = lokkal
    meta = api.pages.create("th_a", "index.html", "Démo", HTML)
    pid = meta.page_id
    assert _http(port, "POST", f"/v1/pages/{pid}/publish", body={})[0] == 401
    st, _, raw = _http(port, "POST", f"/v1/pages/{pid}/publish", api.token, {"index": "oui"})
    assert st == 400
    st, _, raw = _http(port, "POST", f"/v1/pages/{pid}/publish", api.token, {})
    card = json.loads(raw)
    assert st == 202 and card["strength"] == "strong" and card["rule"] == "pages:publish" and card["version"] == 1
    assert not api.pages.get(pid).public                                   # rien de public avant la signature
    # le jeton local ne l'approuve pas (le refuser, si)
    st, _, raw = _http(port, "POST", f"/v1/local/approvals/{card['approval_id']}/decide", api.token,
                       {"decision": "approve"})
    assert st == 403 and json.loads(raw)["error"]["code"] == "device_key_required"
    # signée au téléphone : la version de la carte devient publique
    st, _, raw = _sign_with_phone(port, approvals, card["approval_id"], bot, init_of)
    assert st == 200 and json.loads(raw)["status"] == "executed"
    pub = api.pages.get(pid)
    assert pub.public and pub.public_version == 1 and pub.public_id
    # URL publique : une autre origine (--pub) et un autre id ; le lien privé ne la porte pas dans son jeton
    st, _, raw = _http(port, "POST", f"/v1/pages/{pid}/link", api.token, {})
    link = json.loads(raw)
    assert link["public_url"] == f"https://atelier--pub.nokker.app/{pub.public_id}" and pid not in link["public_url"]
    # servie par son id public seulement, en-têtes stricts ; jamais par le page_id sans preuve
    st, h, body = _http(port, "GET", f"/v1/pages/public/{pub.public_id}")
    assert st == 200 and body == HTML
    assert "connect-src 'none'" in h["Content-Security-Policy"] and "form-action 'none'" in h["Content-Security-Policy"]
    assert "allow-popups-to-escape-sandbox" in h["Content-Security-Policy"]  # « Signaler » ouvre nokker.ai hors du sandbox
    assert "allow-same-origin" not in h["Content-Security-Policy"]
    assert h["X-Robots-Tag"] == "noindex, nofollow" and h["X-Nokker-Index"] == "0" and "Set-Cookie" not in h
    assert h["Referrer-Policy"] == "no-referrer" and h["X-Content-Type-Options"] == "nosniff"
    assert _http(port, "GET", f"/v1/pages/{pid}")[0] == 404
    # une version plus récente ne sort pas sans nouvelle carte
    api.pages.snapshot(pid, HTML.replace(b"v1", b"v2"))
    assert _http(port, "GET", f"/v1/pages/public/{pub.public_id}")[2] == HTML
    # dépublier : tout de suite, sans carte ; l'URL meurt ; la copie publique est retirée chez nokker.ai
    assert _http(port, "POST", f"/v1/pages/{pid}/unpublish", body={})[0] == 401
    st, _, raw = _http(port, "POST", f"/v1/pages/{pid}/unpublish", api.token, {})
    assert st == 200 and json.loads(raw) == {"page_id": pid, "public": False, "purge": "done"}
    assert api.link.purged == [pid]
    assert _http(port, "GET", f"/v1/pages/public/{pub.public_id}")[0] == 404
    assert "public_url" not in json.loads(_http(port, "POST", f"/v1/pages/{pid}/link", api.token, {})[2])


def test_publish_refused_for_local_unlinked_or_not_allowed(lokkal):
    port, api, _, _ = lokkal
    meta = api.pages.create("th_a", "index.html", "Démo", HTML)
    pid = meta.page_id
    st, _, raw = _http(port, "PUT", f"/v1/pages/{pid}/locality", api.token, {"locality": "local"})
    assert st == 200 and json.loads(raw)["locality"] == "local"
    st, _, raw = _http(port, "POST", f"/v1/pages/{pid}/publish", api.token, {})
    assert st == 409 and json.loads(raw)["error"]["code"] == "local_page"
    assert _http(port, "PUT", f"/v1/pages/{pid}/locality", api.token, {"locality": "both"})[0] == 400
    assert _http(port, "PUT", f"/v1/pages/{pid}/locality", api.token, {"locality": None})[0] == 200
    api.link.allowed = False
    st, _, raw = _http(port, "POST", f"/v1/pages/{pid}/publish", api.token, {})
    assert st == 403 and json.loads(raw)["error"]["code"] == "pages_public_not_allowed"
    api.setup.state["relay_host"] = None
    st, _, raw = _http(port, "POST", f"/v1/pages/{pid}/publish", api.token, {})
    assert st == 409 and json.loads(raw)["error"]["code"] == "not_linked"
    assert _http(port, "GET", f"/v1/pages/{pid}/publish", api.token)[0] == 404


def test_pinning_a_public_page_local_unpublishes_and_purges(lokkal):
    port, api, _, _ = lokkal
    meta = api.pages.create("th_a", "index.html", "Démo", HTML)
    pub = api.pages.publish(meta.page_id, 1, hashlib.sha256(HTML).hexdigest())
    st, _, raw = _http(port, "PUT", f"/v1/pages/{meta.page_id}/locality", api.token, {"locality": "local"})
    assert st == 200 and json.loads(raw) == {"page_id": meta.page_id, "locality": "local", "public": False,
                                             "purge": "done"}
    assert _http(port, "GET", f"/v1/pages/public/{pub.public_id}")[0] == 404


def test_relayed_hosts_include_both_page_origins(tmp_path):
    from tests.unit.test_local_api import _api

    api = _api(tmp_path)
    api.relay_host = "atelier.nokker.app"
    api.tunnel = type("T", (), {"state": "up", "secret": "s"})()
    for h in ("atelier.nokker.app", "atelier--pages.nokker.app", "atelier--pub.nokker.app"):
        assert api.host_ok(h), h
    for h in ("autre--pub.nokker.app", "atelier--x.nokker.app", "atelier.nokker.app.evil", "x.atelier.nokker.app"):
        assert not api.host_ok(h), h
    api.tunnel.state = "down"
    assert not api.host_ok("atelier--pub.nokker.app")


def test_bubble_follows_publication(hub, rec, tmp_path):  # noqa: F811
    s = PageStore(tmp_path / "pages")
    m = s.create("th_a", "r.html", "Rapport", HTML)
    hub.threads.ensure("th_a", "local-owner", "x")
    b = hub.create_kernel_bubble("th_a", kind="artifact", title="Rapport", state=m.state())
    pub = s.publish(m.page_id, 1, hashlib.sha256(HTML).hexdigest())
    assert hub.page_changed(pub) == 1
    st = hub.bubbles.get(b.id).state
    assert st["public"] is True and st["public_version"] == 1 and "public_id" not in st and st["path"] == "r.html"


# ─────────────────────────────── miroir : la copie porte sa publication ───────────────────────────────


def test_public_copy_served_offline_only_for_the_published_version(cloud):  # noqa: F811
    port, mt, machine, store, links, tmp = cloud
    cloud_http._cloud.pages_public_allowed = lambda acc: acc == "us_p"
    pages = PageStore(tmp / "pages")
    m = pages.create("th_1", "bilan.html", "Bilan", PAGE)
    state: dict = {}
    w = _worker(port, mt, pages, state)
    w.set_page_mirror(m.page_id, True)
    assert w.push_pages_once() == 1
    key = {"X-Nokker-Relay-Key": "k" * 32}

    def offline(public_id, k=key):
        return _call(port, "POST", "/v1/gateway/mirror/public-page", {"host": machine.host, "public_id": public_id},
                     headers=k)

    pub = pages.publish(m.page_id, 1, hashlib.sha256(PAGE).hexdigest(), index=True)
    assert offline(pub.public_id)[0] == 404                # pas encore recopiée avec sa publication
    assert w.push_pages_once() == 1                        # la publication change la copie : elle repart
    st, got = offline(pub.public_id)
    html = base64.b64decode(got["page"]["html_b64"]).decode()
    assert st == 200 and got["page"]["index"] is True and "<h1>Bilan</h1>" in html and "machine hors ligne" in html
    row = store.get("tn_p", machine.id, m.page_id)
    assert row["public_sha256"] == hashlib.sha256(pub.public_id.encode()).hexdigest()
    assert pub.public_id not in json.dumps({k: v for k, v in row.items() if k != "html"})  # l'empreinte, pas l'id
    assert offline(pub.public_id, {"X-Nokker-Relay-Key": "x" * 32})[0] == 404
    assert offline("AAAAAAAAAAAAAAAAAAAAAAAA")[0] == 404 and offline(m.page_id)[0] == 404
    # nouvelle version non publiée : la copie suit, sans publication (la version signée n'est plus celle copiée)
    pages.snapshot(m.page_id, PAGE.replace(b"Bilan</h1>", b"Bilan v2</h1>"))
    assert w.push_pages_once() == 1 and offline(pub.public_id)[0] == 404
    pages.publish(m.page_id, 2, hashlib.sha256(PAGE.replace(b"Bilan</h1>", b"Bilan v2</h1>")).hexdigest())
    w.push_pages_once()
    assert offline(pub.public_id)[0] == 200
    # droit retiré au compte : plus servie (et plus gardée publique à la copie suivante)
    cloud_http._cloud.pages_public_allowed = lambda acc: False
    assert offline(pub.public_id)[0] == 404
    assert _call(port, "GET", "/v1/gateway/machine", token=mt)[1]["pages_public"] is False
    cloud_http._cloud.pages_public_allowed = lambda acc: acc == "us_p"
    assert _call(port, "GET", "/v1/gateway/machine", token=mt)[1]["pages_public"] is True
    # dépublier : retrait immédiat chez nokker.ai, la copie privée reste
    pages.unpublish(m.page_id)
    assert w.purge_public(m.page_id) == "done"
    assert offline(pub.public_id)[0] == 404 and store.get("tn_p", machine.id, m.page_id) is not None
    assert w.push_pages_once() == 0                         # rien à recopier : la marque a suivi le retrait


def test_unpublish_purge_waits_and_retries_when_nokker_ai_is_down(cloud, monkeypatch):  # noqa: F811
    import httpx

    port, mt, machine, store, _, tmp = cloud
    cloud_http._cloud.pages_public_allowed = lambda acc: True
    pages = PageStore(tmp / "pages")
    m = pages.create("th_1", "bilan.html", "Bilan", PAGE)
    w = _worker(port, mt, pages, {})
    w.set_page_mirror(m.page_id, True)
    pub = pages.publish(m.page_id, 1, hashlib.sha256(PAGE).hexdigest())
    w.push_pages_once()
    assert store.get("tn_p", machine.id, m.page_id)["public_sha256"]
    pages.unpublish(m.page_id)
    real = w._http

    class Down:
        def delete(self, *a, **k):
            raise httpx.ConnectError("coupé")

    w._http = lambda: Down()
    assert w.purge_public(m.page_id) == "pending" and w.status()["pending"]["pages"] >= 1
    w._http = real
    w.push_pages_once()                                     # le retrait en retard passe en premier
    assert store.get("tn_p", machine.id, m.page_id)["public_sha256"] is None
    assert not (w.state.get("mirror_public_purge") or {})
    del pub


def test_cloud_refuses_bad_public_and_ignores_it_without_the_right():
    s = mp.MemoryMirrorPages()
    sha = hashlib.sha256(b"AbCdEfGhIjKlMnOpQrStUv_-").hexdigest()
    ok = {"id_sha256": sha, "index": False, "version": 1}
    for bad in ({"id_sha256": "x", "index": False, "version": 1}, {**ok, "version": 2}, {**ok, "index": "non"}, "oui"):
        assert mp.push_page(s, "tn", "mc", "pg_0123456789abcdef", _body(PAGE, public=bad), 10 ** 6, 1.0)[0] == 400
    st, out = mp.push_page(s, "tn", "mc", "pg_0123456789abcdef", _body(PAGE, public=ok), 10 ** 6, 1.0)
    assert st == 200 and out["public"] is False and s.get("tn", "mc", "pg_0123456789abcdef")["public_sha256"] is None
    st, out = mp.push_page(s, "tn", "mc", "pg_0123456789abcdef", _body(PAGE, public=ok), 10 ** 6, 1.0,
                           public_allowed=True)
    assert out["public"] is True and s.get_public("tn", "mc", sha)["title"] == "Bilan"
    assert s.clear_public("tn", "mc", "pg_0123456789abcdef") and s.get_public("tn", "mc", sha) is None
    assert not s.clear_public("tn", "mc", "pg_0123456789abcdef")


def test_unpublish_route_on_nokker_ai_is_a_right(cloud):  # noqa: F811
    port, mt, machine, store, _, _ = cloud
    assert _call(port, "DELETE", "/v1/mirror/pages/pg_0123456789abcdef/public")[0] == 401
    assert _call(port, "DELETE", "/v1/mirror/pages/pg_0123456789abcdef/public", token=mt) == (200, {"unpublished": False})
    assert _call(port, "PUT", "/v1/mirror/pages/pg_0123456789abcdef/public", {}, token=mt)[0] == 404
    cloud_http._cloud.mirror_allowed = lambda acc: False    # abonnement échu : dépublier reste permis
    assert _call(port, "DELETE", "/v1/mirror/pages/pg_0123456789abcdef/public", token=mt)[0] == 200


# ─────────────────────────────── registre partagé des jetons de pages ───────────────────────────────


def test_used_jti_registry_route(cloud):  # noqa: F811
    port, mt, _, _, _, _ = cloud
    cloud_http._cloud.used_jti = mp.MemoryUsedJti()
    key = {"X-Nokker-Relay-Key": "k" * 32}
    import time as _t

    exp = int(_t.time()) + 60
    assert _call(port, "POST", "/v1/gateway/pages-jti", {"jti": "j1", "exp": exp}, headers=key) == (200, {"first": True})
    assert _call(port, "POST", "/v1/gateway/pages-jti", {"jti": "j1", "exp": exp}, headers=key) == (409, {"first": False})
    assert _call(port, "POST", "/v1/gateway/pages-jti", {"jti": "j2", "exp": exp}, headers=key)[0] == 200
    assert _call(port, "POST", "/v1/gateway/pages-jti", {"jti": "j3", "exp": exp})[0] == 404       # sans la clé
    assert _call(port, "POST", "/v1/gateway/pages-jti", {"jti": "j3", "exp": exp}, token=mt)[0] == 404
    for bad in ({"jti": "", "exp": exp}, {"jti": "x" * 200, "exp": exp}, {"jti": "j4"}, {"jti": 4, "exp": exp}):
        assert _call(port, "POST", "/v1/gateway/pages-jti", bad, headers=key)[0] == 400, bad
    # gardé au plus 10 min, puis oublié (un jeton de pages vit 60 s)
    reg = mp.MemoryUsedJti()
    assert mp.use_jti(reg, {"jti": "j", "exp": 10 ** 12}, now=1000.0)[0] == 200
    assert next(iter(reg.seen.values())) == 1600.0
    assert mp.use_jti(reg, {"jti": "j", "exp": 10 ** 12}, now=1500.0)[0] == 409
    assert mp.use_jti(reg, {"jti": "j", "exp": 10 ** 12}, now=1601.0)[0] == 200
    assert "j" not in reg.seen                               # empreintes seulement
