"""Plan et incidents (lot X18) sur le vrai `make_handler` : drapeaux éteints = 404 comme avant ; allumés = routes
/v1/plan*, /v1/alerts*, /v1/incidents* sous l'identité d'un compte (deux comptes, deux locataires), alerte signée
reçue sans identité, rejeu refusé, rien d'un locataire visible de l'autre."""
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
from nokker.incidents import http as incidents_http
from nokker.incidents.service import Operate, sign
from nokker.incidents.store import MemoryIncidents
from nokker.plan import http as plan_http
from nokker.plan.service import Plan
from nokker.plan.store import MemoryPlan
from nokker.tenancy.store import MemoryTenancy
from tests.unit.api.test_server import make_kernel
from tests.unit.team.test_http import _session


def _start(on: bool):
    tenancy = MemoryTenancy()
    accs = {k: tenancy.create_account(f"{k}@example.org") for k in ("ana", "ben")}
    approvals = InMemoryApprovals()
    agents = AgentsHub(InMemoryAgents())
    plan_http.configure(Plan(store=MemoryPlan(), agents=agents) if on else None)
    incidents_http.configure(Operate(store=MemoryIncidents(), agents=agents) if on else None)
    handler = make_handler(make_kernel(), {}, approvals=approvals, agents=agents, tenancy=tenancy, open_api=False,
                           internal_tenant=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tokens = {k: _session(approvals, a.account_id) for k, a in accs.items()}
    port = httpd.server_address[1]

    def call(method, path, body=None, who=None, raw=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        if who:
            h["Authorization"] = f"Bearer {tokens[who]}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h, data=data)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                out = r.read()
                return r.status, json.loads(out) if out else None
        except urllib.error.HTTPError as e:
            out = e.read()
            return e.code, json.loads(out) if out else None

    return httpd, call


@pytest.fixture
def api():
    httpd, call = _start(True)
    yield call
    httpd.shutdown()
    plan_http.configure(None)
    incidents_http.configure(None)


def test_flags_off_change_nothing():
    httpd, call = _start(False)
    try:
        for method, path in (("GET", "/v1/plan/cards"), ("GET", "/v1/incidents"), ("POST", "/v1/alerts/as_0123456789ab"),
                             ("GET", "/v1/alerts/sources")):
            assert call(method, path, who="ana")[0] == 404
    finally:
        httpd.shutdown()


def test_plan_routes_and_tenant_isolation(api):
    assert api("GET", "/v1/plan/cards")[0] == 401
    s, goal = api("POST", "/v1/plan/cards", {"title": "Objectif", "intent": "but", "constraints": ["c1"]}, who="ana")
    assert s == 201 and goal["kind"] == "goal" and goal["project"] is None
    s, task = api("POST", "/v1/plan/cards", {"title": "Tâche", "parent_id": goal["id"]}, who="ana")
    assert s == 201 and task["kind"] == "milestone"
    assert api("PATCH", f"/v1/plan/cards/{task['id']}", {"status": "done"}, who="ana")[1]["status"] == "done"
    s, g = api("GET", f"/v1/plan/cards/{goal['id']}", who="ana")
    assert g["status"] == "done" and g["progress"]["ratio"] == 1.0
    s, err = api("PATCH", f"/v1/plan/cards/{goal['id']}", {"status": "todo"}, who="ana")
    assert s == 400 and err["error"]["code"] == "computed"
    s, ctx = api("GET", f"/v1/plan/cards/{task['id']}/context", who="ana")
    assert s == 200 and "c1" in ctx["block"]
    # l'autre compte (autre locataire) ne voit rien et ne touche rien
    assert api("GET", "/v1/plan/cards", who="ben")[1] == {"cards": []}
    assert api("GET", f"/v1/plan/cards/{goal['id']}", who="ben")[0] == 404
    assert api("DELETE", f"/v1/plan/cards/{goal['id']}", who="ben")[0] == 404
    assert api("POST", "/v1/plan/cards", {"title": "x", "parent_id": goal["id"]}, who="ben")[0] == 404
    # brief sans moteur Crew : 503, rien de créé
    assert api("POST", "/v1/plan/brief", {}, who="ana")[0] == 503
    assert api("GET", "/v1/plan/brief", who="ana")[1] == {"agent": None}


def test_signed_alert_over_http_and_incident_routes(api):
    s, src = api("POST", "/v1/alerts/sources", {"name": "prom", "format": "alertmanager"}, who="ana")
    assert s == 201 and src["secret"].startswith("whsec_") and src["url"] == f"/v1/alerts/{src['source']['id']}"
    assert "secret" not in json.dumps(api("GET", "/v1/alerts/sources", who="ana")[1]["sources"][0]).replace(
        "has_secret", "")
    body = json.dumps({"version": "4", "alerts": [{"status": "firing", "labels": {"alertname": "Down"},
                                                    "fingerprint": "0011223344556677"}]}).encode()
    url = src["url"]
    assert api("POST", url, raw=body)[0] == 401  # sans signature
    sig = sign(src["secret"], body)
    s, out = api("POST", url, raw=body, headers={"X-Nokker-Signature": sig})
    assert s == 202 and out["alerts"][0]["result"] == "opened"
    assert api("POST", url, raw=body, headers={"X-Nokker-Signature": sig})[0] == 409  # rejeu
    iid = out["alerts"][0]["incident_id"]
    s, lst = api("GET", "/v1/incidents?state=live", who="ana")
    assert s == 200 and [i["id"] for i in lst["incidents"]] == [iid] and "timeline" not in lst["incidents"][0]
    s, inc = api("GET", f"/v1/incidents/{iid}", who="ana")
    assert inc["timeline"][0]["kind"] == "opened" and inc["state"] == "open"
    assert api("GET", f"/v1/incidents/{iid}", who="ben")[0] == 404
    assert api("POST", f"/v1/incidents/{iid}/resolve", {"note": "x"}, who="ben")[0] == 404
    assert api("POST", f"/v1/incidents/{iid}/ack", who="ana")[1]["state"] == "in_progress"
    s, inc = api("POST", f"/v1/incidents/{iid}/resolve", {"note": "réparé"}, who="ana")
    assert s == 200 and inc["state"] == "resolved"
    assert api("POST", f"/v1/incidents/{iid}/resolve", {}, who="ana")[0] == 409
    assert api("DELETE", f"/v1/alerts/sources/{src['source']['id']}", who="ben")[0] == 404
    assert api("DELETE", f"/v1/alerts/sources/{src['source']['id']}", who="ana")[1] == {"deleted": True}
    assert api("POST", url, raw=body, headers={"X-Nokker-Signature": sign(src["secret"], body)})[0] == 404
