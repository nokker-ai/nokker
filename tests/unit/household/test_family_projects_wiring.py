"""Projets du foyer câblés dans le VRAI serveur (config de la bêta + `household_minors` + `family_projects`), vrai
Handler, vrai noyau : un tour scripté propose puis écrit → UNE carte `family:project`, rien dans le foyer avant ; les
routes du mur (GET projets, GET tâches, POST coche) et celles du brouillon (PATCH) passent par l'aiguillage du serveur
(feedback-routes-par-le-serveur-http), avec les droits d'une enfant et d'un voisin hors foyer. Drapeau éteint : 404,
aucun outil."""
from __future__ import annotations

import json

import pytest

from tests.unit.tenancy.test_isolation_suite import FakeBot, _session  # noqa: F401


def _rig(monkeypatch, *, projects: bool):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from pathlib import Path

    from bench.conv.runner import ScriptedModel
    from nokker.api.events import EventBus
    from nokker.api.server import (
        build_connector_store,
        build_hub,
        build_kernel,
        configure_family_projects,
        configure_household,
        configure_team,
        make_handler,
    )
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.config import load_config
    from nokker.connectors.crypto import SecretBox
    from nokker.connectors.provider import ConnectorProvider
    from nokker.core.turn import Tier
    from nokker.tenancy.store import MemoryTenancy

    root = __import__("pathlib").Path(__file__).resolve().parents[3]
    for var in ("NOKKER_PG_DSN", "API_SERVER_KEY", "NOKKER_PG_OWNER_DSN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NOKKER_CONNECTOR_KEY", SecretBox.generate_key())
    config = load_config(str(Path(root) / "deploy/beta/core-config.yaml"))
    config.prefix_path = str(Path(root) / "bench/conv/fixtures/prefix.md")
    config.previews_enabled = False
    config.admin_enabled = False
    config.team = config.household = config.household_minors = True
    config.household_minors_mode = "all"
    config.family_projects = projects
    tenancy = MemoryTenancy()
    p, m, o = (tenancy.create_account(f"{n}@example.org") for n in ("payeur", "enfant", "voisin"))
    bus = EventBus()
    approvals = InMemoryApprovals(on_event=bus.publish)
    hub = build_hub(config, bus)
    kernel = build_kernel(config, approvals=approvals, hub=hub,
                          connectors=ConnectorProvider(build_connector_store(config), tenancy.account_for_tenant))
    model = ScriptedModel()
    kernel.providers = {Tier.SHIP: model, Tier.DEEP: model}
    configure_team(config, tenancy, scrub=kernel.invariants.scrub_output)
    configure_household(config, kernel, tenancy, approvals)
    configure_family_projects(config, kernel, tenancy)
    handler = make_handler(kernel, config.api_key_map(), None, approvals=approvals, events=bus, hub=hub,
                           known_users=config.known_users(), tenancy=tenancy, open_api=config.dev_open_api,
                           internal_tenant=config.internal_tenant)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tokens = {"P": _session(approvals, p.account_id), "M": _session(approvals, m.account_id),
              "O": _session(approvals, o.account_id)}
    port = httpd.server_address[1]

    def call(method, path, body=None, who=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        if who:
            h["Authorization"] = f"Bearer {tokens[who]}"
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else None)

    def chat(text, who, actions, session="s1"):
        model.load(actions, "J'attends ta décision sur la carte.")
        return call("POST", "/v1/chat/completions", {"model": "nokker", "messages": [{"role": "user", "content": text}]},
                    who=who, headers={"X-Session-Id": session})

    class W:
        pass

    w = W()
    w.__dict__.update(call=call, chat=chat, p=p, m=m, o=o, kernel=kernel, approvals=approvals, tenancy=tenancy,
                      httpd=httpd)
    return w


def _close(w):
    from nokker.household import http as household_http
    from nokker.household.projects import http as projects_http
    from nokker.team import http as team_http

    w.httpd.shutdown()
    for mod in (household_http, team_http, projects_http):
        mod.configure(None)


def _household_with_child(w):
    from nokker.household.model import consent_text as text

    assert w.call("POST", "/v1/household", {"name": "Maison"}, who="P")[0] == 201
    st, inv = w.call("POST", "/v1/household/invitations", {"email": w.m.email}, who="P")
    assert st == 201
    assert w.call("POST", f"/v1/household/invitations/{inv['id']}/accept", {}, who="M")[0] == 200
    st, row = w.call("PUT", f"/v1/household/members/{w.m.account_id}/profile",
                     {"profile": "child", "attestation": {"version": text("fr").sha256, "lang": "fr",
                                                          "legal_guardian": True}}, who="P")
    assert st == 200, row


@pytest.fixture
def fam(monkeypatch):
    w = _rig(monkeypatch, projects=True)
    try:
        _household_with_child(w)
        yield w
    finally:
        _close(w)


def test_family_projects_wired_in_the_real_server(fam):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from nokker.core.ports import ToolContext
    from nokker.household.projects import http as projects_http
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    w = fam
    svc = projects_http.service()
    ids = iter(range(1, 1000))
    svc.new_id = lambda prefix: f"{prefix}{next(ids):016d}"
    svc.clock = lambda: datetime(2026, 11, 7, 10, 0, tzinfo=ZoneInfo("Europe/Zurich")).timestamp()
    child = w.m.email.split("@")[0]  # prénom montré : partie locale de l'adresse (aucun prénom saisi)
    payer = w.p.email.split("@")[0]
    with tenant_scope(w.p.tenant_id):
        assert w.kernel.tools.spec("family_project_commit") is not None
    with tenant_scope(w.m.tenant_id):  # enfant : ses tâches et la coche, rien d'autre
        assert w.kernel.tools.spec("family_project_tasks") is not None
        assert w.kernel.tools.spec("family_project_task_done") is not None
        assert w.kernel.tools.spec("family_project_propose") is None
    with tenant_scope(INTERNAL_TENANT):
        assert w.kernel.tools.spec("family_project_tasks") is None
    plan = {"title": "Rentrée de janvier", "deadline": "2026-12-20", "members": [child], "lines": [
        {"title": "Couvrir les cahiers", "assignee": child, "due": "2026-11-14"},
        {"title": "Acheter le sac", "assignee": payer, "due": "2026-11-09"}]}
    draft = "pd_0000000000000001"
    st, _ = w.chat("prépare la rentrée", "P", [
        {"tool": "family_project_propose", "args": plan},
        {"tool": "family_project_commit", "args": {"draft_id": draft, "revision": 1}}])
    assert st == 200
    with tenant_scope(w.p.tenant_id):
        [card] = [c for c in w.approvals.pending() if c.tool_name == "family_project_commit"]
    assert card.rule == "family:project" and card.strength == "standard"
    assert w.call("GET", "/v1/household/projects", who="P")[1]["projects"] == []  # rien avant la carte
    # Édition par la carte web (PATCH) : la révision monte, la carte de la révision 1 n'écrit plus rien.
    st, d = w.call("PATCH", f"/v1/household/project-drafts/{draft}/lines/2",
                   {"action": "update", "fields": {"due": "2026-11-10"}}, who="P")
    assert st == 200 and d["draft"]["revision"] == 2 and d["draft"]["committable"]
    assert w.call("GET", f"/v1/household/project-drafts/{draft}", who="M")[0] == 403
    assert w.call("GET", f"/v1/household/project-drafts/{draft}", who="O")[0] == 404
    ctx = ToolContext(thread_id="t", turn_index=0, channel="api", approval_id="ap_test")
    with tenant_scope(w.p.tenant_id):
        stale = w.kernel.tools.call("family_project_commit", {"draft_id": draft, "revision": 1}, ctx)
        assert stale.is_error and "révision 2 (pas 1)" in stale.content
        ok = w.kernel.tools.call("family_project_commit", {"draft_id": draft, "revision": 2}, ctx)
        assert not ok.is_error, ok.content
    # Mur : routes par le serveur.
    st, view = w.call("GET", "/v1/household/projects", who="P")
    assert st == 200 and [p["title"] for p in view["projects"]] == ["Rentrée de janvier"]
    assert view["projects"][0]["progress"] == {"done": 0, "total": 2, "overdue": 0}
    st, view = w.call("GET", "/v1/household/projects", who="M")
    assert st == 200 and [t["title"] for t in view["projects"][0]["tasks"]] == ["Couvrir les cahiers"]
    assert w.call("GET", "/v1/household/projects", who="O")[0] == 404
    st, mine = w.call("GET", "/v1/household/tasks", who="M")
    assert st == 200 and [t["title"] for t in mine["tasks"]] == ["Couvrir les cahiers"]
    assert w.call("GET", f"/v1/household/tasks?member={payer}", who="M")[0] == 403
    st, upcoming = w.call("GET", "/v1/household/tasks?due_before=2026-11-12", who="P")
    assert st == 200 and [t["title"] for t in upcoming["tasks"]] == ["Acheter le sac"]
    sac = upcoming["tasks"][0]["id"]
    cahiers = mine["tasks"][0]["id"]
    assert w.call("POST", f"/v1/household/tasks/{sac}/done", {}, who="M")[0] == 404
    st, done = w.call("POST", f"/v1/household/tasks/{cahiers}/done", {}, who="M")
    assert st == 200 and done["task"]["state"] == "done"
    st, nudge = w.call("POST", f"/v1/household/tasks/{sac}/nudge", {}, who="P")
    assert st == 409  # sa propre tâche : pas de relance
    assert w.call("POST", f"/v1/household/tasks/{cahiers}/nudge", {}, who="M")[0] == 403
    assert w.call("PUT", "/v1/household/project-settings", {"overdue_by_member": True}, who="M")[0] == 403
    assert w.call("PUT", "/v1/household/project-settings", {"overdue_by_member": True}, who="P") == \
        (200, {"overdue_by_member": True, "wall_ha": [], "wall_place": None})
    st, week = w.call("GET", "/v1/household/projects/weekly", who="P")
    assert st == 200 and week["projects"][0]["progress"]["done"] == 1
    # Le foyer garde ses propres routes.
    assert w.call("GET", "/v1/household", who="P")[0] == 200


def test_flag_off_routes_404_and_no_tools(monkeypatch):
    from nokker.household.projects import http as projects_http
    from nokker.tenancy.context import tenant_scope

    w = _rig(monkeypatch, projects=False)
    try:
        _household_with_child(w)
        assert not projects_http.enabled()
        for method, path in (("GET", "/v1/household/projects"), ("GET", "/v1/household/tasks"),
                             ("POST", "/v1/household/tasks/ht_x/done"),
                             ("PATCH", "/v1/household/project-drafts/pd_x/lines/1")):
            assert w.call(method, path, {} if method != "GET" else None, who="P")[0] == 404, path
        with tenant_scope(w.p.tenant_id):
            assert w.kernel.tools.spec("family_project_propose") is None
            assert w.kernel.tools.spec("family_project_tasks") is None
        assert w.call("GET", "/v1/household", who="P")[0] == 200
    finally:
        _close(w)
