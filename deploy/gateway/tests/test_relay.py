"""Relais nokker.app contre un faux Lokkal (client de tunnel minimal qui répond selon le chemin)."""
import asyncio
import base64
import json
import os
import sys

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay as R  # noqa: E402

MACHINES = {"tok-jean": R.Machine("m1", "jean", "t_jean", True),
            "tok-pauvre": R.Machine("m2", "pauvre", "t_pauvre", False)}
TOKENS = {"a.jean.sig": {"tenant": "t_jean"}, "a.autre.sig": {"tenant": "t_autre"},
          "a.jeanm2.sig": {"tenant": "t_jean", "machine": "m9"}}
_T = 2_000_000_000
TOKENS.update({
    "p.ok1.sig": {"tenant": "t_jean", "machine": "m1", "scope": "pages", "iat": _T, "exp": _T + 60, "jti": "j1"},
    "p.ok2.sig": {"tenant": "t_jean", "machine": "m1", "scope": "pages", "iat": _T, "exp": _T + 60, "jti": "j2"},
    "p.ok3.sig": {"tenant": "t_jean", "machine": "m1", "scope": "pages", "iat": _T, "exp": _T + 60, "jti": "j3"},
    "p.long.sig": {"tenant": "t_jean", "machine": "m1", "scope": "pages", "iat": _T, "exp": _T + 900, "jti": "j4"},
    "p.m9.sig": {"tenant": "t_jean", "machine": "m9", "scope": "pages", "iat": _T, "exp": _T + 60, "jti": "j5"},
    "p.nojti.sig": {"tenant": "t_jean", "machine": "m1", "scope": "pages", "iat": _T, "exp": _T + 60},
    "d.deleg.sig": {"tenant": "t_jean", "machine": "m1", "scope": "lokkal:session", "iat": _T, "exp": _T + 60,
                    "jti": "j6"},
    "d.deleg2.sig": {"tenant": "t_jean", "machine": "m1", "scope": "lokkal:session", "iat": _T, "exp": _T + 300},
})


async def check_machine(tok):
    return MACHINES.get(tok)


async def check_token(tok):
    return TOKENS.get(tok)


S = R.Settings(cookie_secret=b"x" * 32)


@pytest.fixture
async def env():
    relay = R.Relay(S, check_machine, check_token)
    client = TestClient(TestServer(R.make_app(relay)))
    await client.start_server()
    yield relay, client
    await client.close()


async def open_tunnel(client, token="tok-jean", seen=None, handler=None):
    """Faux noyau local : enregistre les requêtes reçues dans `seen`, répond par `handler(frame, ws)`."""
    ws = await client.ws_connect("/v1/tunnel", headers={"Host": "relay.nokker.app", "Authorization": f"Bearer {token}"},
                                 protocols=("nokker-tunnel.v1",))
    bodies = {}

    async def loop():
        async for msg in ws:
            f = json.loads(msg.data)
            if f["t"] == "ping":
                await ws.send_str('{"t":"pong"}')
                continue
            if f["t"] in ("req", "body"):
                bodies.setdefault(f["id"], [f if f["t"] == "req" else None, b""])
                if f["t"] == "req":
                    bodies[f["id"]][0] = f
                bodies[f["id"]][1] += base64.b64decode(f.get("body") or f.get("data") or "")
                if not f.get("more"):
                    req, body = bodies.pop(f["id"])
                    if seen is not None:
                        seen.append((req, body))
                    await (handler or default)(req, body, ws)
    task = asyncio.create_task(loop())
    await asyncio.sleep(0.05)
    return ws, task


async def default(req, body, ws):
    await ws.send_str(json.dumps({"t": "res", "id": req["id"], "status": 200,
                                  "headers": [["Content-Type", "text/plain"], ["Set-Cookie", "a=b"]],
                                  "body": base64.b64encode(b"ok " + req["path"].encode() + b" " +
                                                           str(len(body)).encode()).decode(), "more": False}))


def H(host="jean.nokker.app", **kw):
    return {"Host": host, **kw}


async def test_tunnel_refus(env):
    _, c = env
    r = await c.get("/v1/tunnel", headers=H("relay.nokker.app"))
    assert r.status == 401
    r = await c.get("/v1/tunnel", headers=H("relay.nokker.app", Authorization="Bearer faux"))
    assert r.status == 401
    r = await c.get("/v1/tunnel", headers=H("relay.nokker.app", Authorization="Bearer tok-pauvre"))
    assert r.status == 403


async def test_hors_ligne_et_hotes(env):
    _, c = env
    assert (await c.get("/", headers=H())).status == 502
    assert (await c.get("/", headers=H("evil.com"))).status == 404
    assert (await c.get("/", headers=H("a--b.nokker.app"))).status == 404
    assert (await c.get("/", headers=H("x.y.nokker.app"))).status == 404
    assert (await c.get("/healthz", headers=H("relay.nokker.app"))).status == 200


async def test_interface_et_cookies(env):
    _, c = env
    seen = []
    ws, task = await open_tunnel(c, seen=seen)
    r = await c.get("/index.html", headers=H(Cookie="secret=1", **{"X-Forwarded-For": "6.6.6.6", "CF-Connecting-IP": "1.2.3.4"}))
    assert r.status == 200 and (await r.text()).startswith("ok /index.html")
    assert "Set-Cookie" not in r.headers
    hdrs = {k.lower(): v for k, v in seen[0][0]["headers"]}
    assert "cookie" not in hdrs and hdrs["x-nokker-relay"] == "1" and hdrs["host"] == "jean.nokker.app"
    assert hdrs["x-forwarded-for"] == "1.2.3.4"
    assert (await c.post("/index.html", headers=H(), data=b"x")).status == 405
    task.cancel(); await ws.close()


async def test_api_filtree(env):
    _, c = env
    seen = []
    ws, task = await open_tunnel(c, seen=seen)
    assert (await c.get("/v1/threads", headers=H())).status == 401
    assert (await c.get("/v1/threads", headers=H(Authorization="Bearer jetonlocalsanspoints"))).status == 401
    assert (await c.get("/v1/threads", headers=H(Authorization="Bearer a.autre.sig"))).status == 401
    assert (await c.get("/v1/threads", headers=H(Authorization="Bearer a.jeanm2.sig"))).status == 401
    assert (await c.get("/v1/threads", headers=H(Authorization="Bearer a.jean.sig", **{"X-Nokker-Local": "1"}))).status == 401
    assert seen == []                     # rien n'est parti vers la machine
    r = await c.get("/v1/threads", headers=H(Authorization="Bearer a.jean.sig"))
    assert r.status == 200
    hdrs = {k.lower(): v for k, v in seen[0][0]["headers"]}
    assert hdrs["authorization"] == "Bearer a.jean.sig" and hdrs["x-nokker-account"] == "t_jean"
    task.cancel(); await ws.close()


async def test_gros_corps_en_morceaux(env):
    _, c = env
    seen = []
    ws, task = await open_tunnel(c, seen=seen)
    data = os.urandom(600 * 1024)
    r = await c.post("/v1/files", headers=H(Authorization="Bearer a.jean.sig"), data=data)
    assert r.status == 200 and (await r.text()).endswith(str(len(data)))
    assert seen[0][1] == data
    task.cancel(); await ws.close()


async def test_sse(env):
    _, c = env

    async def sse(req, body, ws):
        await ws.send_str(json.dumps({"t": "res", "id": req["id"], "status": 200,
                                      "headers": [["Content-Type", "text/event-stream"]], "body": "", "more": True}))
        for i in range(3):
            await asyncio.sleep(0.05)
            await ws.send_str(json.dumps({"t": "body", "id": req["id"], "more": i < 2,
                                          "data": base64.b64encode(f"data: {i}\n\n".encode()).decode()}))
    ws, task = await open_tunnel(c, handler=sse)
    r = await c.post("/v1/chat/completions", headers=H(Authorization="Bearer a.jean.sig"), data=b"{}")
    assert r.status == 200 and (await r.text()) == "data: 0\n\ndata: 1\n\ndata: 2\n\n"
    task.cancel(); await ws.close()


async def test_remplacement(env):
    relay, c = env
    ws1, t1 = await open_tunnel(c)
    ws2, t2 = await open_tunnel(c)
    await asyncio.sleep(0.1)
    assert ws1.closed and ws1.close_code == 4001
    assert relay.reg.by_name["jean"].ws is not None
    assert (await c.get("/", headers=H())).status == 200
    t1.cancel(); t2.cancel(); await ws2.close()


async def test_pages(env):
    _, c = env
    seen = []
    ws, task = await open_tunnel(c, seen=seen)
    P = "jean--pages.nokker.app"
    r = await c.get("/rapport.html", headers=H(P))
    assert r.status == 200 and "sandbox" in r.headers["Content-Security-Policy"]
    assert seen[-1][0]["path"] == "/v1/pages/rapport.html"
    assert "x-nokker-account" not in {k.lower() for k, _ in seen[-1][0]["headers"]}
    from yarl import URL
    for raw in ("/x/%2e%2e/y", "/x/../../y", "/.hidden/..", "/a%2f..%2fb"):
        r = await c.session.get(URL(str(c.make_url("")) + raw, encoded=True), headers=H(P))
        assert r.status in (404, 400) or seen[-1][0]["path"].count("..") == 0, raw
    assert (await c.get("/a//b", headers=H(P))).status == 404
    assert (await c.get("/_auth?token=a.autre.sig&to=/x", headers=H(P), allow_redirects=False)).status == 401
    r = await c.get("/_auth?token=a.jean.sig&to=//evil.com", headers=H(P), allow_redirects=False)
    assert r.status == 302 and r.headers["Location"] == "/"
    ck = r.cookies["__Host-nk_pg"]
    assert ck["secure"] and ck["httponly"] and not ck["domain"]
    r = await c.get("/rapport.html", headers=H(P, Cookie=f"__Host-nk_pg={ck.value}"))
    assert ("X-Nokker-Account", "t_jean") in [tuple(h) for h in seen[-1][0]["headers"]]
    # cookie de pages d'un autre nom : ignoré
    other = R.sign_cookie(S.cookie_secret, "t_jean", "autre", 2**31)
    await c.get("/rapport.html", headers=H(P, Cookie=f"__Host-nk_pg={other}"))
    assert "x-nokker-account" not in {k.lower() for k, _ in seen[-1][0]["headers"]}
    assert (await c.post("/rapport.html", headers=H(P))).status == 405
    task.cancel(); await ws.close()


async def test_machine_muette(env, monkeypatch):
    _, c = env
    monkeypatch.setattr(R, "RESPONSE_TIMEOUT", 0.3)

    async def muet(req, body, ws):
        pass
    ws, task = await open_tunnel(c, handler=muet)
    assert (await c.get("/", headers=H())).status == 504
    task.cancel(); await ws.close()


async def test_jetons_signes_jwks():
    """CoreClient.token : vraie signature ES256, JWKS servi en HTTP, iss/aud/exp vérifiés."""
    import time
    from aiohttp import web
    from cryptography.hazmat.primitives.asymmetric import ec
    import jwt as pyjwt

    key = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(pyjwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="k1", alg="ES256", use="sig")
    app = web.Application()
    app.router.add_get("/jwks.json", lambda r: web.json_response({"keys": [jwk]}))
    srv = TestServer(app)
    await srv.start_server()
    s = R.Settings(jwks_url=str(srv.make_url("/jwks.json")), cookie_secret=b"x" * 32)
    async with aiohttp.ClientSession() as http:
        core = R.CoreClient(s, http)
        now = int(time.time())
        ok = pyjwt.encode({"iss": s.issuer, "aud": "nokker.app", "exp": now + 60, "tenant": "t_jean"}, key,
                          algorithm="ES256", headers={"kid": "k1"})
        assert (await core.token(ok))["tenant"] == "t_jean"
        for bad in ({"iss": "https://evil", "aud": "nokker.app", "exp": now + 60},
                    {"iss": s.issuer, "aud": "autre", "exp": now + 60},
                    {"iss": s.issuer, "aud": "nokker.app", "exp": now - 120},
                    {"iss": s.issuer, "aud": "nokker.app"}):
            assert await core.token(pyjwt.encode(bad, key, algorithm="ES256", headers={"kid": "k1"})) is None
        other = ec.generate_private_key(ec.SECP256R1())
        forged = pyjwt.encode({"iss": s.issuer, "aud": "nokker.app", "exp": now + 60}, other, algorithm="ES256",
                              headers={"kid": "k1"})
        assert await core.token(forged) is None
        none_alg = pyjwt.encode({"iss": s.issuer, "aud": "nokker.app", "exp": now + 60}, None, algorithm="none",
                                headers={"kid": "k1"})
        assert await core.token(none_alg) is None
    await srv.close()


async def test_jetons_du_noyau_nokker_ai():
    """Jetons émis par le VRAI signataire de nokker.ai (nokker/cloud/link.py) et JWKS qu'il publie : le relais les
    accepte (accès, délégation de la bonne machine) et refuse la délégation pour une autre machine."""
    import time
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    link = pytest.importorskip("nokker.cloud.link")
    from aiohttp import web

    signer = link.Signer.generate()
    app = web.Application()
    app.router.add_get("/.well-known/jwks.json", lambda r: web.json_response(signer.jwks()))
    srv = TestServer(app)
    await srv.start_server()
    s = R.Settings(jwks_url=str(srv.make_url("/.well-known/jwks.json")), cookie_secret=b"x" * 32)
    async with aiohttp.ClientSession() as http:
        core = R.CoreClient(s, http)
        acc = await core.token(signer.access_token("us_jean", "t_jean"))
        assert acc and R.claims_tenant(acc) == "t_jean"
        dele = await core.token(signer.delegation_token("us_jean", "t_jean", "m1", "ws_1"))
        assert dele["machine"] == "m1" and dele["scope"] == "lokkal:session"
        assert await core.token(signer.access_token("us_jean", "t_jean", now=time.time() - 3600)) is None

        # par le relais : délégation pour m1 acceptée sur la machine m1, refusée si elle nomme une autre machine
        relay = R.Relay(s, check_machine, core.token)
        client = TestClient(TestServer(R.make_app(relay)))
        await client.start_server()
        ws, task = await open_tunnel(client)
        ok = signer.delegation_token("us_jean", "t_jean", "m1", "ws_1")
        bad = signer.delegation_token("us_jean", "t_jean", "m2", "ws_1")
        assert (await client.get("/v1/local/sessions", headers=H(Authorization=f"Bearer {ok}"))).status == 200
        assert (await client.get("/v1/local/sessions", headers=H(Authorization=f"Bearer {bad}"))).status == 401
        other = signer.access_token("us_x", "t_autre")
        assert (await client.get("/v1/local/sessions", headers=H(Authorization=f"Bearer {other}"))).status == 401
        task.cancel(); await ws.close(); await client.close()
    await srv.close()


async def test_pages_jeton_dedie_et_iframe(env):
    relay, c = env
    seen = []

    async def redirige(req, body, ws):
        hs = [["Content-Type", "text/html"]]
        if req["path"].startswith("/v1/pages/vieux"):
            hs.append(["Location", "/neuf"])
        await ws.send_str(json.dumps({"t": "res", "id": req["id"], "status": 302 if len(hs) > 1 else 200,
                                      "headers": hs, "body": "", "more": False}))
    ws, task = await open_tunnel(c, seen=seen, handler=redirige)
    P = "jean--pages.nokker.app"
    # jeton de pages : une seule fois ; ≤ 60 s ; cette machine ; jti exigé ; délégation refusée
    r = await c.get("/_auth?token=p.ok1.sig&to=/rapport.html", headers=H(P), allow_redirects=False)
    assert r.status == 302 and "__Host-nk_pg" in r.cookies
    assert (await c.get("/_auth?token=p.ok1.sig&to=/rapport.html", headers=H(P), allow_redirects=False)).status == 401
    for bad in ("p.long.sig", "p.m9.sig", "p.nojti.sig", "d.deleg.sig"):
        assert (await c.get(f"/_auth?token={bad}&to=/x", headers=H(P), allow_redirects=False)).status == 401, bad
    # jeton d'accès : toléré jusqu'au 2.11, refusé une fois l'interrupteur coupé
    relay.s = R.Settings(cookie_secret=b"x" * 32, pages_accept_access_token=False)
    assert (await c.get("/_auth?token=a.jean.sig&to=/x", headers=H(P), allow_redirects=False)).status == 401
    relay.s = S
    # iframe : capacité d'URL, pas de cookie ; le téléchargement garde sa requête
    r = await c.get("/_auth?token=p.ok2.sig&to=/rapport.html%3Fdl%3D1&embed=1", headers=H(P), allow_redirects=False)
    assert r.status == 302 and "__Host-nk_pg" not in r.cookies
    loc = r.headers["Location"]
    assert loc.startswith("/_s/") and loc.endswith("/rapport.html?dl=1")
    r = await c.get(loc, headers=H(P))
    assert r.status == 200
    assert seen[-1][0]["path"] == "/v1/pages/rapport.html?dl=1"
    assert ("X-Nokker-Account", "t_jean") in [tuple(h) for h in seen[-1][0]["headers"]]
    csp = r.headers["Content-Security-Policy"]
    assert "sandbox" in csp and "frame-ancestors https://jean.nokker.app https://nokker.ai https://app.nokker.ai" in csp
    # redirection relative du noyau : reste sous la capacité
    cap = loc.split("/")[2]
    r = await c.get(f"/_s/{cap}/vieux", headers=H(P), allow_redirects=False)
    assert r.headers["Location"] == f"/_s/{cap}/neuf"
    # capacité expirée, d'une autre machine, ou un cookie de pages présenté comme capacité : 401
    key = R.cap_key(S.cookie_secret)
    for bad in (R.sign_cookie(key, "t_jean", "jean", 1), R.sign_cookie(key, "t_jean", "autre", 2**31),
                R.sign_cookie(S.cookie_secret, "t_jean", "jean", 2**31), "n-importe-quoi"):
        r = await c.get(f"/_s/{bad}/rapport.html", headers=H(P))
        assert r.status == 401 and "nokker:pages-expired" in await r.text()
        assert "frame-ancestors" in r.headers["Content-Security-Policy"]
    # chemin de page invalide sous la capacité
    assert (await c.get(f"/_s/{cap}/a//b", headers=H(P))).status == 404
    # le cookie d'onglet ne vaut pas capacité : sans preuve, le noyau décide (pages publiques)
    r = await c.get("/rapport.html", headers=H(P))
    assert "frame-ancestors" in r.headers["Content-Security-Policy"]
    assert "x-nokker-account" not in {k.lower() for k, _ in seen[-1][0]["headers"]}
    task.cancel(); await ws.close()


class FakeCore:
    def __init__(self):
        self.calls, self.mode, self.ips = [], "ok", []
        self.n = 0

    async def __call__(self, path, body, client_ip=""):
        self.calls.append((path, body))
        self.ips.append(client_ip)
        if self.mode == "down":
            return 0, None
        if self.mode == "5xx":
            return 503, {"error": "x"}
        if path == "/v1/gateway/revoke":
            return 200, {}
        if self.mode == "refuse" or body.get("code") == "mauvais" or body.get("refresh_token") == "rt-mort":
            return 400, {"error": "invalid_grant"}
        self.n += 1
        return 200, {"access_token": f"at{self.n}", "expires_in": 900, "refresh_token": f"rt{self.n}"}


@pytest.fixture
async def gwenv():
    core = FakeCore()
    relay = R.Relay(S, check_machine, check_token, core)
    client = TestClient(TestServer(R.make_app(relay)))
    await client.start_server()
    yield core, client
    await client.close()


def G(**kw):
    return H("jean.nokker.app", Origin="https://jean.nokker.app", **kw)


async def test_gw_connexion(gwenv):
    core, c = gwenv
    # machine hors ligne : la connexion marche quand même (pas de tunnel)
    r = await c.post("/_gw/token", json={"code": "c1", "code_verifier": "v1"}, headers=G())
    assert r.status == 200 and r.headers["Cache-Control"] == "no-store"
    assert await r.json() == {"access_token": "at1", "expires_in": 900}
    assert core.calls[-1] == ("/v1/gateway/token", {"code": "c1", "code_verifier": "v1", "host": "jean.nokker.app"})
    ck = r.cookies["__Host-nk_rt"]
    assert ck.value == "rt1" and ck["secure"] and ck["httponly"] and ck["path"] == "/" and not ck["domain"]
    assert ck["samesite"] == "Strict"
    # refresh : rotation
    r = await c.post("/_gw/refresh", json={}, headers=G(Cookie="__Host-nk_rt=rt1"))
    assert r.status == 200 and (await r.json())["access_token"] == "at2" and r.cookies["__Host-nk_rt"].value == "rt2"
    assert core.calls[-1] == ("/v1/gateway/refresh", {"refresh_token": "rt1", "host": "jean.nokker.app"})
    # noyau en panne : 502, cookie GARDÉ
    for mode in ("down", "5xx"):
        core.mode = mode
        r = await c.post("/_gw/refresh", json={}, headers=G(Cookie="__Host-nk_rt=rt2"))
        assert r.status == 502 and "__Host-nk_rt" not in r.cookies, mode
    core.mode = "ok"
    # refus net : 401 et cookie effacé
    r = await c.post("/_gw/refresh", json={}, headers=G(Cookie="__Host-nk_rt=rt-mort"))
    assert r.status == 401 and r.cookies["__Host-nk_rt"].value == "" and r.cookies["__Host-nk_rt"]["secure"]
    assert (await c.post("/_gw/refresh", json={}, headers=G())).status == 401
    # code refusé : 401
    assert (await c.post("/_gw/token", json={"code": "mauvais", "code_verifier": "v"}, headers=G())).status == 401
    assert (await c.post("/_gw/token", json={"code": 1}, headers=G())).status == 400
    # logout : 204, révocation, cookie effacé ; sans cookie aussi
    r = await c.post("/_gw/logout", json={}, headers=G(Cookie="__Host-nk_rt=rt2"))
    assert r.status == 204 and r.cookies["__Host-nk_rt"].value == ""
    assert core.calls[-1] == ("/v1/gateway/revoke", {"refresh_token": "rt2"})
    n = len(core.calls)
    assert (await c.post("/_gw/logout", json={}, headers=G())).status == 204 and len(core.calls) == n


async def test_gw_garde_fous(gwenv):
    core, c = gwenv
    body = {"code": "c", "code_verifier": "v"}
    # CSRF : origine exacte et JSON exigés
    assert (await c.post("/_gw/token", json=body, headers=H())).status == 403
    assert (await c.post("/_gw/token", json=body, headers=H(Origin="https://evil.nokker.app"))).status == 403
    assert (await c.post("/_gw/token", json=body, headers=H(Origin="https://jean--pages.nokker.app"))).status == 403
    assert (await c.post("/_gw/token", data="code=c&code_verifier=v",
                         headers=G(**{"Content-Type": "application/x-www-form-urlencoded"}))).status == 403
    assert (await c.post("/_gw/token", data="x", headers=G(**{"Content-Type": "text/plain"}))).status == 403
    # méthode, route inconnue, hôte des pages : jamais au tunnel
    assert (await c.get("/_gw/token", headers=G())).status == 405
    assert (await c.post("/_gw/autre", json={}, headers=G())).status == 404
    assert (await c.post("/_gw/token", json=body, headers=H("jean--pages.nokker.app",
                                                             Origin="https://jean--pages.nokker.app"))).status in (405, 502)
    assert (await c.post("/_gw/token", data=b"{" + b" " * (9 << 10) + b"}",
                         headers=G(**{"Content-Type": "application/json"}))).status == 413
    assert core.calls == []
    # débit par IP
    codes = [(await c.post("/_gw/token", json=body, headers=G(**{"CF-Connecting-IP": "9.9.9.9"}))).status
             for _ in range(22)]
    assert codes.count(429) == 2 and codes[:20] == [200] * 20
    assert (await c.post("/_gw/token", json=body, headers=G(**{"CF-Connecting-IP": "8.8.8.8"}))).status == 200
    assert core.ips[-1] == "8.8.8.8"          # l'IP du navigateur suit jusqu'au noyau (limites par IP là-bas aussi)


async def test_gw_le_cookie_ne_passe_pas_au_lokkal(gwenv):
    _, c = gwenv
    seen = []
    ws, task = await open_tunnel(c, seen=seen)
    r = await c.get("/v1/threads", headers=H(Authorization="Bearer a.jean.sig", Cookie="__Host-nk_rt=rt1"))
    assert r.status == 200
    assert "cookie" not in {k.lower() for k, _ in seen[-1][0]["headers"]}
    task.cancel(); await ws.close()


async def test_corps_en_flux_et_plafond_sans_longueur(env, monkeypatch):
    _, c = env
    seen = []
    ws, task = await open_tunnel(c, seen=seen)
    big = bytes(range(256)) * (27 * 4096)        # ~27 Mo : 4 images de 5 Mo + l'enveloppe
    r = await c.post("/v1/threads/x/messages", data=big, headers=H(Authorization="Bearer a.jean.sig"))
    assert r.status == 200 and seen[-1][1] == big

    async def chunked():
        for _ in range(8):
            yield b"x" * 100_000
    monkeypatch.setattr(R, "MAX_BODY", 300_000)
    r = await c.post("/v1/threads/x/messages", data=chunked(), headers=H(Authorization="Bearer a.jean.sig"))
    assert r.status == 413
    task.cancel(); await ws.close()


async def test_presence_et_delai_de_delegation(monkeypatch):
    signals = []

    async def presence(tok, online):
        signals.append((tok, online))
    relay = R.Relay(S, check_machine, check_token, None, presence)
    c = TestClient(TestServer(R.make_app(relay)))
    await c.start_server()
    try:
        ws1, t1 = await open_tunnel(c)
        await asyncio.sleep(0.05)
        assert signals == [("tok-jean", True)]
        ws2, t2 = await open_tunnel(c)                 # remplace le premier : pas de « hors ligne » entre les deux
        await asyncio.sleep(0.1)
        assert signals == [("tok-jean", True), ("tok-jean", True)]
        t1.cancel()
        # délégation : délai long ; accès normal : 60 s (ici réduits)
        monkeypatch.setattr(R, "RESPONSE_TIMEOUT", 0.2)
        monkeypatch.setattr(R, "DELEGATION_RESPONSE_TIMEOUT", 1.0)
        await ws2.close(); t2.cancel()
        await asyncio.sleep(0.1)
        assert signals[-1] == ("tok-jean", False)

        async def lent(req, body, ws):
            await asyncio.sleep(0.5)
            await default(req, body, ws)
        ws3, t3 = await open_tunnel(c, handler=lent)
        assert (await c.post("/v1/chat/completions", headers=H(Authorization="Bearer a.jean.sig"))).status == 504
        assert (await c.post("/v1/chat/completions", headers=H(Authorization="Bearer d.deleg2.sig"))).status == 200
        t3.cancel(); await ws3.close()
    finally:
        await c.close()
