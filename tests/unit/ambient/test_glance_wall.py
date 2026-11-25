"""Écran mural (P10b, docs/AMBIENT.md §6) : une vue glance posée dans une pièce commune. Le filtre est dans le NOYAU :
sur un mur, jamais le contenu d'une carte (compte seulement), incidents réduits à {id, title}, brief et titre du prochain
événement seulement si le titulaire l'a choisi (`show_brief`, par appareil, session complète du titulaire). Le prochain
événement vient des agendas connectés du TITULAIRE, dans son locataire, jamais d'un autre ni du téléphone."""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from nokker.ambient import glance as gl
from nokker.config import NokkerConfig
from nokker.core.ports import Effect, ToolResult, ToolSpec
from tests.unit.ambient.test_glance import _call


class FakeCalendar:
    """Un agenda connecté : `calendar_events` rend le JSON clôturé, comme les vrais connecteurs."""

    def __init__(self, events):
        self.events, self.calls = events, []

    def specs(self):
        return [ToolSpec(name="calendar_events", description="x", parameters={}, effect=Effect.READ)]

    def call(self, name, arguments, ctx):
        from nokker.connectors.calendar import fence_events

        self.calls.append((name, dict(arguments), ctx.sender))
        return ToolResult(call_id="", name=name, content=fence_events({"count": len(self.events),
                                                                        "events": self.events}))


def _iso(dt):
    return dt.isoformat(timespec="minutes")


@pytest.fixture()
def cloud(monkeypatch):
    from nokker.api.server import configure_ambient, make_handler
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.tenancy.store import MemoryTenancy
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.team.test_http import _session

    monkeypatch.delenv("NOKKER_PG_DSN", raising=False)
    configure_ambient(NokkerConfig(ambient={"glance": {"enabled": True}}),
                      SimpleNamespace(invariants=SimpleNamespace(scrub_output=lambda s: s)))
    tenancy = MemoryTenancy()
    ana, cam = tenancy.create_account("ana@example.org"), tenancy.create_account("cam@example.org")
    soon = datetime.now(UTC) + timedelta(hours=2)
    cals = {ana.tenant_id: FakeCalendar([{"summary": "Dentiste de Lina", "start": _iso(soon + timedelta(hours=1)),
                                          "all_day": False},
                                         {"summary": "Réunion parents", "start": _iso(soon), "all_day": False},
                                         {"summary": "Loin", "start": _iso(soon + timedelta(days=3)),
                                          "all_day": False}]),
            cam.tenant_id: FakeCalendar([{"summary": "Secret de cam", "start": _iso(soon - timedelta(hours=1)),
                                          "all_day": False}])}
    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    kernel.tools.connectors = SimpleNamespace(sources_for=lambda tid: [cals[tid]] if tid in cals else [])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {}, approvals=approvals, tenancy=tenancy,
                                                               open_api=False, internal_tenant=False))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    sa, sc = _session(approvals, ana.account_id), _session(approvals, cam.account_id)
    yield SimpleNamespace(port=port, ana=ana, cam=cam, sa=sa, sc=sc, cals=cals, approvals=approvals)
    httpd.shutdown()


def pair(c, session, surface=None):
    body = {"name": "Cuisine"} | ({"surface": surface} if surface else {})
    st, offer = _call(c.port, "/v1/glance/pair", token=session, body=body)
    assert st == 201, offer
    st, claimed = _call(c.port, "/v1/glance/claim", body={"code": offer["code"]})
    assert st == 201
    return claimed["token"], claimed["device"]["id"]


def test_a_wall_hides_the_title_by_default_and_shows_it_once_the_owner_allows(cloud):
    c = cloud
    token, did = pair(c, c.sa, "wall")
    dev = next(d for d in _call(c.port, "/v1/glance/devices", token=c.sa)[1]["devices"] if d["id"] == did)
    assert dev["surface"] == "wall" and dev["show_brief"] is False
    st, view = _call(c.port, "/v1/glance", token=token)
    assert st == 200 and view["surface"] == "wall" and view["show_brief"] is False
    assert view["next_event"]["all_day"] is False and "title" not in view["next_event"]  # titre jamais sorti
    assert "Réunion" not in json.dumps(view) and view["brief"] is None and "brief" in view["unavailable"]
    assert view["approvals"] == [] and view["approvals_count"] == 0
    # le titulaire allume le réglage, depuis SA session complète
    st, out = _call(c.port, f"/v1/glance/devices/{did}", token=c.sa, body={"show_brief": True}, method="PATCH")
    assert st == 200 and out["show_brief"] is True
    view = _call(c.port, "/v1/glance", token=token)[1]
    assert view["next_event"]["title"] == "Réunion parents"  # le plus proche des 24 h, pas le plus loin


def test_only_the_owner_with_a_full_session_can_change_show_brief(cloud):
    c = cloud
    token, did = pair(c, c.sa, "wall")
    st, out = _call(c.port, f"/v1/glance/devices/{did}", token=token, body={"show_brief": True}, method="PATCH")
    assert st == 403 and out["error"]["code"] == "glance_scope"  # jamais par le jeton nkg_
    assert _call(c.port, f"/v1/glance/devices/{did}", token=c.sc, body={"show_brief": True}, method="PATCH")[0] == 404
    assert _call(c.port, f"/v1/glance/devices/{did}", body={"show_brief": True}, method="PATCH")[0] == 401
    st, out = _call(c.port, f"/v1/glance/devices/{did}", token=c.sa, body={"show_brief": "oui"}, method="PATCH")
    assert st == 400 and out["error"]["code"] == "bad_request"
    glasses_token, glasses = pair(c, c.sa)
    st, out = _call(c.port, f"/v1/glance/devices/{glasses}", token=c.sa, body={"show_brief": False}, method="PATCH")
    assert st == 400 and out["error"]["code"] == "not_wall"
    assert _call(c.port, "/v1/glance/pair", token=c.sa, body={"surface": "plafond"})[0] == 400
    assert _call(c.port, "/v1/glance", token=token)[1]["show_brief"] is False  # rien n'a changé


def test_next_event_comes_from_the_owners_calendars_only_and_glasses_keep_everything(cloud):
    c = cloud
    token, _ = pair(c, c.sa)  # lunettes d'ana
    view = _call(c.port, "/v1/glance", token=token)[1]
    assert view["surface"] == "glasses" and view["show_brief"] is True
    assert view["next_event"]["title"] == "Réunion parents" and "Secret de cam" not in json.dumps(view)
    assert c.cals[c.cam.tenant_id].calls == [] and c.cals[c.ana.tenant_id].calls[-1][2] == c.ana.account_id
    cam_token, _ = pair(c, c.sc)
    assert _call(c.port, "/v1/glance", token=cam_token)[1]["next_event"]["title"] == "Secret de cam"
    c.cals[c.ana.tenant_id].events = []
    assert "next_event" not in _call(c.port, "/v1/glance", token=token)[1]  # aucun événement : champ absent


def test_wall_view_counts_cards_and_reduces_incidents():
    view = {"brief": {"agent": {"id": "ag"}}, "brief_last_run": {"id": "r"}, "unavailable": ["brief_last_run"],
            "approvals": [{"id": "ap1", "tool_name": "pay_request", "arguments": {"amount": 99}}],
            "incidents": [{"id": "in1", "title": "Disque plein", "timeline": ["x"], "severity": "sev2"}],
            "next_event": {"start": "2026-11-07T09:00+01:00", "all_day": False, "title": "Dentiste"}}
    wall = gl.wall_view(view, SimpleNamespace(surface="wall", show_brief=False))
    assert wall["approvals"] == [] and wall["approvals_count"] == 1 and "pay_request" not in json.dumps(wall)
    assert wall["incidents"] == [{"id": "in1", "title": "Disque plein"}]
    assert wall["brief"] is None and wall["brief_last_run"] is None and wall["unavailable"] == ["brief"]
    assert wall["next_event"] == {"start": "2026-11-07T09:00+01:00", "all_day": False}
    shown = gl.wall_view(view, SimpleNamespace(surface="wall", show_brief=True))
    assert shown["next_event"]["title"] == "Dentiste" and shown["brief"] == view["brief"] and shown["approvals"] == []
    glasses = gl.wall_view(view, SimpleNamespace(surface="glasses", show_brief=True))
    assert glasses["approvals"] == view["approvals"] and glasses["incidents"] == view["incidents"]
    assert glasses["approvals_count"] == 1


def test_next_event_never_reads_the_phone_calendar_and_a_failure_is_reported():
    from nokker.devices.calendar import DeviceCalendarSource

    phone = DeviceCalendarSource.__new__(DeviceCalendarSource)  # jamais appelée : la lecture réveillerait l'app
    now = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)
    allday = FakeCalendar([{"summary": "Fête", "start": "2026-11-07", "all_day": True}])
    assert gl.next_event([phone, allday], "us_a", "tn_a", now) == {"start": "2026-11-07", "all_day": True,
                                                                  "title": "Fête"}

    class Broken(FakeCalendar):
        def call(self, name, arguments, ctx):
            return ToolResult(call_id="", name=name, content="agenda injoignable", is_error=True)

    with pytest.raises(RuntimeError):
        gl.next_event([Broken([])], "us_a", "tn_a", now)
    assert gl.next_event([], "us_a", "tn_a", now) is None


def test_a_published_screen_reaches_only_its_owners_wall_after_the_wall_filter(cloud):
    """V7 (docs/SCREENS.md) : l'écran publié d'ana sort sur SON mur, rendu après le filtre du mur (pas de titre
    d'événement sans show_brief) ; jamais sur ses lunettes, jamais sur le mur de cam. Drapeau éteint : aucun champ."""
    from nokker.core.ports import ToolContext
    from nokker.screens import wiring

    c = cloud
    token, _ = pair(c, c.sa, "wall")
    assert "screen" not in _call(c.port, "/v1/glance", token=token)[1]  # écrans éteints : rien
    svc = wiring.configure({"screens": {"enabled": True}, "glance": {"enabled": True}})
    try:
        svc.tenant = lambda: c.ana.tenant_id
        r = svc.draft(ToolContext(thread_id="th_a", turn_index=0, channel="api", sender=c.ana.account_id),
                      {"title": "Cuisine", "blocks": [{"type": "next_event"}, {"type": "text", "text": "Bon appétit"}]})
        sid = r.content.split("écran ", 1)[1].split(" ", 1)[0]
        token2, _ = pair(c, c.sa, "wall")  # au plus 2 lectures par minute et par jeton : un mur par lecture
        assert _call(c.port, "/v1/glance", token=token2)[1]["screen"] is None  # brouillon : rien sur le mur
        svc.approvals = SimpleNamespace(request=lambda turn, call, verdict, note: SimpleNamespace(
            id="ap_1", arguments=call.arguments, status="pending"))
        svc.bubble_action(c.ana.tenant_id, {"screen_id": sid}, "publish", c.ana.account_id)
        card = SimpleNamespace(arguments={"screen_id": sid, "tenant_id": c.ana.tenant_id, "version": 1,
                                          "sha256": __import__("nokker.screens.service", fromlist=["x"]).spec_sha(
                                              svc.store.get(c.ana.tenant_id, sid).spec)}, status="approved")
        svc.publish_handler()(card)
        token3, _ = pair(c, c.sa, "wall")
        view = _call(c.port, "/v1/glance", token=token3)[1]
        assert view["screen"]["id"] == sid and "Bon appétit" in view["screen"]["html"]
        assert "Réunion" not in view["screen"]["html"] and "<script" not in view["screen"]["html"]
        # le cadre servi par le noyau : sans jeton, sous SA CSP (sandbox sans allow-scripts), le même HTML
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{c.port}{view['screen']['frame_url']}") as r:
            csp = r.headers["Content-Security-Policy"]
            assert r.status == 200 and r.read().decode() == view["screen"]["html"]
        assert csp.startswith("sandbox; default-src 'none'; style-src 'sha256-") and "allow-scripts" not in csp
        assert "font-src 'self'" in csp
        with urllib.request.urlopen(f"http://127.0.0.1:{c.port}/v1/screens/fonts/cinzel-latin-600-normal.woff2") as r:
            assert r.status == 200 and r.headers["Content-Type"] == "font/woff2"
            assert r.headers["Access-Control-Allow-Origin"] == "*" and r.read(4) == b"wOF2"
        for name in ("../render.py", "nope.woff2", "LICENSE-cinzel.txt"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(f"http://127.0.0.1:{c.port}/v1/screens/fonts/{name}")
            assert e.value.code == 404
        bad = view["screen"]["frame_url"].replace("sig=", "sig=0")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{c.port}{bad}")
        assert e.value.code == 404
        glasses, _ = pair(c, c.sa)
        assert "screen" not in _call(c.port, "/v1/glance", token=glasses)[1]
        cam_wall, _ = pair(c, c.sc, "wall")
        assert _call(c.port, "/v1/glance", token=cam_wall)[1]["screen"] is None
    finally:
        wiring.configure({})
