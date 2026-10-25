"""Équipe (lot X17) sur le vrai `make_handler` : sessions d'appareil de quatre comptes (quatre locataires), routes
/v1/projects*, agents rattachés, routes de machine /v1/mirror/projects*, et drapeau éteint qui ne change rien."""
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
from nokker.cloud import http as cloud_http
from nokker.cloud.http import CloudLink
from nokker.cloud.link import LinkService, Signer
from nokker.team import http as team_http
from nokker.team.service import Team
from nokker.team.store import MemoryTeamStore
from nokker.tenancy.store import MemoryTenancy
from tests.unit.api.test_server import make_kernel
from tests.unit.approvals.conftest import gen_keypair, sign_p1363


def _session(approvals, user):
    priv, spki = gen_keypair()
    d = approvals.enroll_device(user=user, name=f"appareil {user}", public_key_spki=spki)
    approvals.confirm_device_cli(d.code)
    ch = approvals.login_challenge(d.id)
    return approvals.create_session(d.id, ch.canonical(), sign_p1363(priv, f"{ch.canonical()}.login".encode())).token


def _start(team_on: bool):
    tenancy = MemoryTenancy()
    accs = {k: tenancy.create_account(f"{k}@example.org") for k in ("owner", "maint", "mem", "view", "out")}
    approvals = InMemoryApprovals()
    agents = AgentsHub(InMemoryAgents())
    links, signer = LinkService(), Signer.generate()
    cloud_http.configure(CloudLink(
        links=links, signer=signer, identity=lambda h: None,
        tenant_of=lambda acc: getattr(tenancy.account(acc), "tenant_id", None), router_base="http://127.0.0.1:9/v1",
        router_key=lambda: "", mirror_allowed=lambda acc: True))
    team_http.configure(Team(store=MemoryTeamStore(), tenancy=tenancy, agents=agents,
                             mirror_allowed=lambda acc: True) if team_on else None)
    handler = make_handler(make_kernel(), {}, approvals=approvals, agents=agents, tenancy=tenancy, open_api=False,
                           internal_tenant=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tokens = {k: _session(approvals, a.account_id) for k, a in accs.items()}
    machines = {}
    for k in ("mem", "out"):
        d = links.device(f"poste-{k}")
        links.approve(d["user_code"], accs[k].account_id, accs[k].tenant_id)
        machines[k] = links.token(d["device_code"])["machine_token"]
    port = httpd.server_address[1]

    def call(method, path, body=None, who=None, token=None):
        h = {"Content-Type": "application/json"}
        if who or token:
            h["Authorization"] = f"Bearer {token or tokens[who]}"
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, json.loads(raw) if raw else None

    return httpd, call, accs, machines


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(team_http, "RATE_MAX", 10_000)  # la suite enchaîne des centaines d'écritures
    httpd, call, accs, machines = _start(True)
    yield call, accs, machines
    httpd.shutdown()
    team_http.configure(None)
    cloud_http.configure(None)


@pytest.fixture
def off():
    httpd, call, accs, machines = _start(False)
    yield call, accs, machines
    httpd.shutdown()
    cloud_http.configure(None)


def _project_with_members(call):
    st, p = call("POST", "/v1/projects", {"name": "Plateforme", "repos": ["github.com/acme/api"]}, who="owner")
    assert st == 201 and p["role"] == "owner"
    for who, role in (("maint", "maintainer"), ("mem", "member"), ("view", "viewer")):
        st, inv = call("POST", f"/v1/projects/{p['id']}/invitations", {"email": f"{who}@example.org", "role": role},
                       who="owner")
        assert st == 201
        st, mine = call("GET", "/v1/projects/invitations", who=who)
        assert st == 200 and [i["id"] for i in mine["invitations"]] == [inv["id"]]
        st, joined = call("POST", f"/v1/projects/invitations/{inv['id']}/accept", {}, who=who)
        assert st == 200 and joined["role"] == role
    return p["id"]


def test_routes_and_roles(on):
    call, accs, _ = on
    pid = _project_with_members(call)
    st, lst = call("GET", "/v1/projects", who="view")
    assert st == 200 and [p["id"] for p in lst["projects"]] == [pid]
    assert call("PUT", f"/v1/projects/{pid}/skills/lint", {"source": "official/dev/lint"}, who="maint")[0] == 200
    assert call("PUT", f"/v1/projects/{pid}/rules", {"deny": ["terminal(rm -rf *)"]}, who="maint")[0] == 200
    assert call("PUT", f"/v1/projects/{pid}/secrets/DB_URL", {"description": "préprod"}, who="maint")[0] == 200
    assert call("PATCH", f"/v1/projects/{pid}", {"mirror": True}, who="owner")[0] == 200
    st, members = call("GET", f"/v1/projects/{pid}/members", who="mem")
    assert st == 200 and len(members["members"]) == 4
    # viewer : lit tout, n'écrit rien (403, il sait que le projet existe)
    for path in ("", "/members", "/skills", "/rules", "/secrets", "/notes", "/agents"):
        assert call("GET", f"/v1/projects/{pid}{path}", who="view")[0] == 200, path
    for method, path, body in (("PATCH", "", {"name": "x"}), ("DELETE", "", None),
                               ("PUT", "/skills/x", {"source": "official/a/b"}), ("DELETE", "/skills/lint", None),
                               ("PUT", "/rules", {"deny": []}), ("PUT", "/secrets/X", {}),
                               ("DELETE", "/secrets/DB_URL", None), ("POST", "/invitations", {"email": "z@x.org"}),
                               ("PATCH", f"/members/{accs['mem'].account_id}", {"role": "viewer"}),
                               ("DELETE", f"/members/{accs['mem'].account_id}", None)):
        st, body_out = call(method, f"/v1/projects/{pid}{path}", body, who="view")
        assert st == 403 and body_out["error"]["code"] == "forbidden", (method, path)
    # rien n'a bougé
    st, p = call("GET", f"/v1/projects/{pid}", who="owner")
    assert p["name"] == "Plateforme" and p["secret_names"] == [{"name": "DB_URL", "description": "préprod"}]
    # valeur de secret refusée, règle qui élargirait refusée
    st, err = call("PUT", f"/v1/projects/{pid}/secrets/DB_URL", {"value": "postgres://u:p@h/db"}, who="maint")
    assert st == 400 and err["error"]["code"] == "secret_value_refused"
    st, err = call("PUT", f"/v1/projects/{pid}/rules", {"allow": ["terminal(*)"]}, who="maint")
    assert st == 400 and err["error"]["code"] == "rules_widen"


def test_non_member_gets_the_same_404_as_an_unknown_project(on):
    call, _, _ = on
    pid = _project_with_members(call)
    paths = ["", "/members", "/invitations", "/skills", "/rules", "/secrets", "/notes", "/notes/quarantine",
             "/agents", "/inconnu"]
    for path in paths:
        for method in ("GET", "PUT", "PATCH", "DELETE", "POST"):
            got = call(method, f"/v1/projects/{pid}{path}", {}, who="out")
            unknown = call(method, f"/v1/projects/pj_000000000000{path}", {}, who="out")
            assert got == unknown and got[0] == 404, (method, path, got)
    # un membre sur un chemin inconnu : 404 aussi
    assert call("GET", f"/v1/projects/{pid}/inconnu", who="mem")[0] == 404
    assert call("GET", "/v1/projects", who="out")[1]["projects"] == []
    # sans session : 401 ; identité inconnue : 401
    assert call("GET", "/v1/projects")[0] == 401
    assert call("GET", "/v1/projects", token="faux")[0] == 401


def test_agents_carry_their_project(on):
    call, _, _ = on
    pid = _project_with_members(call)
    st, a = call("POST", "/v1/agents", {"name": "veille", "role": "r", "project": pid}, who="mem")
    assert st == 201 and a["project"] == pid
    assert call("POST", "/v1/agents", {"name": "x", "role": "r", "project": pid}, who="view")[0] == 403
    assert call("POST", "/v1/agents", {"name": "x", "role": "r", "project": pid}, who="out")[0] == 404
    st, plain = call("POST", "/v1/agents", {"name": "seul", "role": "r"}, who="mem")
    assert st == 201 and plain["project"] is None  # §9.5 : champ toujours présent, nul hors projet
    st, lst = call("GET", f"/v1/projects/{pid}/agents", who="mem")
    assert [x["id"] for x in lst["agents"]] == [a["id"]]
    # les autres membres le voient, sans sa carte complète ; seul l'owner du projet (ou mem) peut le désarmer
    [seen] = call("GET", f"/v1/projects/{pid}/agents", who="maint")[1]["agents"]
    assert seen["id"] == a["id"] and seen["mine"] is False and "instructions" not in seen
    assert seen["owner"]["email"] == "mem@example.org"
    assert call("POST", f"/v1/projects/{pid}/agents/{a['id']}/disarm", who="maint")[0] == 403
    assert call("POST", f"/v1/projects/{pid}/agents/{a['id']}/disarm", who="out")[0] == 404
    st, d = call("POST", f"/v1/projects/{pid}/agents/{a['id']}/disarm", who="owner")
    assert st == 200 and d["agent"]["status"] == "paused" and d["agent"]["can_disarm"] is False
    st, ev = call("GET", f"/v1/projects/{pid}/audit", who="maint")
    assert st == 200 and ev["events"][0]["event"] == "agent_disarmed"
    assert call("GET", f"/v1/projects/{pid}/audit", who="mem")[0] == 403


def test_budget_routes(on):
    call, _, _ = on
    pid = _project_with_members(call)
    assert call("PUT", f"/v1/projects/{pid}/budget", {"daily": 5}, who="mem")[0] == 403
    st, b = call("PUT", f"/v1/projects/{pid}/budget", {"daily": 5, "monthly": 50}, who="maint")
    assert st == 200 and b["budget"] == {"daily": 5.0, "monthly": 50.0, "currency": "EUR"}
    st, v = call("GET", f"/v1/projects/{pid}/budget", who="view")
    assert st == 200 and v["budget"]["daily"] == 5.0  # sans ledger branché : rangé, pas décompté
    assert call("GET", f"/v1/projects/{pid}/budget", who="out")[0] == 404
    assert call("PUT", f"/v1/projects/{pid}/budget", {"weekly": 5}, who="owner")[0] == 400
    st, b = call("DELETE", f"/v1/projects/{pid}/budget", who="owner")
    assert st == 200 and b == {"budget": None}


def test_machine_routes(on):
    call, _, machines = on
    pid = _project_with_members(call)
    call("PUT", f"/v1/projects/{pid}/rules", {"mode": "default", "deny": ["terminal(rm -rf *)"]}, who="maint")
    st, view = call("GET", "/v1/mirror/projects", token=machines["mem"])
    assert st == 200 and view["projects"][0]["rules"]["deny"] == ["terminal(rm -rf *)"]
    assert view["projects"][0]["mirror"] is False
    note = {"name": "fact-ci", "kind": "fact", "title": "CI", "text": "Forgejo", "updated_at": 1_700_000_000.0,
            "locality": "both"}
    st, err = call("POST", f"/v1/mirror/projects/{pid}/notes", {"notes": [note]}, token=machines["mem"])
    assert st == 409 and err["error"]["code"] == "mirror_off"
    call("PATCH", f"/v1/projects/{pid}", {"mirror": True}, who="owner")
    st, r = call("POST", f"/v1/mirror/projects/{pid}/notes", {"notes": [note]}, token=machines["mem"])
    assert st == 200 and r["accepted"] == 1
    st, pulled = call("GET", f"/v1/mirror/projects/{pid}/notes?since=0", token=machines["mem"])
    assert st == 200 and pulled["notes"][0]["text"] == "Forgejo"
    st, web = call("GET", f"/v1/projects/{pid}/notes", who="view")
    assert st == 200 and web["notes"][0]["name"] == "fact-ci"
    # la machine d'un non-membre : la même réponse qu'un projet inconnu
    for method, body in (("GET", None), ("POST", {"notes": [note]})):
        got = call(method, f"/v1/mirror/projects/{pid}/notes", body, token=machines["out"])
        unknown = call(method, "/v1/mirror/projects/pj_000000000000/notes", body, token=machines["out"])
        assert got == unknown and got[0] == 404
    assert call("GET", "/v1/mirror/projects", token=machines["out"])[1] == {"projects": []}
    assert call("GET", "/v1/mirror/projects", token="faux")[0] == 401


def test_writes_are_rate_limited_per_account(on):
    call, _, _ = on
    team_http.RATE_MAX = 3
    assert [call("POST", "/v1/projects", {"name": f"p{i}"}, who="out")[0] for i in range(4)] == [201, 201, 201, 429]
    assert call("GET", "/v1/projects", who="out")[0] == 200  # la lecture n'est pas comptée
    assert call("POST", "/v1/projects", {"name": "autre"}, who="mem")[0] == 201  # par compte


def test_flag_off_changes_nothing(off):
    call, _, machines = off
    # /v1/projects* répond exactement comme un chemin que personne ne sert, avec ou sans session
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        for path in ("/v1/projects", "/v1/projects/pj_000000000000", "/v1/projects/invitations"):
            for who in ("owner", None):
                got = call(method, path, {}, who=who)
                assert got == call(method, path.replace("projects", "zzprojects"), {}, who=who), (method, path, who)
                assert got[0] in (401, 404), got
    # agents : le champ `project` envoyé est ignoré (toujours nul : §9.5, champ présent, arbitrage dev du 25.10)
    st, a = call("POST", "/v1/agents", {"name": "veille", "role": "r", "project": "pj_000000000000"}, who="mem")
    assert st == 201 and a["project"] is None
    st, lst = call("GET", "/v1/agents", who="mem")
    assert st == 200 and all(x["project"] is None for x in lst)
    # routes de machine : la même réponse qu'avant le lot
    before = call("GET", "/v1/mirror/inconnu", token=machines["mem"])
    assert call("GET", "/v1/mirror/projects", token=machines["mem"]) == before
    assert call("POST", "/v1/mirror/projects/pj_000000000000/notes", {}, token=machines["mem"]) == before
