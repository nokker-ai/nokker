"""Cadre d'un aperçu d'écran servi par le noyau, sous SA propre CSP (le web peut alors poser une CSP stricte) :
URL signée de courte durée pour `<iframe src>` (qui ne pose jamais `Authorization`), propriétaire seulement, contenu
IDENTIQUE à celui que le web mettait en srcdoc, jamais `allow-same-origin`."""
import json
import urllib.parse

import pytest

from nokker.cast.service import FRAME_CSP, FRAME_URL_TTL_S
from tests.unit.cast.test_cast import _show, approve_screen, call, cast_id_of, ctx, world  # noqa: F401 — service complet (faux écrans, serveur LAN)
from tests.unit.test_public_pages import _http, lokkal  # noqa: F401 — vrai serveur Lokkal


def test_frame_url_owner_only_signed_short_lived_and_same_html(world):  # noqa: F811
    w = world
    cid = _show(w)
    with pytest.raises(LookupError):
        w.svc.frame_url(cid, "bob")  # pas le propriétaire : comme s'il n'existait pas
    with pytest.raises(LookupError):
        w.svc.frame_url("cast_000000000000", "alice")
    got = w.svc.frame_url(cid, "alice")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(got["url"]).query))
    assert got["url"].startswith(f"/v1/cast/{cid}/frame?") and int(q["exp"]) - w.svc.clock() <= FRAME_URL_TTL_S
    html = w.svc.frame_html(cid, exp=q["exp"], sig=q["sig"])
    assert html == w.svc.bubble_state(cid)["html"]  # exactement le srcdoc d'avant
    for exp, sig in ((q["exp"], "0" * 32), (str(int(q["exp"]) + 60), q["sig"]), ("", ""), ("x", q["sig"])):
        with pytest.raises(LookupError):
            w.svc.frame_html(cid, exp=exp, sig=sig)  # signature fausse, échéance allongée, vide
    now = w.svc.clock
    w.svc.clock = lambda: now() + FRAME_URL_TTL_S + 1
    try:
        with pytest.raises(LookupError):
            w.svc.frame_html(cid, exp=q["exp"], sig=q["sig"])  # échue
    finally:
        w.svc.clock = now
    assert w.svc.frame_html(cid, user="alice") == html
    with pytest.raises(LookupError):
        w.svc.frame_html(cid, user="bob")
    assert "/cast/" not in json.dumps(w.svc.bubble_state(cid))  # la bulle ne porte toujours aucun lien


def test_frame_route_headers_rights_and_content(lokkal, world):  # noqa: F811
    port, api, _, _ = lokkal
    w = world
    api.cast = w.svc
    # un aperçu demandé sur la machine (tour sans expéditeur : le propriétaire local)
    text, _ = call(w.svc, {"screen": "Salon", "kind": "table", "rows": [["1"]]}, ctx(sender="local-owner"))
    if "approb" in text or "Approuv" in text:
        approve_screen(w, text, user="local-owner")
    cid = cast_id_of(text)
    st, _, raw = _http(port, "POST", f"/v1/cast/{cid}/frame-url")
    assert st == 401  # sans jeton local
    st, _, raw = _http(port, "POST", f"/v1/cast/{cid}/frame-url", "key-alice")
    assert st == 401  # un autre jeton que celui de la machine
    st, _, raw = _http(port, "POST", f"/v1/cast/{cid}/frame-url", api.token)
    url = json.loads(raw)["url"]
    st, headers, body = _http(port, "GET", url)  # comme une <iframe src> : aucun en-tête d'authentification
    assert st == 200 and body.decode() == w.svc.bubble_state(cid)["html"]
    assert headers["Content-Security-Policy"] == FRAME_CSP and "allow-same-origin" not in FRAME_CSP
    assert FRAME_CSP.startswith("sandbox allow-scripts;") and "default-src 'none'" in FRAME_CSP
    assert headers["X-Content-Type-Options"] == "nosniff" and headers["Cache-Control"] == "no-store"
    assert headers["Content-Type"].startswith("text/html")
    assert _http(port, "GET", url.replace("sig=", "sig=0"))[0] == 404
    assert _http(port, "GET", f"/v1/cast/{cid}/frame")[0] == 404  # ni signature ni jeton
    assert _http(port, "GET", f"/v1/cast/{cid}/frame", api.token)[0] == 200  # le propriétaire, par jeton
    assert _http(port, "GET", "/v1/cast/cast_000000000000/frame", api.token)[0] == 404


def test_frame_url_through_the_relay_for_the_linked_owner_only(lokkal, world, tmp_path):  # noqa: F811
    import urllib.error
    import urllib.request

    from nokker.cloud.link import Signer
    from nokker.local.setup import LocalSetup, LocalVault

    port, api, _, _ = lokkal
    w = world
    api.cast = w.svc
    text, _ = call(w.svc, {"screen": "Salon", "kind": "table", "rows": [["1"]]}, ctx(sender="local-owner"))
    if "approb" in text or "Approuv" in text:
        approve_screen(w, text, user="local-owner")
    cid = cast_id_of(text)
    signer = Signer.generate()
    api.setup = LocalSetup(state_path=tmp_path / "setup-r.json", vault=LocalVault(tmp_path / "vr.bin"), detect=lambda: [])
    api.setup.state.update(jwks=signer.jwks(), account="us_pierre", machine_id="mc_1")

    class Up:
        state = "up"

    api.relay_host, api.tunnel = "pierre.nokker.app", Up()
    api.link = None  # émetteur par défaut (celui du signataire de test), comme tests/unit/test_local_api.py

    def relayed(method, path, token=None):
        h = {"X-Nokker-Relay": "1", **({"Authorization": f"Bearer {token}"} if token else {})}
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h,
                                     data=b"{}" if method == "POST" else None)
        req.add_unredirected_header("Host", "pierre.nokker.app")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    good = signer.access_token("us_pierre", "tn_pierre")
    st, _, raw = relayed("POST", f"/v1/cast/{cid}/frame-url", good)
    assert st == 200
    url = json.loads(raw)["url"]
    st, headers, body = relayed("GET", url)  # le relais laisse passer le GET sans jeton : la signature suffit
    assert st == 200 and headers["Content-Security-Policy"] == FRAME_CSP
    assert body.decode() == w.svc.bubble_state(cid)["html"]
    assert relayed("POST", f"/v1/cast/{cid}/frame-url", signer.access_token("us_autre", "tn_autre"))[0] == 401
    assert relayed("POST", f"/v1/cast/{cid}/frame-url", Signer.generate().access_token("us_pierre", "tn_pierre"))[0] == 401
    deleg = signer.delegation_token("us_pierre", "tn_pierre", machine="mc_1", web_session="w")
    # délégation (X21) et jeton local : refusés par le filtre général de la passerelle, avant la route (403)
    assert relayed("POST", f"/v1/cast/{cid}/frame-url", deleg)[0] == 403
    assert relayed("POST", f"/v1/cast/{cid}/frame-url", api.token)[0] == 403
    assert relayed("GET", f"/v1/cast/{cid}/frame")[0] == 404  # sans signature, rien
