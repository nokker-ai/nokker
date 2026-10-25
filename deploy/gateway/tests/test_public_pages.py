"""Pages publiques (`<nom>--pub.nokker.app/<id public>`), isolation des cookies entre passerelles et registre partagé des
jetons de pages consommés (docs/LOKKAL.md §7.26)."""
import asyncio
import base64
import json
import os
import sys

import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay as R  # noqa: E402
from test_relay import TOKENS, H, check_token, open_tunnel  # noqa: E402

PUB = "AbCdEfGhIjKlMnOpQrStUv_-"          # 24 caractères [A-Za-z0-9_-]
HTML = b"<!doctype html><html><body><h1>Bilan public</h1></body></html>"
MACHINES = {"tok-jean": R.Machine("m1", "jean", "t_jean", True, pages_public=True),
            "tok-sans": R.Machine("m3", "sans", "t_sans", True, pages_public=False)}
S = R.Settings(cookie_secret=b"x" * 32)
P = "jean--pub.nokker.app"


async def check_machine(tok):
    return MACHINES.get(tok)


async def public_handler(req, body, ws):
    """Faux Lokkal : sert la page publique PUB (indexable si demandé), 404 sinon."""
    path = req["path"]
    if path == f"/v1/pages/public/{PUB}":
        hs = [["Content-Type", "text/html; charset=utf-8"], ["Set-Cookie", "suivi=1; Domain=nokker.app"],
              ["Content-Security-Policy", "default-src *"], ["X-Nokker-Index", "0"], ["X-Robots-Tag", "all"],
              ["X-Nokker-Page-Id", "pg_0123456789abcdef"]]
        await ws.send_str(json.dumps({"t": "res", "id": req["id"], "status": 200, "headers": hs,
                                      "body": base64.b64encode(HTML).decode(), "more": False}))
        return
    if path == "/v1/pages/public/indexable_page_000000000":
        await ws.send_str(json.dumps({"t": "res", "id": req["id"], "status": 200,
                                      "headers": [["Content-Type", "text/html"], ["X-Nokker-Index", "1"]],
                                      "body": base64.b64encode(HTML).decode(), "more": False}))
        return
    await ws.send_str(json.dumps({"t": "res", "id": req["id"], "status": 404, "headers": [],
                                  "body": base64.b64encode(b'{"error":{"message":"not found"}}').decode(),
                                  "more": False}))


class FakePublicMirror:
    """nokker.ai vu du relais pour les pages publiques copiées : `POST /v1/gateway/mirror/public-page`."""

    def __init__(self):
        self.calls = []

    async def __call__(self, body):
        self.calls.append(body)
        if body == {"host": "jean.nokker.app", "public_id": PUB}:
            return 200, {"page": {"title": "Bilan", "mirrored_at": 1_800_000_000.0, "index": False,
                                  "html_b64": base64.b64encode(HTML).decode()}}
        return 404, {"error": {"message": "not found"}}


class FakePublicCheck:
    """Registre de modération de nokker.ai vu du relais : `POST /v1/gateway/public-page/check` (§7.26, point 6)."""

    def __init__(self):
        self.calls, self.answer = [], (200, {"allowed": True})

    async def __call__(self, body):
        self.calls.append(body)
        return self.answer


@pytest.fixture
async def penv():
    mirror = FakePublicMirror()
    relay = R.Relay(S, check_machine, check_token, mirror_public=mirror, public_check=FakePublicCheck())
    client = TestClient(TestServer(R.make_app(relay)))
    await client.start_server()
    yield relay, client, mirror
    await client.close()


def strict_headers_ok(r, index=False):
    csp = r.headers["Content-Security-Policy"]
    for d in ("default-src 'none'", "script-src 'unsafe-inline'", "connect-src 'none'", "form-action 'none'",
              "frame-ancestors 'none'", "base-uri 'none'", "sandbox allow-scripts",
              "allow-popups-to-escape-sandbox"):  # le lien « Signaler » s'ouvre hors du sandbox
        assert d in csp, d
    assert "http" not in csp and "*" not in csp and "allow-same-origin" not in csp and "allow-forms" not in csp
    assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["Referrer-Policy"] == "no-referrer"
    assert r.headers["Cache-Control"] == "no-store" and r.headers["Content-Type"].startswith("text/html")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert ("X-Robots-Tag" in r.headers) is (not index)
    if not index:
        assert r.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert "Set-Cookie" not in r.headers


async def test_page_publique_en_ligne(penv):
    relay, c, mirror = penv
    seen = []
    ws, task = await open_tunnel(c, seen=seen, handler=public_handler)
    r = await c.get(f"/{PUB}", headers=H(P, Cookie="__Host-nk_pg=x; nk=1", Authorization="Bearer a.jean.sig",
                                         **{"CF-Connecting-IP": "1.2.3.4"}))
    body = await r.read()
    assert r.status == 200 and body.startswith(HTML[:-14]) and b"Bilan public" in body
    strict_headers_ok(r)
    # lien de signalement ajouté par le relais, sans script, avant </body>
    assert b"data-nokker-report" in body and b"https://nokker.ai/report?url=https%3A%2F%2Fjean--pub.nokker.app%2F" \
        in body and body.endswith(b"</body></html>") and b"<script" not in body
    # vers la machine : ni cookie, ni jeton, ni compte, ni IP du visiteur ; rien que la route publique
    req, _ = seen[-1]
    assert req["path"] == f"/v1/pages/public/{PUB}" and req["method"] == "GET"
    hdrs = {k.lower(): v for k, v in req["headers"]}
    assert hdrs["x-nokker-public"] == "1" and hdrs["host"] == P
    for k in ("cookie", "authorization", "x-nokker-account", "x-nokker-pages", "x-forwarded-for", "cf-connecting-ip"):
        assert k not in hdrs, k
    # HEAD, indexable sur option de la machine, requête ignorée
    assert (await c.head(f"/{PUB}", headers=H(P))).status == 200
    r = await c.get("/indexable_page_000000000?x=1", headers=H(P))
    assert r.status == 200
    strict_headers_ok(r, index=True)
    assert seen[-1][0]["path"] == "/v1/pages/public/indexable_page_000000000"
    # id inconnu de la machine (dépubliée) : 404 ; forme invalide : 404 sans rien demander à la machine
    n = len(seen)
    assert (await c.get("/AAAAAAAAAAAAAAAAAAAAAAAA", headers=H(P))).status == 404
    for bad in ("/pg_0123456789abcdef", "/court", "/" + PUB + "/x", "/_auth?token=p.ok1.sig", "/_s/x/y"):
        assert (await c.get(bad, headers=H(P))).status == 404, bad
    assert len(seen) == n + 1
    # écritures refusées ; jamais le miroir quand la machine est en ligne
    assert (await c.post(f"/{PUB}", headers=H(P))).status == 405
    assert mirror.calls == []
    task.cancel(); await ws.close()


async def test_page_publique_compte_sans_droit(penv):
    _, c, _ = penv
    seen = []
    ws, task = await open_tunnel(c, token="tok-sans", seen=seen, handler=public_handler)
    assert (await c.get(f"/{PUB}", headers=H("sans--pub.nokker.app"))).status == 404
    assert seen == []                      # nokker.ai ne donne pas le droit : la machine n'est pas sollicitée
    task.cancel(); await ws.close()


async def test_page_publique_machine_eteinte(penv):
    relay, c, mirror = penv
    r = await c.get(f"/{PUB}", headers=H(P, Cookie="__Host-nk_pg=x"))
    body = await r.read()
    assert r.status == 200 and b"Bilan public" in body and b"data-nokker-report" in body
    strict_headers_ok(r)
    assert r.headers["X-Nokker-Mirror-Copy"].startswith("2027-01-15T08:00")
    assert mirror.calls == [{"host": "jean.nokker.app", "public_id": PUB}]
    # pas de copie publique (dépubliée, jamais copiée) : hors ligne, rien d'autre
    assert (await c.get("/AAAAAAAAAAAAAAAAAAAAAAAA", headers=H(P))).status == 502
    # sans miroir configuré : hors ligne
    relay.mirror_public = None
    assert (await c.get(f"/{PUB}", headers=H(P))).status == 502


async def test_page_publique_plafond_de_debit(penv, monkeypatch):
    relay, c, _ = penv
    relay.pub_ip_rate = R.RateLimit(3, 60)
    relay.pub_page_rate = R.RateLimit(5, 60)
    ws, task = await open_tunnel(c, handler=public_handler)
    ok = [(await c.get(f"/{PUB}", headers=H(P, **{"CF-Connecting-IP": "9.9.9.9"}))).status for _ in range(4)]
    assert ok == [200, 200, 200, 429]                       # par page et par IP
    r = await c.get(f"/{PUB}", headers=H(P, **{"CF-Connecting-IP": "8.8.8.8"}))
    assert r.status == 200                                   # une autre IP passe
    assert (await c.get(f"/{PUB}", headers=H(P, **{"CF-Connecting-IP": "7.7.7.7"}))).status == 200
    r = await c.get(f"/{PUB}", headers=H(P, **{"CF-Connecting-IP": "6.6.6.6"}))
    assert r.status == 429 and r.headers["Retry-After"] == "60" and "Set-Cookie" not in r.headers  # plafond de la page
    task.cancel(); await ws.close()


async def test_page_publique_corps_trop_gros(penv, monkeypatch):
    _, c, _ = penv
    monkeypatch.setattr(R, "PUBLIC_MAX", 1000)

    async def gros(req, body, ws):
        await ws.send_str(json.dumps({"t": "res", "id": req["id"], "status": 200, "headers": [],
                                      "body": base64.b64encode(b"a" * 800).decode(), "more": True}))
        await ws.send_str(json.dumps({"t": "body", "id": req["id"], "data": base64.b64encode(b"b" * 800).decode(),
                                      "more": False}))
    ws, task = await open_tunnel(c, handler=gros)
    assert (await c.get(f"/{PUB}", headers=H(P))).status == 502
    task.cancel(); await ws.close()


def test_rapport_insere_et_echappe():
    href = "https://nokker.ai/report?url=https%3A%2F%2Fx"
    out = R.with_report(b"<html><body><p>a</p></body></html>", href)
    assert out.index(b"data-nokker-report") < out.index(b"</body>") and out.endswith(b"</body></html>")
    assert R.with_report(b"<p>sans body</p>", href).startswith(b"<p>sans body</p><a data-nokker-report")
    assert b'"><script>' not in R.with_report(b"x", 'https://n"><script>')


# ---- isolation des cookies entre passerelles (nokker.app pas encore sur la Public Suffix List)

async def test_cookies_toujours_host_et_sans_domaine():
    """Tout Set-Cookie du relais : préfixe __Host-, Secure, Path=/, aucun attribut Domain (un cookie posé pour
    `nokker.app` serait lu par toutes les passerelles)."""

    class Core:
        async def __call__(self, path, body, client_ip=""):
            return 200, {"access_token": "at", "expires_in": 900, "refresh_token": "rt"}

    relay = R.Relay(S, check_machine, check_token, Core())
    c = TestClient(TestServer(R.make_app(relay)))
    await c.start_server()
    try:
        ws, task = await open_tunnel(c)
        raw = []
        r = await c.get("/_auth?token=a.jean.sig&to=/pg_0123456789abcdef", headers=H("jean--pages.nokker.app"),
                        allow_redirects=False)
        raw += r.headers.getall("Set-Cookie", [])
        G = H(Origin="https://jean.nokker.app")
        r = await c.post("/_gw/token", json={"code": "c", "code_verifier": "v"}, headers=G)
        raw += r.headers.getall("Set-Cookie", [])
        r = await c.post("/_gw/logout", json={}, headers={**G, "Cookie": "__Host-nk_rt=rt"})
        raw += r.headers.getall("Set-Cookie", [])
        assert len(raw) == 3
        for line in raw:
            low = line.lower()
            assert line.startswith("__Host-") and "secure" in low and "path=/" in low and "domain" not in low, line
        task.cancel(); await ws.close()
    finally:
        await c.close()


def test_le_relais_refuse_de_poser_un_cookie_sans_prefixe():
    from aiohttp import web

    with pytest.raises(ValueError):
        R.set_host_cookie(web.Response(), "nk_pg", "x", samesite="Lax", max_age=60)


async def test_gw_refuse_une_requete_d_un_site_voisin():
    """Avant la PSL, `autre.nokker.app` est « same-site » pour `jean.nokker.app` : le cookie Strict partirait. Le relais
    exige l'origine exacte ET, quand le navigateur le dit, Sec-Fetch-Site = same-origin."""
    calls = []

    async def core(path, body, client_ip=""):
        calls.append(path)
        return 200, {"access_token": "at", "expires_in": 900, "refresh_token": "rt2"}

    relay = R.Relay(S, check_machine, check_token, core)
    c = TestClient(TestServer(R.make_app(relay)))
    await c.start_server()
    try:
        base = {"Host": "jean.nokker.app", "Cookie": "__Host-nk_rt=rt"}
        for origin, site in (("https://autre.nokker.app", None), ("https://jean.nokker.app", "same-site"),
                             ("https://jean.nokker.app", "cross-site"), ("null", None)):
            hs = {**base, "Origin": origin, **({"Sec-Fetch-Site": site} if site else {})}
            assert (await c.post("/_gw/refresh", json={}, headers=hs)).status == 403, (origin, site)
        assert calls == []
        ok = {**base, "Origin": "https://jean.nokker.app", "Sec-Fetch-Site": "same-origin"}
        assert (await c.post("/_gw/refresh", json={}, headers=ok)).status == 200
    finally:
        await c.close()


async def test_cookie_de_pages_seulement_pour_un_document(penv):
    """Le cookie d'onglet `__Host-nk_pg` (SameSite=Lax) ne vaut que pour une navigation de document : une page d'une
    autre passerelle (même site tant que nokker.app n'est pas sur la PSL) ne peut pas s'en servir en sous-ressource ou
    en iframe (l'iframe a sa capacité d'URL)."""
    _, c, _ = penv
    seen = []
    ws, task = await open_tunnel(c, seen=seen)
    PG = "jean--pages.nokker.app"
    ck = R.sign_cookie(S.cookie_secret, "t_jean", "jean", 2 ** 31)
    page = "/pg_0123456789abcdef"
    for dest in ("iframe", "image", "script", "empty", "frame", "object"):
        assert (await c.get(page, headers=H(PG, Cookie=f"__Host-nk_pg={ck}", **{"Sec-Fetch-Dest": dest}))).status \
            == 404, dest
    assert seen == []
    r = await c.get(page, headers=H(PG, Cookie=f"__Host-nk_pg={ck}", **{"Sec-Fetch-Dest": "document"}))
    assert r.status == 200 and ("X-Nokker-Account", "t_jean") in [tuple(h) for h in seen[-1][0]["headers"]]
    r = await c.get(page, headers=H(PG, Cookie=f"__Host-nk_pg={ck}"))   # navigateur sans Sec-Fetch-* : accepté
    assert r.status == 200
    task.cancel(); await ws.close()


async def test_pages_privees_sans_preuve_ne_vont_plus_a_la_machine(penv):
    """Une page privée ne devient jamais publique par son id : sans preuve, 404 du relais (la machine n'est pas
    interrogée) ; l'URL publique est une autre adresse, un autre id."""
    _, c, _ = penv
    seen = []
    ws, task = await open_tunnel(c, seen=seen)
    assert (await c.get("/pg_0123456789abcdef", headers=H("jean--pages.nokker.app"))).status == 404
    assert seen == []
    task.cancel(); await ws.close()


# ---- registre des jetons de pages consommés : mémoire (une réplique) ou partagé (nokker.ai)

async def test_registre_memoire_garde_le_comportement():
    reg = R.MemoryUsedTokens()
    assert await reg.first_use("j1", 2_000_000_060) is True
    assert await reg.first_use("j1", 2_000_000_060) is False
    assert await reg.first_use("j2", 2_000_000_060) is True


async def test_registre_partage_entre_deux_repliques():
    """Deux relais branchés sur le même registre (nokker.ai) : un jeton de pages consommé par l'un est refusé par
    l'autre. nokker.ai injoignable : refus (échec fermé)."""
    store, mode = {}, {"down": False}

    async def core(body):
        if mode["down"]:
            return 0, None
        if body["jti"] in store:
            return 409, {"first": False}
        store[body["jti"]] = body["exp"]
        return 200, {"first": True}

    shared = R.CoreUsedTokens(core)
    machines = {"tok-jean": R.Machine("m1", "jean", "t_jean", True)}

    async def cm(tok):
        return machines.get(tok)

    relays = [R.Relay(S, cm, check_token, used_tokens=shared) for _ in range(2)]
    clients = [TestClient(TestServer(R.make_app(r))) for r in relays]
    for c in clients:
        await c.start_server()
    try:
        tunnels = [await open_tunnel(c) for c in clients]
        PG = "jean--pages.nokker.app"
        r = await clients[0].get("/_auth?token=p.ok1.sig&to=/x", headers=H(PG), allow_redirects=False)
        assert r.status == 302
        r = await clients[1].get("/_auth?token=p.ok1.sig&to=/x", headers=H(PG), allow_redirects=False)
        assert r.status == 401                       # rejoué sur l'autre réplique
        assert store == {"j1": TOKENS["p.ok1.sig"]["exp"]}
        mode["down"] = True
        r = await clients[1].get("/_auth?token=p.ok2.sig&to=/x", headers=H(PG), allow_redirects=False)
        assert r.status == 401                       # registre injoignable : refus
        for ws, task in tunnels:
            task.cancel(); await ws.close()
    finally:
        for c in clients:
            await c.close()


def test_registre_choisi_par_l_environnement():
    env = {"NOKKER_RELAY_COOKIE_SECRET": "x" * 32}
    assert R.Settings.from_env(env).jti_store == "memory"
    assert R.Settings.from_env({**env, "NOKKER_RELAY_JTI_STORE": "core", "NOKKER_RELAY_CORE_KEY": "k" * 32}).jti_store \
        == "core"
    with pytest.raises(SystemExit):              # partagé sans clé du relais : refus au démarrage
        R.Settings.from_env({**env, "NOKKER_RELAY_JTI_STORE": "core"})
    with pytest.raises(SystemExit):
        R.Settings.from_env({**env, "NOKKER_RELAY_JTI_STORE": "redis"})
    assert R.Settings.from_env({**env, "NOKKER_PUBLIC_REPORT_URL": "https://x.example/r"}).report_url \
        == "https://x.example/r"


async def test_client_du_noyau_parle_au_registre(monkeypatch):
    """`CoreClient.relay_post` : clé du relais en en-tête, chemin du registre ; 0 si injoignable."""
    from aiohttp import web

    got = []

    async def jti(request):
        got.append((request.headers.get("X-Nokker-Relay-Key"), await request.json()))
        return web.json_response({"first": True})

    app = web.Application()
    app.router.add_post("/v1/gateway/pages-jti", jti)
    srv = TestServer(app)
    await srv.start_server()
    import aiohttp
    async with aiohttp.ClientSession() as http:
        core = R.CoreClient(R.Settings(cookie_secret=b"x" * 32, core_url=str(srv.make_url("")).rstrip("/"),
                                       core_key="k" * 32, jwks_url=""), http)
        st, data = await core.relay_post("/v1/gateway/pages-jti", {"jti": "j", "exp": 1})
        assert st == 200 and data == {"first": True} and got == [("k" * 32, {"jti": "j", "exp": 1})]
        await srv.close()
        st, _ = await core.relay_post("/v1/gateway/pages-jti", {"jti": "j", "exp": 1})
        assert st == 0
    await asyncio.sleep(0)


async def test_droit_de_publier_relu_avec_le_compte(penv, monkeypatch):
    """Le droit `pages_public` est relu avec le compte (toutes les 10 min) : retiré, les pages publiques tombent."""
    relay, c, _ = penv
    monkeypatch.setattr(R, "RECHECK_EVERY", 0)
    real_sleep = asyncio.sleep

    async def fast(_s):
        await real_sleep(0.01)
    monkeypatch.setattr(R.asyncio, "sleep", fast)
    ws, task = await open_tunnel(c, handler=public_handler)
    assert (await c.get(f"/{PUB}", headers=H(P))).status == 200
    MACHINES["tok-jean"] = R.Machine("m1", "jean", "t_jean", True, pages_public=False)
    try:
        for _ in range(50):
            await real_sleep(0.02)
            if not relay.reg.by_name["jean"].machine.pages_public:
                break
        assert (await c.get(f"/{PUB}", headers=H(P))).status == 404
    finally:
        MACHINES["tok-jean"] = R.Machine("m1", "jean", "t_jean", True, pages_public=True)
        task.cancel(); await ws.close()


def test_cle_du_relais_courte_avertie_et_ignoree(caplog):
    """bc5e04d gardé avec la fusion : une clé tronquée est signalée au démarrage, lue comme absente, et ne suffit pas
    au registre partagé."""
    env = {"NOKKER_RELAY_COOKIE_SECRET": "x" * 32, "NOKKER_RELAY_CORE_KEY": "court"}
    with caplog.at_level("WARNING", logger="relay"):
        assert R.Settings.from_env(env).core_key == ""
    assert "DÉSACTIVÉES" in caplog.text
    with pytest.raises(SystemExit):
        R.Settings.from_env({**env, "NOKKER_RELAY_JTI_STORE": "core"})


# ------------------------------------------------------------------ modération (§7.26, point 6)
async def test_page_publique_demandee_au_registre_avant_d_etre_servie(penv):
    relay, c, _ = penv
    check = relay.public_check
    ws, task = await open_tunnel(c, handler=public_handler)
    r = await c.get(f"/{PUB}", headers=H(P))
    assert r.status == 200 and "X-Nokker-Page-Id" not in r.headers      # en-tête de la machine, jamais au navigateur
    assert check.calls[-1] == {"host": "jean.nokker.app", "public_id": PUB, "page_id": "pg_0123456789abcdef",
                               "size": len(HTML)}
    check.answer = (410, {"error": {"code": "moderated", "message": "retirée par la modération"}})
    r = await c.get(f"/{PUB}", headers=H(P))
    assert r.status == 410 and "modération" in await r.text() and r.headers["Cache-Control"] == "no-store"
    check.answer = (404, {"error": {"message": "not found"}})
    assert (await c.get(f"/{PUB}", headers=H(P))).status == 404
    check.answer = (0, None)                                            # nokker.ai injoignable : rien n'est servi
    assert (await c.get(f"/{PUB}", headers=H(P))).status == 502
    task.cancel(); await ws.close()


async def test_sans_registre_aucune_page_publique_n_est_servie():
    """Sans clé du relais, le registre de modération ne peut pas être lu : échec fermé, la machine n'est pas vue."""
    relay = R.Relay(S, check_machine, check_token)
    c = TestClient(TestServer(R.make_app(relay)))
    await c.start_server()
    try:
        seen = []
        ws, task = await open_tunnel(c, seen=seen, handler=public_handler)
        assert (await c.get(f"/{PUB}", headers=H(P))).status == 404 and seen == []
        task.cancel(); await ws.close()
    finally:
        await c.close()


async def test_copie_publique_retiree_machine_eteinte(penv):
    relay, c, mirror = penv

    async def gone(body):
        mirror.calls.append(body)
        return 410, {"error": {"code": "moderated", "message": "retirée par la modération"}}

    relay.mirror_public = gone
    r = await c.get(f"/{PUB}", headers=H(P))
    assert r.status == 410 and "modération" in await r.text()
