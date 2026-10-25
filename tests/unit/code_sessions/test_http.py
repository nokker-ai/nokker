"""Routes /v1/code/* sur le vrai `make_handler` : drapeau éteint = 404 partout ; allumé = sessions et identifiants de
forge sous l'identité d'un compte (session d'appareil), dans le locataire du compte, jamais ceux d'un autre utilisateur
ni d'un autre locataire ; une valeur de secret ne revient jamais."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer

import pytest

from nokker.api.server import make_handler
from nokker.approvals.memory import InMemoryApprovals
from nokker.code_sessions import http as code_http
from nokker.code_sessions.config import CodeConfig
from nokker.code_sessions.secrets import TenantSecrets
from nokker.code_sessions.service import CodeSessions
from nokker.code_sessions.store import MemoryCodeStore
from nokker.core.turn import ToolResult
from nokker.tenancy.store import MemoryTenancy
from tests.unit.api.test_server import make_kernel
from tests.unit.code_sessions.conftest import MemorySecrets, code_block
from tests.unit.team.test_http import _session

PATHS = (("GET", "/v1/code/sessions"), ("POST", "/v1/code/sessions"), ("GET", "/v1/code/sessions/cs_0123456789abcdef"),
         ("DELETE", "/v1/code/sessions/cs_0123456789abcdef"), ("GET", "/v1/code/credentials"),
         ("PUT", "/v1/code/credentials/bot"), ("DELETE", "/v1/code/credentials/bot"),
         ("POST", "/v1/code/sessions/cs_0123456789abcdef/hooks/trust"),
         ("POST", "/v1/code/sessions/cs_0123456789abcdef/tools/call"))


def _start(svc, kernel=None):
    tenancy = MemoryTenancy()
    ana = tenancy.create_account("ana@example.org")
    cam = tenancy.create_account("cam@example.org")
    ben = replace(ana, account_id="us_" + "b" * 12, email="ben@example.org")  # même locataire qu'ana
    tenancy.accounts[ben.account_id] = ben
    approvals = InMemoryApprovals()
    code_http.configure(svc)
    handler = make_handler(kernel or make_kernel(), {}, approvals=approvals, tenancy=tenancy, open_api=False,
                           internal_tenant=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    accs = {"ana": ana, "ben": ben, "cam": cam}
    tokens = {k: _session(approvals, a.account_id) for k, a in accs.items()}
    port = httpd.server_address[1]

    def call(method, path, body=None, who=None):
        h = {"Content-Type": "application/json"}
        if who:
            h["Authorization"] = f"Bearer {tokens[who]}"
        data = json.dumps(body).encode() if body is not None else (b"{}" if method in ("POST", "PUT") else None)
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h, data=data)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                out = r.read()
                return r.status, json.loads(out) if out else None
        except urllib.error.HTTPError as e:
            out = e.read()
            return e.code, json.loads(out) if out else None

    return httpd, call, accs


def test_flag_off_is_404():
    httpd, call, _ = _start(None)
    try:
        for method, path in PATHS:
            assert call(method, path, {}, who="ana")[0] == 404, (method, path)
    finally:
        httpd.shutdown()
        code_http.configure(None)


@pytest.fixture
def api(cluster, fake_upstream):
    vaults: dict = {}
    cfg = CodeConfig.from_config(code_block(cluster.workspace_root, max_sessions_per_user=2), lokkal=False)
    svc = CodeSessions(cfg=cfg, store=MemoryCodeStore(),
                       secrets=TenantSecrets(lambda tn: vaults.setdefault(tn, MemorySecrets())),
                       cloner=lambda *a, **k: None)
    httpd, call, accs = _start(svc)
    yield call, accs, svc
    httpd.shutdown()
    svc.close_all()
    code_http.configure(None)


def test_sessions_routes(api):
    call, accs, svc = api
    assert call("GET", "/v1/code/sessions")[0] == 401
    s, a = call("POST", "/v1/code/sessions", {}, who="ana")
    assert s == 201 and a["id"].startswith("cs_") and a["thread_id"] == a["id"]
    assert call("GET", "/v1/code/sessions", who="ana")[1]["sessions"][0]["id"] == a["id"]
    assert call("GET", "/v1/code/sessions", who="ana")[1]["limit"] == 2
    lst = call("GET", "/v1/code/sessions", who="ana")[1]  # ce que l'écran propose à l'ouverture (§7.28)
    assert lst["modes"] == ["default", "accept_edits", "plan"] and lst["default_mode"] == "accept_edits"
    assert lst["idle_ttl_s"] == svc.cfg.idle_ttl_s
    assert call("GET", "/v1/code/sessions", who="ben")[1]["sessions"] == []  # même locataire, autre utilisateur
    assert call("GET", "/v1/code/sessions", who="cam")[1]["sessions"] == []  # autre locataire
    for who in ("ben", "cam"):
        assert call("GET", f"/v1/code/sessions/{a['id']}", who=who)[0] == 404
        assert call("DELETE", f"/v1/code/sessions/{a['id']}", who=who)[0] == 404
    assert call("GET", f"/v1/code/sessions/{a['id']}", who="ana")[1]["status"] == "active"
    assert call("POST", "/v1/code/sessions", {}, who="ana")[0] == 201
    s, err = call("POST", "/v1/code/sessions", {}, who="ana")
    assert s == 409 and err["error"]["code"] == "session_limit"
    s, out = call("DELETE", f"/v1/code/sessions/{a['id']}?purge=1", who="ana")
    assert s == 200 and out == {"id": a["id"], "closed": True, "purged": True}
    assert call("POST", "/v1/code/sessions", {"mode": "auto"}, who="ana")[1]["error"]["code"] == "bad_mode"
    assert call("GET", "/v1/code/sessions/cs_zz", who="ana")[0] == 404


def test_credentials_routes_never_return_a_value(api):
    call, accs, svc = api
    assert call("PUT", "/v1/code/credentials/forge-bot", {"value": "tok-HTTP-SECRET"}, who="ana")[1] == {
        "name": "forge-bot", "set": True}
    s, lst = call("GET", "/v1/code/credentials", who="ana")
    assert s == 200 and [c["name"] for c in lst["credentials"]] == ["forge-bot"] and "tok-HTTP" not in json.dumps(lst)
    assert call("GET", "/v1/code/credentials", who="cam")[1]["credentials"] == []  # autre locataire
    assert call("PUT", "/v1/code/credentials/bad%20name", {"value": "x"}, who="ana")[0] == 400
    assert call("PUT", "/v1/code/credentials/x", {}, who="ana")[0] == 400
    body = {"repo": {"url": "https://git.example.org/a.git", "credential": "forge-bot"}}
    s, a = call("POST", "/v1/code/sessions", body, who="ana")
    assert s == 201 and "tok-HTTP" not in json.dumps(a)
    s, err = call("POST", "/v1/code/sessions", body, who="cam")
    assert s == 400 and err["error"]["code"] == "credential_missing"
    assert call("DELETE", "/v1/code/credentials/forge-bot", who="ana")[1] == {"name": "forge-bot", "deleted": True}
    assert call("DELETE", "/v1/code/credentials/forge-bot", who="ana")[0] == 404


@pytest.fixture
def api_tools(cluster, fake_upstream):
    """Appel direct d'un outil de point de retour depuis l'écran (§7.28) : `kernel.direct_call` enregistré."""
    cfg = CodeConfig.from_config(code_block(cluster.workspace_root), lokkal=False)
    svc = CodeSessions(cfg=cfg, store=MemoryCodeStore(), secrets=None, cloner=lambda *a, **k: None)
    kernel = make_kernel()
    calls: list = []
    answer: dict = {"out": None}

    def direct_call(thread_id, name, arguments, sender=None):
        calls.append((thread_id, name, arguments, sender))
        return answer["out"] or ToolResult(call_id="c", name=name, content="[]")

    kernel.direct_call = direct_call
    httpd, call, accs = _start(svc, kernel)
    yield call, accs, calls, answer
    httpd.shutdown()
    svc.close_all()
    code_http.configure(None)


def test_tools_call_checkpoints_only_on_own_session(api_tools):
    call, accs, calls, answer = api_tools
    a = call("POST", "/v1/code/sessions", {}, who="ana")[1]
    url = f"/v1/code/sessions/{a['id']}/tools/call"
    s, out = call("POST", url, {"name": "checkpoint_list", "arguments": {}}, who="ana")
    assert s == 200 and out == {"content": "[]", "is_error": False}
    internal = f"{accs['ana'].tenant_id}:{a['id']}"  # le fil interne du noyau, préfixé du locataire
    assert calls == [(internal, "checkpoint_list", {}, accs["ana"].account_id)]
    for name in ("terminal", "write_file", "patch", "checkpoint_create", "todo", "memory"):
        s, err = call("POST", url, {"name": name, "arguments": {}}, who="ana")
        assert s == 403 and err["error"]["code"] == "tool_not_allowed", name
    assert call("POST", url, {"name": "checkpoint_diff", "arguments": "x"}, who="ana")[0] == 400
    for who in ("ben", "cam"):  # même locataire autre utilisateur, autre locataire : la session n'existe pas
        assert call("POST", url, {"name": "checkpoint_list", "arguments": {}}, who=who)[0] == 404
    assert len(calls) == 1
    answer["out"] = "ap_0123"  # restauration qui attend une carte : 202, décidée par /v1/approvals/{id}/decision
    s, out = call("POST", url, {"name": "checkpoint_restore", "arguments": {"hash": "abc"}}, who="ana")
    assert s == 202 and out == {"approval_id": "ap_0123"}
    call("DELETE", f"/v1/code/sessions/{a['id']}", who="ana")
    assert call("POST", url, {"name": "checkpoint_list", "arguments": {}}, who="ana")[0] == 404
