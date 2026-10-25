"""Routes /v1/packs sur le vrai `make_handler` (drapeau `packs_web`) : session du compte, locataire fixé, un compte ne
voit ni ne touche les packs ou les fils d'un autre ; drapeau éteint = 404 comme avant."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from nokker.agents.hub import AgentsHub
from nokker.agents.memory import InMemoryAgents
from nokker.api.server import make_handler
from nokker.approvals.memory import InMemoryApprovals
from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.store import MemoryTenancy
from nokker.threads.memory import InMemoryThreads
from nokker.tools import skills as sk
from nokker.tools.packs import builtin_effects
from nokker.webpacks import http as packs_http
from nokker.webpacks.service import MemoryPackStore, WebPacks
from tests.unit.api.test_server import make_kernel
from tests.unit.team.test_http import _session
from tests.unit.tools.test_packs import safe_guard


class _InertEngine:
    def drop_card(self, approval_id, why):
        pass


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(sk, "_guard_module", lambda: safe_guard())
    tenancy = MemoryTenancy()
    accs = {k: tenancy.create_account(f"{k}@example.org") for k in ("alice", "bob")}
    approvals = InMemoryApprovals()
    agents = AgentsHub(InMemoryAgents(), rule_effect=builtin_effects().get)
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=lambda *a: None,
                    known_users=frozenset(a.account_id for a in accs.values()), spawn=lambda fn: fn())
    hub.agents_hub = agents
    svc = WebPacks(store=MemoryPackStore(), hub=hub)
    packs_http.configure(svc)
    handler = make_handler(make_kernel(), {}, approvals=approvals, agents=agents, hub=hub, tenancy=tenancy,
                           open_api=False, internal_tenant=False)
    agents.engine = _InertEngine()  # après le câblage : un moteur inerte, pour que les agents programmés se proposent
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tokens = {k: _session(approvals, a.account_id) for k, a in accs.items()}
    port = httpd.server_address[1]

    def call(method, path, body=None, who=None):
        h = {"Content-Type": "application/json"}
        if who:
            h["Authorization"] = f"Bearer {tokens[who]}"
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, json.loads(raw) if raw else None

    def thread_of(who):
        acc = accs[who]
        with tenant_scope(acc.tenant_id):
            t = hub.create_thread("Packs", acc.account_id)
        return t.id.split(":", 1)[1] if ":" in t.id else t.id  # identifiant vu par le client

    yield call, thread_of, accs, svc, hub
    httpd.shutdown()
    packs_http.configure(None)


def test_requires_a_session(world):
    call, *_ = world
    assert call("GET", "/v1/packs")[0] == 401
    assert call("POST", "/v1/packs/mail/install", {})[0] == 401


def test_catalog_install_detail_remove(world):
    call, thread_of, accs, svc, hub = world
    status, cat = call("GET", "/v1/packs", who="alice")
    assert status == 200 and {p["name"] for p in cat["packs"]} >= {"mail", "social", "mcp-builder"}
    thread = thread_of("alice")
    status, out = call("POST", "/v1/packs/mail/install", {"thread_id": thread}, who="alice")
    assert status == 200 and out["installed"] == "mail" and len(out["proposed"]) == 2, out
    status, detail = call("GET", "/v1/packs/mail", who="alice")
    assert status == 200 and detail["installed"] is True and detail["install"]["templates"] == ["inbox-triage",
                                                                                                 "morning-brief"]
    status, gone = call("DELETE", f"/v1/packs/mail?thread_id={thread}", who="alice")
    assert status == 200 and gone["removed"] == "mail" and len(gone["drafts_closed"]) == 2
    assert call("DELETE", "/v1/packs/mail", who="alice")[1]["already_removed"] is True


def test_tenant_control(world):
    call, thread_of, accs, svc, hub = world
    bob_thread = thread_of("bob")
    assert call("POST", "/v1/packs/mail/install", {}, who="alice")[0] == 200
    # bob ne voit pas le pack d'alice et ne le retire pas
    assert not next(p for p in call("GET", "/v1/packs", who="bob")[1]["packs"] if p["name"] == "mail")["installed"]
    assert call("DELETE", "/v1/packs/mail", who="bob")[1]["already_removed"] is True
    assert call("GET", "/v1/packs/mail", who="alice")[1]["installed"] is True
    # alice ne pose pas de cartes dans le fil de bob (le fil n'existe pas pour elle)
    status, body = call("POST", "/v1/packs/mail/propose", {"thread_id": bob_thread}, who="alice")
    assert status == 404 and body["error"]["code"] == "unknown_thread"
    status, body = call("POST", "/v1/packs/social/install", {"thread_id": bob_thread}, who="alice")
    assert status == 404


def test_errors(world):
    call, *_ = world
    status, body = call("POST", "/v1/packs/mcp-builder/install", {}, who="alice")
    assert status == 409 and body["error"]["code"] == "machine_only" and "machine" in body["error"]
    assert call("POST", "/v1/packs/nope/install", {}, who="alice")[0] == 404
    assert call("POST", "/v1/packs/mail/install", {"handoff": "yes"}, who="alice")[0] == 400
    assert call("POST", "/v1/packs/mail/install", {"bind": {"rm": "x"}}, who="alice")[0] == 400
    assert call("POST", "/v1/packs/mail/propose", {"thread_id": "x"}, who="alice")[0] == 404  # pas installé
    assert call("PUT", "/v1/packs/mail", {}, who="alice")[0] in (404, 405)


def test_flag_off_is_a_404(world):
    call, *_ = world
    packs_http.configure(None)
    assert call("GET", "/v1/packs", who="alice")[0] == 404
