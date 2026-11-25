"""`browser_point` (V3, docs/BROWSER-LIVE.md §8) : Nina montre du doigt l'élément dont elle parle — `browser_box` du
navigateur (forme sites-chrome 00814ab), capture fraîche publiée en frame, `browser.point {seq: frame_seq}` ; aucun
geste sur la page, refus sur cible sensible (noyau ou navigateur), page sensible (frame_seq null), passation ; outil READ
chargé avec la famille Navigateur."""
from __future__ import annotations

import json

import httpx
import pytest

from nokker.browserlive import live as live_mod
from nokker.browserlive import texts as T
from nokker.browserlive.frames import FrameStore
from nokker.browserlive.live import BrowserLive
from nokker.browserlive.point import BrowserPointSource
from nokker.core.ports import ToolContext
from nokker.core.turn import Effect
from nokker.tenancy.context import tenant_scope
from nokker.tools.browser_pool import BrowserPoolSource
from tests.unit.browserlive.test_browser_live import Bus, Clock, Pool, act, img

LISTINGS = ("- Page URL: https://immo.example/geneve\n- Page Title: Annonces Genève\n"
            '- link "3 pièces Plainpalais, 1900 CHF" [ref=e10]\n'
            '- link "Studio Eaux-Vives, 1200 CHF" [ref=e11]\n'
            '- textbox "Mot de passe" [ref=e12] [password]\n'
            '- link "Mon compte" [ref=e13]\n')


class Browser:
    """Navigateur aux formes de sites-chrome 00814ab : instantané d'annonces, captures, et `browser_box` (élément amené
    dans la vue, capture fraîche, `_meta.nokker_box = {ref, x, y, w, h, in_view, frame_seq}`, refus si sensible)."""

    calls: list = []
    block = None
    page_sensitive = False
    frame_seq = 40

    def __init__(self, url, headers):
        pass

    def call(self, name, args):
        Browser.calls.append((name, dict(args)))
        if name == "browser_box":
            ref = args["ref"]
            if ref in ("e12", "e13"):  # e13 : un bloc qui CONTIENT un champ sensible, invisible pour le noyau
                return "boîte refusée : élément sensible (contient un champ de mot de passe)", True, None, {"meta": {}}
            if ref not in ("e10", "e11"):
                return "référence inconnue : refaire un browser_snapshot", True, None, {"meta": {}}
            Browser.frame_seq += 1
            fs = None if Browser.page_sensitive else Browser.frame_seq
            box = {"ref": ref, "x": 0.1, "y": 0.3, "w": 0.5, "h": 0.12, "in_view": True, "frame_seq": fs}
            meta = {"nokker_box": box, "nokker_action": {"tool": "box", "frame_seq": fs,
                                                         "sensitive": Browser.page_sensitive,
                                                         "url": "https://immo.example/geneve"}}
            images = [] if Browser.page_sensitive else [{"mime": "image/webp", "data": img()[0]["data"],
                                                        "meta": {"frame_seq": fs}}]
            return json.dumps(box), False, None, {"images": images, "meta": meta}
        meta = act(name, url="https://immo.example/geneve")
        if Browser.block:
            meta["nokker_block"] = Browser.block
        return LISTINGS, False, None, {"images": img(), "meta": meta}


@pytest.fixture
def setup():
    clock, bus = Clock(), Bus()
    lv = BrowserLive(publish=bus, clock=clock, frames=FrameStore(clock=clock))
    live_mod.configure(lv)
    Browser.calls, Browser.block, Browser.page_sensitive = [], None, False
    pool = BrowserPoolSource(base_url="http://pool", api_token=lambda: "k",
                             client=httpx.Client(transport=httpx.MockTransport(Pool())), mcp_factory=Browser)
    ctx = ToolContext(thread_id="th1", turn_index=0, channel="api", sender="alice")
    yield lv, bus, clock, pool, BrowserPointSource(), ctx
    live_mod.configure(None)


def test_spec_is_read_and_loaded_with_the_browser_family():
    from nokker.tools.effects import DEFAULT_EFFECTS
    from nokker.tools.select import FAMILIES

    spec = BrowserPointSource().specs()[0]
    assert spec.name == "browser_point" and spec.effect is Effect.READ and DEFAULT_EFFECTS["browser_point"] is Effect.READ
    assert spec.parameters["required"] == ["ref", "why"] and spec.parameters["properties"]["why"]["maxLength"] == 120
    nav = next(f for f in FAMILIES if f.label == "Navigateur")
    assert "browser_point" in nav.load and nav.match.search("désigne-moi la bonne annonce")


def test_point_emits_event_on_fresh_frame_and_touches_nothing(setup):
    lv, bus, clock, pool, point, ctx = setup
    with tenant_scope("tn_a"):
        pool.call("browser_navigate", {"url": "https://immo.example/geneve"}, ctx)
        frames_before, actions_before = len(bus.of("browser.frame")), len(bus.of("browser.action"))
        Browser.calls.clear()
        why = "C'est le 3 pièces sous 2000 CHF que tu cherches" + "!" * 200
        r = point.call("browser_point", {"ref": "e10", "why": why}, ctx)  # sans attendre une seconde
    assert not r.is_error and r.content == T.POINT_OK.format(label="3 pièces Plainpalais, 1900 CHF")
    ev = bus.of("browser.point")[-1]
    assert ev["box"] == {"x": 0.1, "y": 0.3, "w": 0.5, "h": 0.12} and ev["label"] == "3 pièces Plainpalais, 1900 CHF"
    assert len(ev["why"]) <= 120
    assert set(ev) - {"_tenant", "_audience"} == {"session_id", "seq", "ts", "box", "label", "why"}
    assert Browser.calls == [("browser_box", {"ref": "e10", "scroll": True})]  # aucun geste : seule la boîte est lue
    frames = bus.of("browser.frame")  # la capture fraîche part en frame, même repère que la boîte
    assert len(frames) == frames_before + 1 and frames[-1]["frame_seq"] == ev["seq"] == Browser.frame_seq
    assert frames[-1]["frame_url"] and len(bus.of("browser.action")) == actions_before


def test_point_refused_on_sensitive_target(setup):
    lv, bus, clock, pool, point, ctx = setup
    with tenant_scope("tn_a"):
        pool.call("browser_navigate", {"url": "https://immo.example/geneve"}, ctx)
        Browser.calls.clear()
        r = point.call("browser_point", {"ref": "e12", "why": "le champ"}, ctx)  # le noyau le sait : pas d'appel
        assert r.is_error and r.content == T.POINT_SENSITIVE and Browser.calls == []
        r = point.call("browser_point", {"ref": "e13", "why": "le bloc"}, ctx)  # le navigateur refuse
        assert r.is_error and r.content == T.POINT_SENSITIVE
        r = point.call("browser_point", {"ref": "e99", "why": "x"}, ctx)
        assert r.is_error and "e99" in r.content
    assert not bus.of("browser.point")


def test_point_refused_when_the_page_is_sensitive(setup):
    lv, bus, clock, pool, point, ctx = setup
    with tenant_scope("tn_a"):
        pool.call("browser_navigate", {"url": "https://immo.example/geneve"}, ctx)
        Browser.page_sensitive = True  # browser_box rend frame_seq = null, sans capture
        n = len(bus.of("browser.frame"))
        r = point.call("browser_point", {"ref": "e10", "why": "x"}, ctx)
    assert r.is_error and r.content == T.POINT_NO_FRAME and not bus.of("browser.point")
    assert len(bus.of("browser.frame")) == n


def test_point_refused_during_handoff(setup):
    lv, bus, clock, pool, point, ctx = setup
    with tenant_scope("tn_a"):
        pool.call("browser_navigate", {"url": "https://immo.example/geneve"}, ctx)
        Browser.block = {"kind": "captcha"}
        clock.t += 2
        pool.call("browser_scroll", {}, ctx)
        Browser.calls.clear()
        r = point.call("browser_point", {"ref": "e10", "why": "x"}, ctx)
    assert r.is_error and "la personne a la main" in r.content and not bus.of("browser.point")
    assert Browser.calls == []  # rien n'atteint le navigateur


def test_point_without_any_session(setup):
    lv, bus, clock, pool, point, ctx = setup
    with tenant_scope("tn_a"):
        r = point.call("browser_point", {"ref": "e10", "why": "x"}, ctx)
    assert r.is_error and r.content == T.POINT_NO_SESSION


def test_session_state_keeps_the_last_20_points(setup):
    lv, bus, clock, pool, point, ctx = setup
    with tenant_scope("tn_a"):
        pool.call("browser_navigate", {"url": "https://immo.example/geneve"}, ctx)
        for i in range(23):
            assert not point.call("browser_point", {"ref": "e10", "why": f"raison {i}"}, ctx).is_error
    sid = bus.of("browser.point")[-1]["session_id"]
    pts = lv.get(sid, "tn_a", "alice")["points"]
    assert len(pts) == 20 and pts[-1] == {k: v for k, v in bus.of("browser.point")[-1].items()
                                          if k in ("seq", "ts", "box", "label", "why")}
    assert pts[0]["why"] == "raison 3" and set(pts[0]) == {"seq", "ts", "box", "label", "why"}
