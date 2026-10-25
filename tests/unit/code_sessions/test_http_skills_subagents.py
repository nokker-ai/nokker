"""Skills du dépôt et sous-agents d'une session de code par les routes, sur le vrai `make_handler` et le vrai
`kernel.direct_call` : `POST /v1/code/sessions/{cs}/skills/trust`, `skill_list`/`skill_view` et
`delegate_status`/`delegate_cancel` par `tools/call` (lecture, jamais `delegate` lui-même) ; un autre utilisateur du
même locataire ou d'un autre locataire n'y voit rien ; une carte d'enfant n'est listée qu'à son propriétaire."""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from nokker.api.server import make_handler
from nokker.approvals.memory import InMemoryApprovals
from nokker.code_sessions import http as code_http
from nokker.code_sessions.config import CodeConfig
from nokker.code_sessions.service import CodeSessions
from nokker.code_sessions.store import MemoryCodeStore
from nokker.core.ports import ToolContext
from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.store import MemoryTenancy
from nokker.tools import skills as sk
from nokker.tools.registry import Registry
from tests.unit.api.test_server import make_kernel
from tests.unit.code_sessions.conftest import code_block
from tests.unit.code_sessions.test_project_skills import repo_cloner
from tests.unit.team.test_http import _session
from tests.unit.tools.test_delegate import Factory, turn as fake_turn


@pytest.fixture(autouse=True)
def safe_guard(monkeypatch):
    monkeypatch.setattr(sk, "_guard_module", lambda: SimpleNamespace(
        scan_skill_cached=lambda p, **kw: (SimpleNamespace(verdict="safe", summary="vu"), {})))


class AllowAll:
    code_session = None

    class invariants:
        @staticmethod
        def check_action(call, spec, turn):
            return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="ok")

    def action(self, call, spec, turn):
        return [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="ok")]

    def switch(self, reply, turn, tier):
        return []


@pytest.fixture
def api(cluster, fake_upstream):
    cfg = CodeConfig.from_config(code_block(cluster.workspace_root), lokkal=False)
    svc = CodeSessions(cfg=cfg, store=MemoryCodeStore(), cloner=repo_cloner)
    kernel = make_kernel()
    kernel.tools = Registry(sources=[svc.tools(), svc.skill_tools(), svc.subagent_tools()])
    kernel.gates = svc.gates(AllowAll())
    release = threading.Event()
    factory = Factory(behave=lambda k, inp: release.wait(5) and fake_turn("fini") or fake_turn("fini"))
    svc.kernel_factory, svc.parent_registry = factory, lambda: kernel.tools
    tenancy = MemoryTenancy()
    ana = tenancy.create_account("ana@example.org")
    cam = tenancy.create_account("cam@example.org")
    ben = replace(ana, account_id="us_" + "b" * 12, email="ben@example.org")  # même locataire qu'ana
    tenancy.accounts[ben.account_id] = ben
    approvals = InMemoryApprovals()
    approvals.thread_owner = svc.owner_of_thread
    code_http.configure(svc)
    handler = make_handler(kernel, {}, approvals=approvals, tenancy=tenancy, open_api=False, internal_tenant=False)
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

    yield SimpleNamespace(call=call, accs=accs, svc=svc, approvals=approvals, factory=factory)
    release.set()
    httpd.shutdown()
    svc.close_all()
    code_http.configure(None)


def _tool(api, sid, name, arguments=None, who="ana"):
    return api.call("POST", f"/v1/code/sessions/{sid}/tools/call", {"name": name, "arguments": arguments or {}},
                    who=who)


def test_skills_trust_route_and_tools_call(api):
    s, a = api.call("POST", "/v1/code/sessions", {"repo": {"url": "https://git.example.org/a.git"}}, who="ana")
    assert s == 201 and a["skills"]["project"] == "untrusted" and a["skills"]["count"] == 1
    sha = a["skills"]["sha256"]
    url = f"/v1/code/sessions/{a['id']}/skills/trust"
    for who in ("ben", "cam"):
        assert api.call("POST", url, {"sha256": sha}, who=who)[0] == 404
    s, err = api.call("POST", url, {"sha256": "0" * 64}, who="ana")
    assert s == 409 and err["error"]["code"] == "skills_changed"
    status, body = _tool(api, a["id"], "skill_list")
    assert status == 200 and json.loads(body["content"])["skills"] == []
    s, out = api.call("POST", url, {"sha256": sha}, who="ana")
    assert s == 200 and out == {"thread_id": a["id"], "root": "/workspace", "sha256": sha, "trusted": True}
    assert api.call("GET", f"/v1/code/sessions/{a['id']}", who="ana")[1]["skills"]["project"] == "trusted"
    status, body = _tool(api, a["id"], "skill_list")
    assert status == 200 and [x["name"] for x in json.loads(body["content"])["skills"]] == ["lint"]
    status, body = _tool(api, a["id"], "skill_view", {"name": "lint"})
    assert status == 200 and not body["is_error"] and "make lint" in json.loads(body["content"])["content"]
    for who in ("ben", "cam"):
        assert _tool(api, a["id"], "skill_list", who=who)[0] == 404


def test_subagents_status_and_cancel_by_tools_call(api):
    a = api.call("POST", "/v1/code/sessions", {"mode": "accept_edits"}, who="ana")[1]
    assert a["subagents"] == {"limit": 3, "live": 0}
    thread = f"{api.accs['ana'].tenant_id}:{a['id']}"
    status, body = _tool(api, a["id"], "delegate", {"tasks": [{"goal": "g"}]})
    assert status == 403 and body["error"]["code"] == "tool_not_allowed"  # déléguer reste au modèle
    with tenant_scope(api.accs["ana"].tenant_id):
        out = api.svc.subagent_tools().call("delegate", {"tasks": [{"goal": "long"}], "isolate": False,
                                                         "background": True},
                                            ToolContext(thread_id=thread, turn_index=1, channel="api",
                                                        sender=api.accs["ana"].account_id))
    child = json.loads(out.content)["children"][0]
    assert api.call("GET", f"/v1/code/sessions/{a['id']}", who="ana")[1]["subagents"]["live"] == 1
    status, body = _tool(api, a["id"], "delegate_status")
    assert status == 200 and [c["child_id"] for c in json.loads(body["content"])["children"]] == [child["child_id"]]
    assert _tool(api, a["id"], "delegate_status", {"wait_s": 5})[0] == 400  # le verrou du fil serait tenu
    for who in ("ben", "cam"):
        assert _tool(api, a["id"], "delegate_status", who=who)[0] == 404
        assert _tool(api, a["id"], "delegate_cancel", who=who)[0] == 404
    # carte posée sur le fil de l'enfant : listée à ana seulement, même locataire ou pas
    card_turn = Turn(thread_id=child["thread_id"], index=0, input=InputCheck(text=""), channel="subagent",
                     sender=api.accs["ana"].account_id)
    with tenant_scope(api.accs["ana"].tenant_id):
        card = api.approvals.request(card_turn, ToolCall(id="c", name="write_file", arguments={}),
                                     Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="r"), None)
    assert [c["id"] for c in api.call("GET", "/v1/approvals", who="ana")[1]] == [card.id]
    assert api.call("GET", "/v1/approvals", who="ben")[1] == []
    assert api.call("GET", f"/v1/approvals/{card.id}", who="ben")[0] == 404
    status, body = _tool(api, a["id"], "delegate_cancel", {"ids": [child["child_id"]]})
    assert status == 200 and json.loads(body["content"])["cancelled"] == [child["child_id"]]
    assert api.factory.kernels[0].cancelled == [child["thread_id"]]


def test_trust_route_is_404_when_the_flag_is_off(cluster, fake_upstream):
    cfg = CodeConfig.from_config(code_block(cluster.workspace_root, project_skills=False, max_subagents=0),
                                 lokkal=False)
    svc = CodeSessions(cfg=cfg, store=MemoryCodeStore(), cloner=repo_cloner)
    try:
        with tenant_scope("tn_aaaaaaaaaaaa"):
            s = svc.create("us_aaaaaaaaaaa1", {})
        assert "skills" not in s and "subagents" not in s
        assert not os.path.exists(os.path.join(svc.cfg.state_dir, "tn_aaaaaaaaaaaa", "us_aaaaaaaaaaa1", "subagents"))
    finally:
        svc.close_all()
