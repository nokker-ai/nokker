"""API locale de Nokker Lokkal (nokker/local/api.py) : jeton local 0600, contrôle du Host, sessions de code sous une
racine déclarée, et son câblage dans le vrai `make_handler` (boucle locale, port éphémère)."""
from __future__ import annotations

import json
import os
import stat
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from nokker.api.server import make_handler
from nokker.core.ports import ToolContext
from nokker.local.api import LOCAL_OWNER, LocalApi, SessionCodeTools, ensure_token
from tests.unit.api.test_server import make_kernel


def test_token_is_created_0600_and_reused(tmp_path):
    path = tmp_path / "cfg" / "local-token"
    tok = ensure_token(path)
    assert len(tok) >= 32 and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert ensure_token(path) == tok


def _api(tmp_path) -> LocalApi:
    root = tmp_path / "projets"
    (root / "app").mkdir(parents=True)
    return LocalApi.from_config({"token_path": str(tmp_path / "tok"), "code": {"roots": [str(root)],
                                                                             "backend": "local"}}, port=8742)


def test_host_and_token_checks(tmp_path):
    api = _api(tmp_path)
    assert api.host_ok("127.0.0.1:8742") and api.host_ok("LOCALHOST:8742")
    assert not api.host_ok("evil.example:8742") and not api.host_ok(None) and not api.host_ok("127.0.0.1:9999")
    assert api.handle("GET", "/v1/local/sessions", None, {})[0] == 401
    assert api.handle("GET", "/v1/local/sessions", "Bearer faux", {})[0] == 401
    assert api.handle("GET", "/v1/autre", f"Bearer {api.token}", {}) is None


def test_sessions_only_under_declared_roots(tmp_path):
    api = _api(tmp_path)
    auth = f"Bearer {api.token}"
    (tmp_path / "ailleurs").mkdir()
    for cwd in (str(tmp_path / "ailleurs"), str(tmp_path / "projets" / ".." / "ailleurs"), "", "/etc"):
        assert api.handle("POST", "/v1/local/sessions", auth, {"cwd": cwd})[0] == 403, cwd
    status, body = api.handle("POST", "/v1/local/sessions", auth, {"cwd": str(tmp_path / "projets" / "app")})
    assert status == 201 and body["thread_id"].startswith("th_")
    assert api.handle("GET", "/v1/local/sessions", auth, {})[1]["sessions"][0]["thread_id"] == body["thread_id"]
    assert api.handle("GET", "/v1/local/sessions", auth, {})[1]["roots"] == api.roots  # proposées par le web


def test_session_tools_route_by_thread(tmp_path):
    tools = SessionCodeTools(backend="local")
    assert {s.name for s in tools.specs()} >= {"read_file", "terminal", "patch"}
    r = tools.call("read_file", {"path": "a"}, ToolContext(thread_id="th_x", turn_index=0, channel="api"))
    assert r.is_error and "pas de session" in r.content


@pytest.fixture
def server(tmp_path):
    api = _api(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {api.token: LOCAL_OWNER},
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield api, port
    httpd.shutdown()
    t.join(timeout=5)


def _req(port, path, *, token=None, host=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    if host:
        req.add_unredirected_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_real_handler_refuses_foreign_host_and_serves_local_routes(server, tmp_path):
    api, port = server
    assert _req(port, "/healthz", host="attaquant.example")[0] == 403  # DNS rebinding : toutes les routes
    assert _req(port, "/healthz")[0] == 200
    assert _req(port, "/v1/local/sessions")[0] == 401
    status, body = _req(port, "/v1/local/sessions", token=api.token, body={"cwd": str(tmp_path / "projets" / "app")})
    assert status == 201 and body["thread_id"].startswith("th_")


# ─────────────────────────────── premier lancement (/v1/local/setup*) ───────────────────────────────


def test_vault_refuses_without_key_and_encrypts_with_it(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from nokker.local.setup import LocalVault, VaultError

    vault = LocalVault(tmp_path / "vault.bin")
    monkeypatch.delenv("NOKKER_LOCAL_SECRET_KEY", raising=False)
    with pytest.raises(VaultError):
        vault.put("byok:x", "sk-secret-123")
    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", Fernet.generate_key().decode())
    vault.put("byok:x", "sk-secret-123")
    raw = (tmp_path / "vault.bin").read_bytes()
    assert b"sk-secret-123" not in raw and stat.S_IMODE(os.stat(tmp_path / "vault.bin").st_mode) == 0o600
    assert vault.get("byok:x") == "sk-secret-123" and vault.names() == ["byok:x"]


def test_setup_routes_never_return_a_key_and_switch_the_model(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from nokker.local.setup import LocalSetup, LocalVault

    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", Fernet.generate_key().decode())
    api = _api(tmp_path)
    api.setup = LocalSetup(state_path=tmp_path / "setup.json", vault=LocalVault(tmp_path / "vault.bin"),
                           detect=lambda: [], test=lambda base, key: {"ok": key == "sk-k", "models": ["m1"],
                                                                      "latency_ms": 3})
    seen = []
    api.on_default_model = lambda *a: seen.append(a)
    auth = f"Bearer {api.token}"
    assert api.handle("GET", "/v1/local/setup", auth, {})[1]["done"] is False
    assert api.handle("POST", "/v1/local/setup/models/test", auth,
                      {"base_url": "https://ex.test/v1", "api_key": "sk-k"})[1]["ok"] is True
    assert api.handle("POST", "/v1/local/setup/models/test", auth, {"base_url": "file:///etc"})[1]["ok"] is False
    status, view = api.handle("PUT", "/v1/local/setup/models", auth, {
        "byok": [{"name": "perso", "base_url": "https://ex.test/v1", "model": "m1", "api_key": "sk-k"}],
        "default": "byok:perso"})
    assert status == 200 and "sk-k" not in json.dumps(view) and view["models"]["byok"][0]["has_key"] is True
    assert seen == [("https://ex.test/v1", "sk-k", "m1")]  # paliers rebranchés en direct
    assert api.handle("PUT", "/v1/local/setup/models", auth, {"default": "nimporte"})[0] == 400
    assert api.handle("POST", "/v1/local/setup/done", auth, {}) == (200, {"done": True})
    assert "sk-k" not in (tmp_path / "setup.json").read_text()


def test_roots_and_hosts_from_environment(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    monkeypatch.setenv("NOKKER_HOST_WORKSPACE", f"{tmp_path / 'a'}:{tmp_path / 'b'}")
    monkeypatch.setenv("NOKKER_ALLOWED_HOSTS", "127.0.0.1:7420, localhost:7420")
    api = LocalApi.from_config({"token_path": str(tmp_path / "tok"), "allowed_hosts_env": "NOKKER_ALLOWED_HOSTS",
                                "code": {"roots_env": "NOKKER_HOST_WORKSPACE", "roots": ["/ignoré"]}}, port=8742)
    assert api.roots == [os.path.realpath(tmp_path / "a"), os.path.realpath(tmp_path / "b")]
    assert api.host_ok("127.0.0.1:7420") and not api.host_ok("127.0.0.1:8742")


def test_subagent_threads_use_their_parent_source(tmp_path):
    tools = SessionCodeTools(backend="local")
    (tmp_path / "p").mkdir()
    tools.open("th_parent", str(tmp_path / "p"), str(tmp_path))
    assert tools.for_thread("th_parent/sa-1") is tools.for_thread("th_parent")
    assert tools.for_thread("th_parent/sa-1/sa-2") is tools.for_thread("th_parent")
    assert tools.for_thread("th_autre/sa-1") is None


def test_account_link_device_flow_keeps_the_key_in_the_vault(tmp_path, monkeypatch):
    import httpx
    from cryptography.fernet import Fernet

    from nokker.local.setup import LocalLink, LocalSetup, LocalVault

    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", Fernet.generate_key().decode())
    polls = []

    def cloud(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/link/device":
            return httpx.Response(200, json={"device_code": "dc", "user_code": "ABCD-1234",
                                             "verification_uri": "https://app.nokker.ai/link", "expires_in": 600,
                                             "interval": 5})
        if req.url.path == "/v1/link/token":
            polls.append(1)
            return httpx.Response(200, json={"state": "pending"} if len(polls) == 1 else
                                  {"state": "approved", "api_key": "rk-secret", "account": "us_abc"})
        if req.url.path == "/v1/gateway/machine":
            return httpx.Response(200, json={"pages_public": True})
        return httpx.Response(200, json={})

    api = _api(tmp_path)
    api.setup = LocalSetup(state_path=tmp_path / "setup.json", vault=LocalVault(tmp_path / "vault.bin"), detect=lambda: [])
    api.link = LocalLink(setup=api.setup, client=httpx.Client(transport=httpx.MockTransport(cloud)))
    auth = f"Bearer {api.token}"
    status, body = api.handle("POST", "/v1/local/link/start", auth, {})
    assert status == 200 and body["user_code"] == "ABCD-1234" and "device_code" not in body
    assert api.handle("GET", "/v1/local/link/status", auth, {})[1] == {"state": "pending"}
    status, body = api.handle("GET", "/v1/local/link/status", auth, {})
    # sans jeton de machine, pas de droit de publier lu (pages publiques, §7.26)
    assert body == {"state": "approved", "account": "us_abc", "pages_public": False} and "rk-secret" not in json.dumps(body)
    api.setup.vault.put("machine_token", "mt-x")
    assert api.handle("GET", "/v1/local/link/status", auth, {})[1]["pages_public"] is True
    assert api.setup.vault.get("router") == "rk-secret" and api.setup.state["default"] == "router"
    assert api.handle("DELETE", "/v1/local/link", auth, {})[1] == {"state": "unlinked"}
    assert api.setup.vault.get("router") is None and api.setup.state["default"] is None


def test_account_link_reports_an_unreachable_cloud(tmp_path):
    import httpx

    from nokker.local.setup import LocalLink, LocalSetup, LocalVault

    api = _api(tmp_path)
    api.setup = LocalSetup(state_path=tmp_path / "setup.json", vault=LocalVault(tmp_path / "vault.bin"), detect=lambda: [])

    def down(req):
        raise httpx.ConnectError("refusé")

    api.link = LocalLink(setup=api.setup, client=httpx.Client(transport=httpx.MockTransport(down)))
    assert api.handle("POST", "/v1/local/link/start", f"Bearer {api.token}", {})[0] == 502


def test_cancel_route_and_healthz_versions(server):
    api, port = server
    assert _req(port, "/v1/threads/th_x/cancel", body={})[0] == 401
    status, body = _req(port, "/v1/threads/th_x/cancel", token=api.token, body={})
    assert status == 202 and body["cancelled"] == "th_x"
    status, body = _req(port, "/healthz")
    assert body["version"] and body["min_client_version"]


def test_local_token_opens_threads_bubbles_events_and_chat(tmp_path):
    """Le client web local (mode 127.0.0.1) se sert du jeton local comme session : fils, bulles, flux d'événements et
    chat passent, avec l'identité LOCAL_OWNER. Câblage de `serve` : le hub ne connaît aucun utilisateur de config."""
    from nokker.api.events import EventBus
    from nokker.bubbles.hub import BubbleHub
    from nokker.bubbles.memory import InMemoryBubbles
    from nokker.threads.memory import InMemoryThreads

    api = _api(tmp_path)
    bus = EventBus()
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=bus.publish,
                    known_users=frozenset(), spawn=lambda fn: fn())
    kernel = make_kernel("ok")
    kernel.observer, hub.kernel, hub.scrub = hub, kernel, kernel.invariants.scrub_output
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, events=bus, hub=hub,
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        for path in ("/v1/threads", "/v1/bubbles?thread_id=th_inconnu"):
            assert _req(port, path)[0] == 401, path
        status, th = _req(port, "/v1/threads", token=api.token, body={"title": "local"})
        assert status == 201 and th["owner"] == LOCAL_OWNER
        status, body = _req(port, "/v1/threads", token=api.token)
        assert status == 200 and [x["id"] for x in body] == [th["id"]]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=json.dumps({"model": "nokker", "messages": [{"role": "user", "content": "salut"}]}).encode(),
            headers={"Authorization": f"Bearer {api.token}", "Content-Type": "application/json",
                     "X-Session-Id": th["id"]})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200 and json.loads(r.read())["choices"][0]["message"]["content"] == "ok"
        status, body = _req(port, f"/v1/threads/{th['id']}", token=api.token)
        assert status == 200 and [m["role"] for m in body["messages"]] == ["user", "assistant"]
        assert _req(port, f"/v1/threads/{th['id']}/bubbles", token=api.token)[0] == 200
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/events",
                                     headers={"Authorization": f"Bearer {api.token}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200 and r.headers["Content-Type"].startswith("text/event-stream")
    finally:
        httpd.shutdown()
        t.join(timeout=5)


def test_web_served_by_the_gateway_opens_threads_with_the_browser_token(tmp_path):
    """Le web servi par `<nom>.nokker.app` ouvre sa session par code + PKCE (jeton lié à CETTE machine), puis lit ses
    fils : même identité LOCAL_OWNER que le jeton local. Recette du 26.10 : 401 sur /v1/threads, la connexion échouait."""
    from nokker.api.events import EventBus
    from nokker.bubbles.hub import BubbleHub
    from nokker.bubbles.memory import InMemoryBubbles
    from nokker.cloud.link import Signer
    from nokker.local.setup import LocalSetup, LocalVault
    from nokker.threads.memory import InMemoryThreads

    api = _api(tmp_path)
    bus = EventBus()
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=bus.publish,
                    known_users=frozenset(), spawn=lambda fn: fn())
    kernel = make_kernel("ok")
    kernel.observer, hub.kernel, hub.scrub = hub, kernel, kernel.invariants.scrub_output
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, events=bus, hub=hub,
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    signer = Signer.generate()
    api.setup = LocalSetup(state_path=tmp_path / "setup.json", vault=LocalVault(tmp_path / "v.bin"), detect=lambda: [])
    api.setup.state.update(jwks=signer.jwks(), account="us_pierre", machine_id="mc_1")

    class Up:
        state = "up"

    api.relay_host, api.tunnel = "pierre.nokker.app", Up()
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    def relayed(path, token):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     headers={"Authorization": f"Bearer {token}", "X-Nokker-Relay": "1"})
        req.add_unredirected_header("Host", "pierre.nokker.app")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        browser = signer.access_token("us_pierre", "tn_pierre", machine="mc_1")
        assert relayed("/v1/threads", browser) == 200
        assert relayed("/v1/local/sessions", browser) == 200  # liste + racines pour « + session de code » du web
        assert relayed("/v1/notifications", browser) == 200
        assert relayed("/v1/threads", signer.access_token("us_pierre", "tn_pierre", machine="mc_2")) == 401
        assert relayed("/v1/threads", signer.access_token("us_autre", "tn_autre", machine="mc_1")) == 401
        assert relayed("/v1/threads", Signer.generate().access_token("us_pierre", "tn_pierre", machine="mc_1")) == 401
        assert _req(port, "/v1/threads", token=browser)[0] == 401  # sans la passerelle, ce jeton n'est rien
    finally:
        httpd.shutdown()
        t.join(timeout=5)


# ─────────────────────────────── décisions locales (§7.4.3) ───────────────────────────────


def test_suggested_rules_and_user_rule_file(tmp_path):
    from nokker.local.approvals import append_user_rule, suggested_rules

    assert suggested_rules("terminal", {"command": "npm  test --watch"}, "standard") == [
        "terminal(npm test --watch)", "terminal(npm test*)"]
    assert suggested_rules("patch", {"path": "./src/a.py"}, "standard") == ["patch(src/a.py)", "patch(src/**)"]
    assert suggested_rules("terminal", {"command": "rm -rf build"}, "strong") == []  # pas d'« always » sur une forte
    f = tmp_path / "settings.json"
    assert append_user_rule(f, "terminal(npm test*)") and not append_user_rule(f, "terminal(npm test*)")
    assert json.loads(f.read_text())["permissions"]["allow"] == ["terminal(npm test*)"]


def _card(approvals, thread, command, strength="standard"):
    from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict

    turn = Turn(thread_id=thread, index=0, input=InputCheck(text="lance les tests"), channel="api", sender=LOCAL_OWNER)
    return approvals.request(turn, ToolCall(id="c1", name="terminal", arguments={"command": command}),
                             Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="permissions:default",
                                     strength=strength), None)


def test_local_decision_routes(tmp_path):
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals

    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    kernel.gates = PermissionGates(inner=kernel.gates, permissions=LocalPermissions(mode=Mode.ACCEPT_EDITS))
    api = _api(tmp_path)
    api.settings_path = tmp_path / "settings.json"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, approvals=approvals,
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        a = _card(approvals, "th_1", "npm test --watch")
        assert _req(port, "/v1/local/approvals")[0] == 401
        listed = _req(port, "/v1/local/approvals", token=api.token)[1]["approvals"]
        assert listed[0]["suggested_rules"] == ["terminal(npm test --watch)", "terminal(npm test*)"]
        bad = _req(port, f"/v1/local/approvals/{a.id}/decide", token=api.token,
                   body={"decision": "approve", "scope": "always", "rule": "terminal(*)"})
        assert bad[0] == 400  # une règle hors des suggestions est refusée
        status, body = _req(port, f"/v1/local/approvals/{a.id}/decide", token=api.token,
                            body={"decision": "approve", "scope": "session", "rule": "terminal(npm test*)"})
        assert status == 202 and body["scope"] == "session" and body["continuation"] in ("running", "queued")
        assert kernel.gates.permissions.thread_allow["th_1"][0].pattern == "npm test*"
        b = _card(approvals, "th_1", "npm run build --prod")
        status, body = _req(port, f"/v1/local/approvals/{b.id}/decide", token=api.token,
                            body={"decision": "reject", "scope": "always", "rule": "terminal(npm run*)"})
        assert status == 200 and approvals.get(b.id).status == "rejected"
        assert not api.settings_path.exists()  # un refus n'écrit aucune règle
        c = _card(approvals, "th_1", "rm -rf dist", strength="strong")
        assert _req(port, f"/v1/local/approvals/{c.id}/decide", token=api.token,
                    body={"decision": "approve", "scope": "always", "rule": "terminal(rm -rf dist)"})[0] == 400
    finally:
        httpd.shutdown()


def test_sandboxes_carry_the_instance_and_reaping_is_scoped(tmp_path, monkeypatch):
    from nokker.tools import code

    api = _api(tmp_path)
    assert api.code.instance and len(api.code.instance) == 12
    again = LocalApi.from_config({"token_path": str(tmp_path / "tok")}, port=8742)
    assert again.code.instance == api.code.instance  # stable pour un même dossier d'état
    (tmp_path / "projets" / "app2").mkdir()
    api.code.open("th_i", str(tmp_path / "projets" / "app2"), str(tmp_path / "projets"))
    assert api.code.for_thread("th_i").instance == api.code.instance
    calls = []
    monkeypatch.setattr("subprocess.run", lambda argv, **kw: calls.append(argv) or type("R", (), {"stdout": ""})())
    assert code.reap_sandboxes("") == 0 and calls == []  # sans instance : rien n'est ramassé
    code.reap_sandboxes(api.code.instance)
    assert calls[0][-1] == f"label=nokker.instance={api.code.instance}"


def test_machine_route(tmp_path):
    api = _api(tmp_path)
    status, body = api.handle("GET", "/v1/local/machine", f"Bearer {api.token}", {})
    assert status == 200 and body["machine_id"] == api.code.instance and body["tunnel"] == "down"


def test_relay_host_only_while_the_tunnel_is_up_and_local_token_refused_through_it(server):
    api, port = server

    class FakeTunnel:
        state = "down"

    api.relay_host, api.tunnel = "pierre.nokker.app", FakeTunnel()
    assert _req(port, "/healthz", host="pierre.nokker.app")[0] == 403  # tunnel fermé : Host refusé
    api.tunnel.state = "up"
    assert _req(port, "/healthz", host="pierre.nokker.app")[0] == 200

    def relayed(token):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/local/sessions",
                                     headers={"Authorization": f"Bearer {token}", "X-Nokker-Relay": "1"})
        req.add_unredirected_header("Host", "pierre.nokker.app")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    assert relayed(api.token) == 403  # le jeton local ne passe jamais par la passerelle
    assert _req(port, "/v1/local/machine", token=api.token)[1]["tunnel"] == "up"


def test_signed_token_through_the_gateway_is_the_owner(server, tmp_path):
    from nokker.cloud.link import Signer
    from nokker.local.setup import LocalSetup, LocalVault

    api, port = server
    signer = Signer.generate()
    api.setup = LocalSetup(state_path=tmp_path / "setup2.json", vault=LocalVault(tmp_path / "v2.bin"), detect=lambda: [])
    api.setup.state.update(jwks=signer.jwks(), account="us_pierre", machine_id="mc_1")

    class Up:
        state = "up"

    api.relay_host, api.tunnel = "pierre.nokker.app", Up()

    def relayed(path, token, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": f"Bearer {token}", "X-Nokker-Relay": "1",
                                              "Content-Type": "application/json"})
        req.add_unredirected_header("Host", "pierre.nokker.app")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    good = signer.access_token("us_pierre", "tn_pierre")
    assert relayed("/v1/local/sessions", good) == 200
    assert relayed("/v1/local/sessions", signer.access_token("us_autre", "tn_autre")) == 401  # autre compte
    assert relayed("/v1/local/sessions", Signer.generate().access_token("us_pierre", "tn_pierre")) == 401  # faux émetteur
    other_machine = signer.delegation_token("us_pierre", "tn_pierre", machine="mc_2", web_session="w")
    assert relayed("/v1/local/sessions", other_machine) == 401  # délégation pour une autre machine
    assert relayed("/v1/local/sessions", good) == 200 and _req(port, "/v1/local/sessions", token=good)[0] == 401


def test_repo_settings_restrict_only_their_own_session(tmp_path):
    """`.nokker/settings.json` du dépôt, lu à l'ouverture de la session : plus strict pour CE fil, jamais plus large."""
    from nokker.core.turn import Decision, Effect, ToolCall
    from nokker.qa.permissions import LocalPermissions, Mode

    perms = LocalPermissions(mode=Mode.ACCEPT_EDITS)
    api = _api(tmp_path)
    api.on_session_open = lambda t, cwd: perms.restrict_for_thread(t, os.path.join(cwd, ".nokker", "settings.json"))
    repo = tmp_path / "projets" / "app"
    (repo / ".nokker").mkdir()
    (repo / ".nokker" / "settings.json").write_text(json.dumps({"permissions": {
        "mode": "plan", "deny": ["read_file(secrets/*)"], "allow": ["terminal(*)"]}}))
    status, body = api.handle("POST", "/v1/local/sessions", f"Bearer {api.token}", {"cwd": str(repo)})
    assert status == 201
    th = body["thread_id"]
    edit = ToolCall(id="1", name="write_file", arguments={"path": "a.py"})
    assert perms.decide(edit, Effect.WRITE, th)[0] is Decision.DENY  # mode plan du dépôt
    assert perms.decide(edit, Effect.WRITE, "th_autre")[0] is Decision.ALLOW  # l'autre fil garde accept_edits
    secret = ToolCall(id="2", name="read_file", arguments={"path": "secrets/k"})
    assert perms.decide(secret, Effect.READ, th)[0] is Decision.DENY
    cmd = ToolCall(id="3", name="terminal", arguments={"command": "rm -rf build"})
    assert perms.decide(cmd, Effect.DESTRUCTIVE, th)[0] is not Decision.ALLOW  # son allow est ignoré


def test_mirror_routes_are_owner_only_and_report_a_failed_erase(tmp_path):
    import httpx

    api = _api(tmp_path)
    assert api.handle("GET", "/v1/local/mirror", f"Bearer {api.token}", {}) == (200, {"enabled": False})
    assert api.handle("POST", "/v1/local/mirror/erase", f"Bearer {api.token}", {})[0] == 404

    class Mirror:
        paused = False

        def status(self):
            return {"enabled": True, "paused": self.paused}

        def erase_remote(self):
            raise httpx.ConnectError("injoignable")

        def resume(self):
            self.paused = False

    api.mirror = Mirror()
    assert api.handle("POST", "/v1/local/mirror/erase", None, {})[0] == 401
    status, body = api.handle("POST", "/v1/local/mirror/erase", f"Bearer {api.token}", {})
    assert status == 502 and "rien n'a changé" in body["error"]["message"]
    assert api.handle("POST", "/v1/local/mirror/resume", f"Bearer {api.token}", {}) == (200, {"enabled": True,
                                                                                            "paused": False})
