"""CORS de `<nom>.nokker.app/v1/*` (docs/LOKKAL-PHONE.md §1.2 point 1) : origines exactes (app.nokker.ai, app native
iOS et Android), jamais `Allow-Credentials`, pré-vérification sans tunnel, rien sur `/_gw`, les pages ni le relais ; les
en-têtes CORS venus de la machine sont retirés."""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay as R  # noqa: E402
from test_relay import H, env, open_tunnel  # noqa: E402,F401 — fixture

APP, IOS, ANDROID = "https://app.nokker.ai", "capacitor://localhost", "https://localhost"


async def cors_from_machine(req, body, ws):
    """Faux Lokkal qui pose ses propres en-têtes CORS (il ne doit pas : le relais les retire)."""
    hs = [["Content-Type", "application/json"], ["Access-Control-Allow-Origin", "*"],
          ["Access-Control-Allow-Credentials", "true"], ["Vary", "Accept-Encoding"]]
    await ws.send_str(json.dumps({"t": "res", "id": req["id"], "status": 200, "headers": hs,
                                  "body": base64.b64encode(b'{"results":[]}').decode(), "more": False}))


def preflight(origin, method="GET", headers="authorization", host="jean.nokker.app"):
    return H(host, Origin=origin, **{"Access-Control-Request-Method": method,
                                     "Access-Control-Request-Headers": headers})


async def test_preflight_exact_origins_without_tunnel_or_token(env):  # noqa: F811
    _, c = env
    for origin in (APP, IOS, ANDROID):  # machine hors ligne : la pré-vérification ne passe pas par le tunnel
        r = await c.options("/v1/threads/search", headers=preflight(origin, headers="authorization, x-session-id, x-nokker-surface"))
        assert r.status == 204, origin
        assert r.headers["Access-Control-Allow-Origin"] == origin and r.headers["Vary"] == "Origin"
        assert "Access-Control-Allow-Credentials" not in r.headers
        assert r.headers["Access-Control-Allow-Headers"] == (
            "Authorization, Content-Type, X-Session-Id, X-Nokker-Stream-Tokens, Last-Event-ID, User-Agent, X-Nokker-Surface")
        assert r.headers["Access-Control-Max-Age"] == "600"
        assert r.headers["Access-Control-Allow-Methods"] == "GET, POST, PUT, PATCH, DELETE"
    for bad in ("https://evil.example", "null", "https://app.nokker.ai.evil.example", "http://localhost",
                "capacitor://localhost.evil", ""):
        r = await c.options("/v1/threads", headers=preflight(bad))
        assert r.status != 204 and "Access-Control-Allow-Origin" not in r.headers, bad  # "" : pas une pré-vérification
    r = await c.options("/v1/threads", headers=preflight(IOS, headers="authorization, x-evil"))
    assert r.status == 403 and "Access-Control-Allow-Origin" not in r.headers and r.headers["Vary"] == "Origin"
    r = await c.options("/v1/threads", headers=preflight(IOS, method="CONNECT"))
    assert r.status == 403
    # ni /_gw (cookie de renouvellement), ni les pages, ni le relais
    for path, host in (("/_gw/token", "jean.nokker.app"), ("/v1/x", "jean--pages.nokker.app"),
                       ("/v1/tunnel", "relay.nokker.app"), ("/index.html", "jean.nokker.app")):
        r = await c.options(path, headers=preflight(IOS, host=host))
        assert "Access-Control-Allow-Origin" not in r.headers, (path, host)
    # un OPTIONS qui n'est pas une pré-vérification suit le chemin ordinaire (ici : machine hors ligne, 502)
    r = await c.options("/v1/threads", headers=H(Origin=IOS))
    assert r.status == 502


async def test_responses_carry_cors_for_listed_origins_and_drop_the_machines_own(env):  # noqa: F811
    _, c = env
    seen = []
    ws, task = await open_tunnel(c, seen=seen, handler=cors_from_machine)
    r = await c.get("/v1/threads/search?q=site", headers=H(Origin=IOS, Authorization="Bearer a.jean.sig"))
    assert r.status == 200 and r.headers["Access-Control-Allow-Origin"] == IOS
    assert "Access-Control-Allow-Credentials" not in r.headers  # celui de la machine est retiré
    assert r.headers.getall("Access-Control-Allow-Origin") == [IOS]  # un seul, jamais « * »
    assert "Origin" in r.headers["Vary"] and "Accept-Encoding" in r.headers["Vary"]
    assert r.headers["Access-Control-Expose-Headers"] == "Retry-After"
    r = await c.get("/v1/threads", headers=H(Origin="https://evil.example", Authorization="Bearer a.jean.sig"))
    assert r.status == 200 and not any(k.lower().startswith("access-control-") for k in r.headers)
    assert "Origin" in r.headers["Vary"]  # relecture navigateur : Vary sur toute réponse /v1 (cache)
    r = await c.get("/v1/threads", headers=H(Authorization="Bearer a.jean.sig"))  # sans Origin : aucun CORS
    assert not any(k.lower().startswith("access-control-") for k in r.headers)
    assert r.headers.getall("Vary") == ["Accept-Encoding, Origin"]  # complété, jamais en double
    # un refus du relais est lisible par l'app (elle renouvelle son jeton sur 401)
    r = await c.get("/v1/threads", headers=H(Origin=APP))
    assert r.status == 401 and r.headers["Access-Control-Allow-Origin"] == APP
    r = await c.get("/index.html", headers=H(Origin=APP))  # interface, hors /v1 : rien, pas même Vary
    assert "Access-Control-Allow-Origin" not in r.headers and "Origin" not in r.headers.get("Vary", "")
    task.cancel()
    await ws.close()
    r = await c.get("/v1/threads", headers=H(Origin=ANDROID, Authorization="Bearer a.jean.sig"))
    assert r.status == 502 and r.headers["Access-Control-Allow-Origin"] == ANDROID  # « hors ligne », lisible


async def test_surface_header_reaches_the_machine_through_the_relay(env):  # noqa: F811
    _, c = env
    seen = []
    ws, task = await open_tunnel(c, seen=seen, handler=cors_from_machine)
    r = await c.post("/v1/chat/completions", json={"messages": []},
                     headers=H(Origin=APP, Authorization="Bearer a.jean.sig", **{"X-Nokker-Surface": "pwa"}))
    assert r.status == 200
    assert {k.lower(): v for k, v in seen[0][0]["headers"]}["x-nokker-surface"] == "pwa"   # transmis tel quel (STRIP_REQ ne le retire pas)
    task.cancel()
    await ws.close()


def test_cors_origin_list_is_exact():
    assert R.CORS_ORIGINS == frozenset({APP, IOS, ANDROID})
