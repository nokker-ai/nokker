"""Routes de nokker.ai pour Lokkal (nokker/cloud/http.py) sur le vrai `make_handler` : flux d'appareil complet, machine
vue par le relais, jeton d'accès, JWKS, inférence relayée (JSON et SSE) et comptée au locataire, révocation."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nokker.api.server import make_handler
from nokker.cloud import http as cloud_http
from nokker.cloud.http import CloudLink
from nokker.cloud.link import LinkService, Signer, verify_token
from tests.unit.api.test_server import make_kernel


class FakeRouter(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        FakeRouter.seen.append((self.headers.get("Authorization"), body.get("model")))
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"bon"}}]}\n\n')
            self.wfile.write(b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")
            return
        out = json.dumps({"choices": [{"message": {"content": "ok"}}],
                          "usage": {"prompt_tokens": 10, "completion_tokens": 3}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def cloud():
    router = ThreadingHTTPServer(("127.0.0.1", 0), FakeRouter)
    threading.Thread(target=router.serve_forever, daemon=True).start()
    usage = []
    signer = Signer.generate()
    cloud_http.configure(CloudLink(
        links=LinkService(), signer=signer,
        identity=lambda h: "us_pierre" if h.headers.get("Authorization") == "Bearer sess-pierre" else None,
        tenant_of=lambda acc: "tn_pierre" if acc == "us_pierre" else None,
        router_base=f"http://127.0.0.1:{router.server_address[1]}/v1", router_key=lambda: "rk-nokker",
        record_usage=lambda *a: usage.append(a)))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], signer, usage
    cloud_http.configure(None)
    httpd.shutdown()
    router.shutdown()


def _call(port, path, body=None, token=None, raw=False):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
            return r.status, (data if raw else json.loads(data or b"{}"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_link_gateway_and_inference_end_to_end(cloud):
    port, signer, usage = cloud
    status, dev = _call(port, "/v1/link/device", {"machine": "atelier"})
    assert status == 200 and dev["user_code"]
    assert _call(port, "/v1/link/token", {"device_code": dev["device_code"]})[1] == {"state": "pending"}
    assert _call(port, "/v1/link/approve", {"user_code": dev["user_code"]})[0] == 401  # sans session du compte
    status, ok = _call(port, "/v1/link/approve", {"user_code": dev["user_code"]}, token="sess-pierre")
    assert status == 200 and ok["host"] == "atelier.nokker.app"
    tok = _call(port, "/v1/link/token", {"device_code": dev["device_code"]})[1]
    mt = tok["machine_token"]
    assert tok["state"] == "approved" and tok["relay_host"] == "atelier.nokker.app"

    status, m = _call(port, "/v1/gateway/machine", token=mt)
    assert status == 200 and m["tenant"] == "tn_pierre" and m["inference"] is True
    assert _call(port, "/v1/gateway/machine", token="faux")[0] == 401
    jwks = _call(port, "/v1/.well-known/jwks.json")[1]
    acc = _call(port, "/v1/gateway/token", {}, token="sess-pierre")[1]["access_token"]
    assert verify_token(acc, jwks)["tenant"] == "tn_pierre"

    status, out = _call(port, "/v1/inference/chat/completions", {"model": "m", "messages": []}, token=mt)
    assert status == 200 and out["choices"][0]["message"]["content"] == "ok"
    assert FakeRouter.seen[-1] == ("Bearer rk-nokker", "m")  # la clé du Router est la nôtre, jamais celle du client
    status, sse = _call(port, "/v1/inference/chat/completions", {"model": "m", "messages": [], "stream": True},
                        token=mt, raw=True)
    assert status == 200 and b"bon" in sse and b"[DONE]" in sse
    assert [(u[0], u[2]) for u in usage] == [("tn_pierre", {"prompt_tokens": 10, "completion_tokens": 3}),
                                             ("tn_pierre", {"prompt_tokens": 7, "completion_tokens": 2})]

    assert _call(port, "/v1/link/revoke", {}, token=mt)[0] == 200
    assert _call(port, "/v1/inference/chat/completions", {"messages": []}, token=mt)[0] == 401


def test_pending_shows_the_machine_before_approval(cloud):
    port, _, _ = cloud
    dev = _call(port, "/v1/link/device", {"machine": "Portable de Jean"})[1]
    assert _call(port, f"/v1/link/pending?code={dev['user_code']}")[0] == 401
    status, body = _call(port, f"/v1/link/pending?code={dev['user_code'].lower()}", token="sess-pierre")
    assert status == 200 and body["machine_name"] == "Portable de Jean" and 0 < body["expires_in"] <= 600
    assert _call(port, "/v1/link/pending?code=ZZZZ-ZZZZ", token="sess-pierre")[0] == 404
    assert _call(port, "/v1/link/token", {"device_code": dev["device_code"]})[1] == {"state": "pending"}  # sans effet


def test_lokkal_session_tool_goes_through_the_gateway_with_a_delegation():
    import httpx

    from nokker.cloud.link import LinkService, Signer, verify_token
    from nokker.cloud.lokkal_session import LokkalSessionTools
    from nokker.core.ports import ToolContext

    svc, signer = LinkService(), Signer.generate()
    d = svc.device("atelier")
    svc.approve(d["user_code"], "us_pierre", "tn_pierre")
    svc.token(d["device_code"])
    seen = []

    def gateway(req: httpx.Request) -> httpx.Response:
        claims = verify_token(req.headers["Authorization"][7:], signer.jwks(), machine=svc.machines_of("us_pierre")[0].id)
        seen.append((req.url.host, req.url.path, claims["scope"], req.headers.get("X-Session-Id")))
        if req.url.path == "/v1/local/sessions":
            return httpx.Response(201, json={"thread_id": "th_l1", "cwd": "/home/p/app"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "tests verts"}}]})

    tools = LokkalSessionTools(links=svc, signer=signer, client=httpx.Client(transport=httpx.MockTransport(gateway)))
    assert tools.specs()[0].effect.value == "destructive"
    r = tools.call("lokkal_session", {"cwd": "/home/p/app", "task": "corrige"},
                   ToolContext(thread_id="th_web", turn_index=0, channel="api", sender="us_pierre"))
    assert not r.is_error and json.loads(r.content)["reply"] == "tests verts"
    assert seen == [("atelier.nokker.app", "/v1/local/sessions", "lokkal:session", None),
                    ("atelier.nokker.app", "/v1/chat/completions", "lokkal:session", "th_l1")]
    other = tools.call("lokkal_session", {"cwd": "/x", "task": "t"},
                       ToolContext(thread_id="t", turn_index=0, channel="api", sender="us_autre"))
    assert other.is_error and "aucune machine" in other.content


def _turn(thread, index, text):
    from nokker.core.turn import InputCheck, Message, Turn

    return Turn(thread_id=thread, index=index, input=InputCheck(text=text), channel="api", sender="us_pierre",
                reply=f"réponse à {text}", messages=[Message("assistant", f"réponse à {text}")])


def test_mirror_push_and_pull_between_machine_and_web():
    from nokker.local.mirror import MirrorWorker
    from nokker.store.pg import MemoryStore
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    cloud_store = MemoryStore()
    router = ThreadingHTTPServer(("127.0.0.1", 0), FakeRouter)
    links = LinkService()
    cloud_http.configure(CloudLink(
        links=links, signer=Signer.generate(), identity=lambda h: "us_pierre", tenant_of=lambda a: "tn_pierre",
        router_base="http://127.0.0.1:1", router_key=lambda: "x", store=cloud_store,
        mirror_allowed=lambda acc: acc == "us_pierre"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        dev = _call(port, "/v1/link/device", {"machine": "atelier"})[1]
        _call(port, "/v1/link/approve", {"user_code": dev["user_code"]}, token="s")
        mt = _call(port, "/v1/link/token", {"device_code": dev["device_code"]})[1]["machine_token"]
        local, state, saves = MemoryStore(), {}, []
        worker = MirrorWorker(store=local, base_url=f"http://127.0.0.1:{port}", token=lambda: mt, state=state,
                              save=lambda: saves.append(dict(state)))
        with tenant_scope(INTERNAL_TENANT):
            local.append(_turn("th_local", 0, "corrige le test"))
        assert worker.push_once() == 1 and state["mirror_push_cursor"] and saves
        mid = links.machines_of("us_pierre")[0].id
        copies = [t for t, turns in cloud_store._by_thread.items() if f"lokkal-{mid}-th_local" in t]
        assert len(copies) == 1 and cloud_store._by_thread[copies[0]][0].calls == []
        assert worker.push_once() == 0  # rien de neuf après le curseur
        state.pop("mirror_push_cursor")  # redémarrage sans curseur (état perdu) : tout repart, le web saute les doublons
        assert worker.push_once() == 1 and len(cloud_store._by_thread[copies[0]]) == 1
        cloud_store.append(_turn("web1", 0, "planifie la semaine"))
        assert worker.pull_once() == 1  # la copie lokkal-* n'est jamais retirée
        assert local._by_thread["mirror-web-web1"][0].input.text == "planifie la semaine"
        assert worker.pull_once() == 0 and state["mirror_cursor"]
        assert worker.push_once() == 0  # une copie web n'est jamais renvoyée (le curseur la saute)
        # Effacement propagé (§6.3) : le propriétaire efface les copies de SA machine chez nokker.ai ; l'envoi se met en
        # pause, une reprise renvoie tout ; la session web, elle, reste.
        assert worker.erase_remote() == 1 and state["mirror_paused"]
        assert not [t for t in cloud_store._by_thread if f"lokkal-{mid}-" in t] and cloud_store._by_thread["web1"]
        assert worker.push_once() == 0
        worker.resume()
        assert worker.push_once() == 1 and [t for t in cloud_store._by_thread if f"lokkal-{mid}-" in t]
    finally:
        cloud_http.configure(None)
        httpd.shutdown()
        router.server_close()


def test_machine_refused_three_times_erases_its_web_copies():
    """Compte effacé ou machine déliée chez nokker.ai : 401 au tirage. Un refus isolé n'efface rien ; au troisième
    d'affilée, les copies des fils web gardées sur la machine partent (et seulement elles)."""
    import httpx

    from nokker.local.mirror import MirrorWorker
    from nokker.store.pg import MemoryStore
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    class Refuse:
        def get(self, url, headers=None, params=None):
            return httpx.Response(401, request=httpx.Request("GET", url), json={"error": {"message": "machine"}})

    local = MemoryStore()
    with tenant_scope(INTERNAL_TENANT):
        local.append(_turn("mirror-web-web1", 0, "copie du web"))
        local.append(_turn("th_local", 0, "fil de la machine"))
    state = {"mirror_cursor": "c"}
    w = MirrorWorker(store=local, base_url="http://cloud.invalid", token=lambda: "mt", state=state, save=lambda: None,
                     client=Refuse())
    assert w.pull_once() == 0 and w.pull_once() == 0 and "mirror-web-web1" in local._by_thread
    w.pull_once()
    assert "mirror-web-web1" not in local._by_thread and local._by_thread["th_local"]
    assert "mirror_cursor" not in state


def test_mirror_erase_refuses_anything_but_mirror_copies():
    import pytest

    from nokker.store.pg import MemoryStore

    store = MemoryStore()
    store.append(_turn("th_local", 0, "maison"))
    for bad in ("th_", "", "lokkal-", "mirror-", "tn_x:th_"):
        with pytest.raises(ValueError):
            store.erase_mirror_copies(bad)
    assert store.erase_mirror_copies("mirror-web-") == 0 and store._by_thread["th_local"]


def _linked(port, name="atelier"):
    dev = _call(port, "/v1/link/device", {"machine": name})[1]
    _call(port, "/v1/link/approve", {"user_code": dev["user_code"]}, token="sess-pierre")
    return _call(port, "/v1/link/token", {"device_code": dev["device_code"]})[1]


def test_my_machines_list_seen_revoke_and_pages_token(cloud):
    """Écran « mes machines » (onboarding) et jeton de pages (docs/BUBBLES.md §10.3)."""
    port, signer, _ = cloud
    tok = _linked(port)
    mid = tok["machine_id"]
    assert _call(port, "/v1/link/machines")[0] == 401
    status, body = _call(port, "/v1/link/machines", token="sess-pierre")
    assert status == 200 and [m["machine_id"] for m in body["machines"]] == [mid]
    assert body["machines"][0]["last_seen_at"] is None
    _call(port, "/v1/gateway/machine", token=tok["machine_token"])  # contrôle du relais
    assert _call(port, "/v1/link/machines", token="sess-pierre")[1]["machines"][0]["last_seen_at"] > 0

    page = "pg_0123456789abcdef"
    status, link = _call(port, f"/v1/link/machines/{mid}/pages-token", {"page_id": page, "embed": True},
                         token="sess-pierre")
    assert status == 200 and link["url"].startswith("https://atelier--pages.nokker.app/_auth?token=")
    assert link["url"].endswith(f"&to=/{page}&embed=1")
    from urllib.parse import parse_qs, urlsplit

    claims = verify_token(parse_qs(urlsplit(link["url"]).query)["token"][0], _call(port, "/v1/.well-known/jwks.json")[1])
    assert claims["scope"] == "pages" and claims["machine"] == mid and claims["exp"] - claims["iat"] <= 60
    assert not _call(port, f"/v1/link/machines/{mid}/pages-token", {"page_id": page}, token="sess-pierre")[1][
        "url"].endswith("embed=1")
    assert _call(port, f"/v1/link/machines/{mid}/pages-token", {"page_id": "../x"}, token="sess-pierre")[0] == 404
    other = "mc_" + "0" * 16
    assert _call(port, f"/v1/link/machines/{other}/revoke", {}, token="sess-pierre")[0] == 404  # pas la sienne
    assert _call(port, f"/v1/link/machines/{mid}/revoke", {}, token="sess-pierre") == (200, {"revoked": True})
    assert _call(port, "/v1/link/machines", token="sess-pierre")[1]["machines"] == []
    assert _call(port, "/v1/gateway/machine", token=tok["machine_token"])[0] == 401


def test_machine_load_sent_by_the_machine_read_in_my_machines(cloud):
    """Lot X5 (docs/LOKKAL.md §7.2) : la machine pousse sa charge avec son jeton ; « mes machines » rend la dernière
    et son âge ; rien sans jeton, rien d'illisible, rien de trop gros, plus rien après révocation."""
    port, _, _ = cloud
    tok = _linked(port, "calcul")
    mt, mid = tok["machine_token"], tok["machine_id"]
    machines = _call(port, "/v1/link/machines", token="sess-pierre")[1]["machines"]
    assert [(m["load"], m["load_at"], m["load_age_s"]) for m in machines] == [(None, None, None)]
    load = {"v": 1, "version": "0.1.0", "ram": {"total_mb": 32768, "free_mb": 20000, "source": "host"},
            "cpu": {"count": 16, "load1": 2.5, "source": "host"}, "sessions": 2, "active_turns": 1,
            "gpus": [{"name": "NVIDIA GeForce RTX 3090", "memory_total_mb": 24576, "memory_free_mb": 24000,
                      "util_pct": 0}], "pirate": "<script>"}
    assert _call(port, "/v1/gateway/machine/load", load)[0] == 401
    assert _call(port, "/v1/gateway/machine/load", load, token="faux")[0] == 401
    status, ack = _call(port, "/v1/gateway/machine/load", load, token=mt)
    assert status == 200 and ack["stored"] is True and ack["at"] > 0
    assert _call(port, "/v1/gateway/machine/load", {"rien": 1}, token=mt)[0] == 400
    assert _call(port, "/v1/gateway/machine/load", {"ram": {}, "x": "y" * 20000}, token=mt)[0] == 413
    [m] = _call(port, "/v1/link/machines", token="sess-pierre")[1]["machines"]
    assert m["machine_id"] == mid and m["load_age_s"] >= 0 and m["load_at"] > 0
    assert "pirate" not in m["load"] and m["load"]["ram"] == load["ram"] and m["load"]["gpus"] == load["gpus"]
    assert m["load"]["sessions"] == 2 and m["load"]["active_turns"] == 1 and m["last_seen_at"] is None  # pas le relais
    _call(port, f"/v1/link/machines/{mid}/revoke", {}, token="sess-pierre")
    assert _call(port, "/v1/gateway/machine/load", load, token=mt)[0] == 401


def test_local_reporter_reaches_the_cloud_route(cloud, tmp_path):
    """Le vrai `LoadReporter` contre la vraie route : la mesure de la machine passe le nettoyage telle quelle."""
    import httpx

    from nokker.local.load import LoadReporter, measure

    port, _, _ = cloud
    tok = _linked(port, "poste")
    rep = LoadReporter(base_url=f"http://127.0.0.1:{port}", token=lambda: tok["machine_token"],
                       measure=lambda: measure(version="0.1.0", sessions=1), client=httpx.Client(timeout=10))
    assert rep.send_once() is True
    [m] = [x for x in _call(port, "/v1/link/machines", token="sess-pierre")[1]["machines"]
           if x["machine_id"] == tok["machine_id"]]
    assert m["load"]["version"] == "0.1.0" and m["load"]["sessions"] == 1 and m["load"]["ram"]["total_mb"] > 0


def test_machine_asks_for_its_own_page_link(cloud):
    """Interface servie par la passerelle : le Lokkal demande le lien avec son jeton de machine (pas d'id à connaître,
    pas de session app.nokker.ai dans le navigateur)."""
    port, _, _ = cloud
    tok = _linked(port, "bureau")
    page = "pg_fedcba9876543210"
    assert _call(port, "/v1/gateway/pages-token", {"page_id": page})[0] == 401
    status, link = _call(port, "/v1/gateway/pages-token", {"page_id": page, "embed": True}, token=tok["machine_token"])
    assert status == 200 and link["url"].startswith("https://bureau--pages.nokker.app/_auth?token=")
    assert link["url"].endswith(f"to=/{page}&embed=1")
    assert _call(port, "/v1/gateway/pages-token", {"page_id": "x"}, token=tok["machine_token"])[0] == 404
    dl = _call(port, "/v1/gateway/pages-token", {"page_id": page, "download": True}, token=tok["machine_token"])[1]
    assert dl["url"].endswith(f"to=/{page}%3Fdl%3D1")  # le relais redirige vers /<page>?dl=1


def test_local_link_client_posts_with_the_machine_token(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from nokker.local.setup import LocalLink, LocalSetup, LocalVault

    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", Fernet.generate_key().decode())

    class Resp:
        status_code = 200

        def json(self):
            return {"url": "https://x--pages.nokker.app/_auth?token=t&to=/pg_0", "expires_at": 1}

    class Client:
        def __init__(self):
            self.seen = []

        def post(self, url, json=None, headers=None):
            self.seen.append((url, json, headers))
            return Resp()

    setup = LocalSetup(state_path=tmp_path / "setup.json", vault=LocalVault(tmp_path / "vault.bin"))
    client = Client()
    link = LocalLink(setup=setup, base_url="https://app.nokker.ai", client=client)
    assert link.pages_token("pg_0") is None  # pas lié : rien demandé
    setup.vault.put("machine_token", "mt-secret")
    assert link.pages_token("pg_0", embed=True)["url"].startswith("https://x--pages")
    url, body, headers = client.seen[-1]
    assert url == "https://app.nokker.ai/v1/gateway/pages-token" and body == {"page_id": "pg_0", "embed": True, "download": False}
    assert headers == {"Authorization": "Bearer mt-secret"}


def test_gateway_login_code_pkce_refresh_rotation_and_reuse(cloud):
    """Connexion par la passerelle (§7.3) : code 60 s à usage unique lié au challenge, jeton d'accès lié à la machine,
    rotation du renouvellement, réutilisation = famille révoquée, machine déliée = plus de renouvellement."""
    import base64
    import hashlib

    from nokker.cloud.gateway_auth import GatewayAuth

    port, signer, _ = cloud
    cloud_http._cloud.gateway_auth = GatewayAuth(links=cloud_http._cloud.links, signer=signer)
    tok = _linked(port, "salon")
    host = "salon.nokker.app"
    verifier = "v" * 50
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    auth = "/v1/gateway/authorize"
    assert _call(port, auth, {"host": host, "code_challenge": challenge})[0] == 401
    assert _call(port, auth, {"host": "autre.nokker.app", "code_challenge": challenge}, token="sess-pierre")[0] == 403
    assert _call(port, auth, {"host": host, "code_challenge": challenge, "code_challenge_method": "plain"},
                 token="sess-pierre")[0] == 400
    status, a = _call(port, auth, {"host": host, "code_challenge": challenge}, token="sess-pierre")
    assert status == 200 and a["redirect"] == f"https://{host}/auth/callback"
    assert _call(port, "/v1/gateway/token", {"code": a["code"], "code_verifier": "faux" * 12, "host": host})[0] == 400
    assert _call(port, "/v1/gateway/token", {"code": a["code"], "code_verifier": verifier, "host": host})[0] == 400  # consommé
    a = _call(port, auth, {"host": host, "code_challenge": challenge}, token="sess-pierre")[1]
    status, t1 = _call(port, "/v1/gateway/token", {"code": a["code"], "code_verifier": verifier, "host": host})
    assert status == 200 and t1["expires_in"] == 900 and t1["refresh_token"]
    claims = verify_token(t1["access_token"], _call(port, "/v1/.well-known/jwks.json")[1])
    assert claims["machine"] == tok["machine_id"] and claims["tenant"] == "tn_pierre"
    status, t2 = _call(port, "/v1/gateway/refresh", {"refresh_token": t1["refresh_token"], "host": host})
    assert status == 200 and t2["refresh_token"] != t1["refresh_token"]
    # l'ancien resservi (vol) : refusé, et toute la famille tombe — le nouveau aussi
    assert _call(port, "/v1/gateway/refresh", {"refresh_token": t1["refresh_token"], "host": host})[0] == 400
    assert _call(port, "/v1/gateway/refresh", {"refresh_token": t2["refresh_token"], "host": host})[0] == 400
    # nouvelle connexion, puis déconnexion
    a = _call(port, auth, {"host": host, "code_challenge": challenge}, token="sess-pierre")[1]
    t3 = _call(port, "/v1/gateway/token", {"code": a["code"], "code_verifier": verifier, "host": host})[1]
    assert _call(port, "/v1/gateway/revoke", {"refresh_token": t3["refresh_token"]}) == (200, {"revoked": True})
    assert _call(port, "/v1/gateway/refresh", {"refresh_token": t3["refresh_token"], "host": host})[0] == 400
    # machine déliée : plus de renouvellement
    a = _call(port, auth, {"host": host, "code_challenge": challenge}, token="sess-pierre")[1]
    t4 = _call(port, "/v1/gateway/token", {"code": a["code"], "code_verifier": verifier, "host": host})[1]
    _call(port, f"/v1/link/machines/{tok['machine_id']}/revoke", {}, token="sess-pierre")
    assert _call(port, "/v1/gateway/refresh", {"refresh_token": t4["refresh_token"], "host": host})[0] == 400
    # l'ancien chemin (session du compte → jeton d'accès) reste, sans code
    assert _call(port, "/v1/gateway/token", {}, token="sess-pierre")[0] == 200
