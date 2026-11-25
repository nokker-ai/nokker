"""Vue en direct contre un faux `sites-chrome` aux formes EXACTES de nokker/v3-frames (fake_sites_chrome.py), par le
chemin de production des sites connectés : `sites/build.py::_mcp_driver_factory` → `McpBrowserDriver` →
`browserlive.sites.LiveCaller` → `_McpCaller` (vrai client MCP streamable HTTP)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nokker.browserlive import live as live_mod
from nokker.browserlive import sites as sites_live
from nokker.browserlive.frames import FrameStore
from nokker.browserlive.live import BrowserLive
from nokker.sites.build import _mcp_driver_factory
from nokker.tenancy.context import INTERNAL_TENANT
from tests.unit.browserlive.fake_sites_chrome import TOKEN, FakeSitesChrome


class Clock:
    def __init__(self):
        self.t = 2_000_000.0

    def __call__(self):
        return self.t


class Bus:
    def __init__(self):
        self.events = []

    def __call__(self, t, d):
        self.events.append((t, d))

    def of(self, t):
        return [d for k, d in self.events if k == t]


class Hub:
    def __init__(self):
        self.created, self.updated = [], []

    def create_kernel_bubble(self, thread_id, *, kind, title="", state=None, status="idle", actions=None):
        self.created.append((thread_id, kind, title, dict(state or {})))
        return SimpleNamespace(id="b1")

    def update_from_kernel(self, bubble_id, *, title=None, state=None, status=None, if_version=None):
        self.updated.append((bubble_id, dict(state or {}), status))


@pytest.fixture
def env(monkeypatch):
    fake = FakeSitesChrome()
    monkeypatch.setenv("NOKKER_SITES_BROWSER_URL", fake.base)
    monkeypatch.setenv("NOKKER_SITES_BROWSER_TOKEN", TOKEN)
    monkeypatch.setenv("NOKKER_SITES_SCREEN_BASE", "http://127.0.0.1:8120")
    clock, bus, hub = Clock(), Bus(), Hub()
    lv = BrowserLive(publish=bus, hub=hub, clock=clock, frames=FrameStore(clock=clock))
    live_mod.configure(lv)
    conn = SimpleNamespace(id="c_li1", account="local", label="LinkedIn")
    sites_live.note_thread("local", "th-demo", "local-owner")
    drv = _mcp_driver_factory()(conn, None)
    try:
        yield SimpleNamespace(fake=fake, lv=lv, bus=bus, hub=hub, clock=clock, drv=drv, conn=conn)
    finally:
        live_mod.configure(None)
        with sites_live._HINTS_LOCK:
            sites_live._HINTS.clear()
        fake.close()


def _tick(e, s=1.5):
    e.clock.t += s


def test_demo_path_frames_actions_sensitive_handoff_resume_end(env):
    e = env
    e.drv.goto("https://www.linkedin.com/login?trk=abc")
    sid = e.bus.of("browser.action")[0]["session_id"]
    nav = e.bus.of("browser.action")[0]
    assert nav["kind"] == "navigate" and nav["url_host"] == "www.linkedin.com"
    f = e.bus.of("browser.frame")[-1]
    assert f["frame_url"] and f["title"] == "Connexion | LinkedIn" and (f["w"], f["h"]) == (640, 400)
    assert "_audience" not in e.bus.events[0][1] and e.bus.events[0][1]["_tenant"] is None  # Lokkal : le locataire
    # bulle `browser` dans le fil du dernier appel sites_* ; tâche = libellé du connecteur
    assert e.hub.created == [("th-demo", "browser", "LinkedIn", {"session_id": sid, "state": "running",
                                                                  "task": "LinkedIn"})]  # au démarrage
    _tick(e)
    e.drv.type("e1", "alex@example.org")
    a = [x for x in e.bus.of("browser.action") if x["kind"] == "type"][-1]  # suivi de l'instantané (read)
    assert a["kind"] == "type" and a["target_label"] == "texte saisi (16 car.)" and a["cursor"] == {"x": 0.42, "y": 0.61}
    assert "alex@example.org" not in json.dumps(e.bus.events, ensure_ascii=False)
    # mot de passe : champ marqué par le coffre, frame sensible jamais capturée
    _tick(e)
    n_frames = e.lv.frames.count(sid)
    e.drv.mark_secret("e2")
    e.drv.type("e2", "hunter2-secret")
    f = e.bus.of("browser.frame")[-1]
    assert f["sensitive"] is True and f["frame_url"] is None and e.lv.frames.count(sid) == n_frames
    assert "hunter2-secret" not in json.dumps(e.bus.events, ensure_ascii=False)
    # CAPTCHA → passation ; takeover → cookie → écran sur l'origine du web Lokkal + path du ticket
    _tick(e)
    e.fake.block = {"kind": "captcha", "vendor": "arkose", "detail": "défi visuel"}
    e.fake.filled.clear()
    e.drv.click("e3")
    h = e.bus.of("browser.handoff")[-1]
    assert h["reason"] == "captcha" and h["session_id"] == sid
    assert e.hub.updated[-1][1]["state"] == "handoff"
    code = h["takeover_url"].rsplit("/", 1)[1]
    status, out = e.lv.takeover(code, INTERNAL_TENANT, "local-owner")
    assert status == 200
    assert e.lv.takeover(code, INTERNAL_TENANT, "local-owner")[0] == 410
    assert e.lv.screen(sid, out["cookie"]) == (302, "http://127.0.0.1:8120/sites/screen/c_li1/?ticket=tk123")
    # le titulaire rend la main → POST /s/{id}/resume chez sites-chrome, un seul browser.resumed
    assert e.lv.resume(sid, INTERNAL_TENANT, "local-owner")[0] == 200
    assert e.lv.resume(sid, INTERNAL_TENANT, "local-owner")[0] == 200
    assert ("POST", "/s/c_li1/resume") in e.fake.routes and len(e.bus.of("browser.resumed")) == 1
    # fin : browser_close du pilote → ended, captures purgées, bulle passée en ended
    e.drv.close()
    assert e.bus.of("browser.ended")[-1] == {"session_id": sid, "seq": e.bus.of("browser.ended")[-1]["seq"],
                                             "reason": "done", "_tenant": None}
    assert e.lv.frames.count(sid) == 0 and e.hub.updated[-1][1]["state"] == "ended"


def test_model_text_never_carries_the_image(env):
    e = env
    e.drv.goto("https://www.linkedin.com/feed/")
    gesture_text = e.drv._call("browser_click", {"ref": "e3", "snapshot": False})
    assert "ImageContent" not in gesture_text and gesture_text == "browser_click : ok"
    assert e.bus.of("browser.frame")  # la capture est passée à la vue en direct, pas au texte


def test_frames_off_falls_back_to_manager_frame(env):
    e = env
    e.fake.frames = False  # SC_FRAMES=0
    e.drv.goto("https://www.linkedin.com/feed/")
    assert any(r[1] == "/s/c_li1/frame" for r in e.fake.routes)
    assert e.bus.of("browser.frame")[-1]["frame_url"]


def test_block_cleared_by_the_site_resumes_on_its_own(env):
    e = env
    e.fake.block = {"kind": "2fa", "detail": "code"}
    e.drv.goto("https://www.linkedin.com/checkpoint/")
    assert e.bus.of("browser.handoff")[-1]["reason"] == "2fa"
    e.fake.block = None
    _tick(e)
    e.drv.click("e3")  # plus de nokker_block : l'humain a résolu
    assert len(e.bus.of("browser.resumed")) == 1
    assert ("POST", "/s/c_li1/resume") not in e.fake.routes  # déjà levé côté navigateur


def test_screen_ticket_only_reads_path_for_this_connector(env):
    ops = sites_live.SitesOps(env.fake.base, lambda: TOKEN, screen_origin=lambda: "https://pierre.nokker.app")
    assert ops.screen_url("c_li1") == "https://pierre.nokker.app/sites/screen/c_li1/?ticket=tk123"
    assert ops.frame("c_inactive") is None  # 404 session inactive


def test_resume_stays_idempotent_when_the_site_is_gone(env):
    e = env
    e.fake.block = {"kind": "login", "detail": "session expirée"}
    e.drv.goto("https://www.linkedin.com/login")
    sid = e.bus.of("browser.handoff")[-1]["session_id"]
    e.fake.active.clear()  # le site n'est plus actif : /resume rend 404
    assert e.lv.resume(sid, INTERNAL_TENANT, "local-owner") == (200, {"session_id": sid, "state": "running",
                                                                     "seq": e.bus.of("browser.resumed")[-1]["seq"]})
    assert e.lv.resume(sid, INTERNAL_TENANT, "local-owner")[1]["state"] == "running"
    assert ("POST", "/s/c_li1/resume") in e.fake.routes and len(e.bus.of("browser.resumed")) == 1
    nav = [a for a in e.bus.of("browser.action") if a["kind"] == "navigate"][-1]
    assert "cursor" not in nav  # absent pour navigate


def test_sites_snapshot_masks_a_value_typed_by_the_person(env):
    """sites-chrome renvoie (à tort) la valeur d'un code 2FA dans l'instantané : ni le pilote ni le modèle ne la voient."""
    e = env
    e.drv.goto("https://www.linkedin.com/checkpoint/")
    e.fake.title = "Vérification"
    orig = e.fake.tool

    def leaky(name, args):
        out = orig(name, args)
        if name == "browser_snapshot":
            out = {"content": [{"type": "text", "text": out["content"][0]["text"]
                                + '- textbox "Code de vérification" [ref=e8]: 552211\n'}]}
        return out

    e.fake.tool = leaky
    text = e.drv._call("browser_snapshot", {})
    assert "552211" not in text and '"Code de vérification" [ref=e8]: [masqué]' in text
    assert all(n.value != "552211" for n in e.drv.nodes())


def test_point_on_sites_chrome_uses_browser_box_and_its_frame_seq(env):
    from nokker.browserlive.point import BrowserPointSource
    from nokker.core.ports import ToolContext
    from nokker.tenancy.context import tenant_scope

    e = env
    e.drv.goto("https://www.linkedin.com/login")
    ctx = ToolContext(thread_id="th-demo", turn_index=0, channel="api", sender="local-owner")
    with tenant_scope(INTERNAL_TENANT):
        r = BrowserPointSource().call("browser_point", {"ref": "e3", "why": "Le bouton pour te connecter"}, ctx)
        bad = BrowserPointSource().call("browser_point", {"ref": "e2", "why": "x"}, ctx)
    assert not r.is_error, r.content
    ev = e.bus.of("browser.point")[-1]
    assert ev["box"] == {"x": 0.3, "y": 0.55, "w": 0.4, "h": 0.06} and ev["seq"] == e.bus.of("browser.frame")[-1]["frame_seq"]
    assert ("browser_box", {"ref": "e3", "scroll": True}) in e.fake.calls
    assert not [c for c in e.fake.calls if c[0] in ("browser_click", "browser_type")]  # aucun geste
    assert bad.is_error and len(e.bus.of("browser.point")) == 1
