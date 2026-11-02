"""Écrans du foyer (docs/SCREEN-CAST.md) avec de faux appareils : pychromecast, pyatv et une télé DLNA simulés."""
from __future__ import annotations

import asyncio
import http.client
import json
import re
import time
from types import SimpleNamespace
from typing import ClassVar
from uuid import UUID

import pytest

from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.cast import texts as T
from nokker.cast.bridge import CastEngine, Discovery
from nokker.cast.render import RenderError, render, sanitize_svg
from nokker.cast.screens import AirPlayBackend, ChromecastBackend, DlnaBackend, ScreenBook
from nokker.cast.server import CastServer
from nokker.cast.service import CAST_TOOLS, CastService
from nokker.core.ports import ToolContext
from nokker.core.turn import InputCheck, ToolCall, Turn
from nokker.local.api import LocalApi
from nokker.qa.invariants import DeterministicInvariants
from nokker.qa.secrets import KnownSecrets
from nokker.threads.memory import InMemoryThreads

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SECRET = "Xk9!qpzT42LmVb7wQe3R"  # valeur du coffre : jamais à l'écran
UUID_SALON = "6f1c2a52-0d3e-4c55-9a77-1d2e3f405060"
UUID_CHAMBRE = "11111111-2222-3333-4444-555555555555"


# ─────────────────────────────── faux appareils ───────────────────────────────


class FakeDash:
    loaded: ClassVar[list[tuple[str, bool]]] = []

    def load_url(self, url, *, force=False, reload_seconds=0, callback_function=None):
        FakeDash.loaded.append((url, force))


class FakeCast:
    def __init__(self, uuid, name, host):
        self.cast_info = SimpleNamespace(uuid=UUID(uuid), friendly_name=name, host=host, port=8009,
                                         model_name="Chromecast", cast_type="cast")
        self.handlers, self.played, self.quit = [], [], 0
        self.media_controller = SimpleNamespace(play_media=lambda url, mime: self.played.append((url, mime)))

    def wait(self, timeout=None):
        return None

    def register_handler(self, h):
        self.handlers.append(h)

    def quit_app(self):
        self.quit += 1

    def disconnect(self, timeout=None):
        return None


class FakeChromecastLib:
    def __init__(self, casts):
        self.casts = casts
        self.stopped = 0
        self.unicast: list[str] = []
        self.discovery = SimpleNamespace(stop_discovery=self._stop)

    def _stop(self, browser):
        self.stopped += 1

    def get_chromecasts(self, timeout=None):
        return list(self.casts), object()

    def get_chromecast_from_host(self, host, tries=None, retry_wait=None, timeout=None):
        ip, _port, uuid, _model, _name = host
        self.unicast.append(ip)
        return next(c for c in self.casts if c.cast_info.uuid == uuid)


class FakeAtvConf:
    def __init__(self):
        self.identifier, self.name, self.address = "AA:BB", "Apple TV Bureau", "127.0.0.1"
        self.device_info = SimpleNamespace(model_str="Apple TV 4K")
        self.creds = None

    def get_service(self, protocol):
        return SimpleNamespace(pairing=SimpleNamespace(name="Mandatory"))

    def set_credentials(self, protocol, creds):
        self.creds = creds


class FakePyatv:
    def __init__(self):
        self.conf = FakeAtvConf()
        self.played: list[str] = []
        self.const = SimpleNamespace(Protocol=SimpleNamespace(AirPlay="airplay"))
        lib = self

        class Pairing:
            has_paired = False
            service = SimpleNamespace(credentials=None)

            async def begin(self):
                return None

            def pin(self, pin):
                self.ok = pin == 1234

            async def finish(self):
                if self.ok:
                    self.has_paired = True
                    self.service = SimpleNamespace(credentials="cred-abc")

            async def close(self):
                return None

        self.Pairing = Pairing

        async def scan(loop, timeout=5, identifier=None, hosts=None):
            return [lib.conf]

        async def connect(conf, loop):
            async def play_url(url):
                lib.played.append(url)

            return SimpleNamespace(stream=SimpleNamespace(play_url=play_url), close=lambda: None)

        async def pair(conf, protocol, loop):
            return Pairing()

        self.scan, self.connect, self.pair = scan, connect, pair


def _hosts_ok(server):
    return server.host


@pytest.fixture
def world(tmp_path):
    """Service complet sur 127.0.0.1 (seul cas où le bouclage est permis), deux Chromecast, une Apple TV."""
    FakeDash.loaded = []
    salon = FakeCast(UUID_SALON, "Salon", "127.0.0.1")
    chambre = FakeCast(UUID_CHAMBRE, "Chambre de Bob", "127.0.0.1")
    cc_lib = FakeChromecastLib([salon, chambre])
    atv_lib = FakePyatv()
    vault: dict[str, str] = {}
    root = tmp_path / "repo"
    root.mkdir()
    (root / "plan.png").write_bytes(PNG)
    inv = DeterministicInvariants(known=KnownSecrets({"COFFRE_ITEM": SECRET}))
    server = CastServer(bind="127.0.0.1", allow_loopback=True, heartbeat_s=0.2)
    backends = {"chromecast": ChromecastBackend(lib=cc_lib, dashcast=FakeDash),
                "airplay": AirPlayBackend(lib=atv_lib, secret_get=vault.get, secret_set=vault.__setitem__)}
    engine = CastEngine(backends=backends, server=server, discover=Discovery(backends))
    svc = CastService(book=ScreenBook(tmp_path / "cast" / "screens.json"), bridge=engine, scrub=inv.scrub_output, root_of=lambda _t: root,
                      fetch_web=lambda url: ("Article", f"Premier paragraphe.\nclé {SECRET} ici"))
    yield SimpleNamespace(svc=svc, server=server, engine=engine, salon=salon, chambre=chambre, cc=cc_lib, atv=atv_lib, vault=vault,
                          root=root)
    svc.close()


def ctx(thread="th_a", sender="alice"):
    return ToolContext(thread_id=thread, turn_index=0, channel="cli", sender=sender)


def call(svc, args, c=None):
    r = svc.tool_source().call("show_on_screen", args, c or ctx())
    return r.content, r.is_error


def cast_id_of(text):
    return re.search(r"cast_[0-9a-f]{12}", text).group(0)


def get(server, path, host=None, headers=None):
    conn = http.client.HTTPConnection(server.host, server.port, timeout=5)
    conn.request("GET", path, headers={"Host": host or server.authority, **(headers or {})})
    r = conn.getresponse()
    body = r.read() if "events" not in path else b""
    return r.status, body, r


def token_of(url):
    return url.rsplit("/cast/", 1)[1].split("/")[0]


def approve_screen(w, text, user="alice"):
    st = w.svc.bubble_state(cast_id_of(text))
    return w.svc.decide(st["request"]["id"], True, user)


# ─────────────────────────────── découverte ───────────────────────────────


def test_discovery_lists_screens_with_rooms_and_never_hosts(world):
    w = world
    w.svc.book.set("cc-" + UUID_SALON, room="Salon")
    text = w.svc.tool_source().call("list_screens", {}, ctx()).content
    assert text.startswith(T.LIST_HEAD)
    assert f"cc-{UUID_SALON} « Salon » (Salon, chromecast) : affiche pages, images ; {T.STATE_NEW}" in text
    assert "atv-AA-BB « Apple TV Bureau » (Apple TV Bureau, airplay) : affiche images" in text
    assert T.STATE_UNPAIRED.strip(" ;") in text
    assert "127.0.0.1" not in text and w.cc.stopped >= 1  # la découverte s'arrête ; aucune adresse au modèle
    view = w.svc.screens_view()
    assert {v["kind"] for v in view} == {"chromecast", "airplay"} and all("host" not in v for v in view)


def test_dlna_discovery_and_media_url(tmp_path):
    desc = (b'<root xmlns="urn:schemas-upnp-org:device-1-0"><device><friendlyName>TV Cuisine</friendlyName>'
            b'<UDN>uuid:abc-1</UDN><serviceList><service><serviceType>urn:schemas-upnp-org:service:AVTransport:1'
            b'</serviceType><controlURL>/ctl</controlURL></service></serviceList></device></root>')
    sent = []

    def http(url, body, headers):
        sent.append((url, body, headers))
        return desc

    d = DlnaBackend(search=lambda _t: ["http://192.168.1.20:8080/desc.xml", "http://8.8.8.8/desc.xml"], http=http)
    screens = d.discover()
    assert [(s.id, s.name, s.caps) for s in screens] == [("dlna-abc-1", "TV Cuisine", ("image",))]
    d.show(screens[0], "http://192.168.1.5:9/cast/t/media", media="image/png")
    actions = [h["SOAPACTION"] for _, _, h in sent if h]
    assert actions == ['"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"',
                       '"urn:schemas-upnp-org:service:AVTransport:1#Play"']
    assert all(u.startswith("http://192.168.1.20:8080") for u, _, _ in sent)  # adresse publique jamais suivie


# ─────────────────────────────── approbation du premier écran ───────────────────────────────


def test_first_display_needs_approval_then_screen_is_remembered(world):
    w = world
    text, err = call(w.svc, {"screen": "Salon", "kind": "table", "columns": ["a"], "rows": [["1"]]})
    assert not err and T.PENDING_SCREEN.split("{")[0] in text and FakeDash.loaded == []  # rien n'est affiché
    st = w.svc.bubble_state(cast_id_of(text))
    assert st["status"] == "pending" and st["request"]["reason"] == "new_screen"
    with pytest.raises(PermissionError):
        w.svc.decide(st["request"]["id"], True, "bob")  # pas sa demande
    after = approve_screen(w, text)
    assert after["status"] == "showing" and len(FakeDash.loaded) == 1 and FakeDash.loaded[0][1] is True
    assert w.svc.book.entry("cc-" + UUID_SALON)["approved_by"] == "alice"
    # deuxième affichage : plus de demande
    text2, err2 = call(w.svc, {"screen": "Salon", "kind": "table", "rows": [["2"]]})
    assert not err2 and text2.startswith("affiché sur « Salon »") and len(FakeDash.loaded) == 2


def test_refused_screen_shows_nothing(world):
    w = world
    text, _ = call(w.svc, {"screen": "Salon", "kind": "table", "rows": [["1"]]})
    st = w.svc.bubble_state(cast_id_of(text))
    assert w.svc.decide(st["request"]["id"], False, "alice")["status"] == "refused"
    assert FakeDash.loaded == [] and call(w.svc, {"cast_id": cast_id_of(text), "kind": "table",
                                                  "rows": [["x"]]})[1] is True


# ─────────────────────────────── lien éphémère ───────────────────────────────


def _show(w, **args):
    text, _ = call(w.svc, {"screen": "Salon", "kind": "table", "rows": [["1"]], **args})
    if T.PENDING_SCREEN.split("{")[0] in text:
        approve_screen(w, text)
    return cast_id_of(text)


def test_expired_link_is_refused(world):
    w = world
    now = [time.time()]
    w.server.clock = lambda: now[0]
    _show(w)
    url = FakeDash.loaded[-1][0]
    assert url.startswith(f"http://127.0.0.1:{w.server.port}/cast/")
    path = "/cast/" + token_of(url)
    assert get(w.server, path)[0] == 200
    now[0] += w.svc.ttl_s + 1
    assert get(w.server, path)[0] == 410


def test_link_uses_are_limited_and_bound_to_first_client(world):
    w = world
    w.svc.max_uses = 2
    _show(w)
    path = "/cast/" + token_of(FakeDash.loaded[-1][0])
    status, body, r = get(w.server, path)
    assert status == 200 and b"<table>" in body
    assert "script-src 'nonce-" in r.getheader("Content-Security-Policy")
    assert r.getheader("Cache-Control") == "no-store"
    assert get(w.server, path)[0] == 200
    assert get(w.server, path)[0] == 410  # épuisé
    assert get(w.server, "/cast/" + "x" * 43)[0] == 404


def test_link_never_leaves_the_local_network(world):
    w = world
    _show(w)
    path = "/cast/" + token_of(FakeDash.loaded[-1][0])
    assert get(w.server, path, headers={"X-Forwarded-For": "203.0.113.9"})[0] == 403  # passé par un relais
    assert get(w.server, path, headers={"CF-Connecting-IP": "203.0.113.9"})[0] == 403
    assert get(w.server, path, host="cast.example.com")[0] == 403  # rebinding DNS
    strict = CastServer(bind="127.0.0.1")  # sans permission de bouclage : refus de démarrer hors réseau privé
    with pytest.raises(RuntimeError):
        strict.start()
    with pytest.raises(RuntimeError):
        CastServer(bind="8.8.8.8").start()


def test_link_is_never_in_tool_result_nor_bubble(world):
    w = world
    cid = _show(w)
    token = token_of(FakeDash.loaded[-1][0])
    text, _ = call(w.svc, {"cast_id": cid, "kind": "table", "rows": [["2"]]})
    blob = text + json.dumps(w.svc.bubble_state(cid)) + json.dumps(w.svc.screens_view())
    assert token not in blob and "/cast/" not in blob and "127.0.0.1" not in blob


def test_live_update_reaches_the_screen_by_sse(world):
    w = world
    cid = _show(w)
    path = "/cast/" + token_of(FakeDash.loaded[-1][0])
    assert get(w.server, path)[0] == 200
    conn = http.client.HTTPConnection(w.server.host, w.server.port, timeout=5)
    conn.request("GET", path + "/events", headers={"Host": w.server.authority})
    r = conn.getresponse()
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/event-stream")
    time.sleep(0.05)
    text, err = call(w.svc, {"cast_id": cid, "kind": "table", "rows": [["mis à jour"]]})
    assert not err and text.startswith(f"affichage {cid} mis à jour")
    seen = b""
    while b"event: update" not in seen:
        seen += r.fp.readline()
    data = r.fp.readline()
    assert "mis à jour" in json.loads(data.decode().removeprefix("data: "))["body"]
    call(w.svc, {"cast_id": cid, "stop": True})
    rest = b""
    while b"event: end" not in rest:
        rest += r.fp.readline()
    assert w.svc.bubble_state(cid)["status"] == "stopped" and w.salon.quit == 1


# ─────────────────────────────── caviardage, privé ───────────────────────────────


def test_secret_is_redacted_on_screen_and_in_bubble(world):
    w = world
    cid = _show(w, rows=[["mot de passe", SECRET]], columns=["quoi", "valeur"])
    path = "/cast/" + token_of(FakeDash.loaded[-1][0])
    body = get(w.server, path)[1].decode()
    assert SECRET not in body and "[masqué]" in body and "[secret:" not in body
    st = w.svc.bubble_state(cid)
    assert SECRET not in json.dumps(st) and st["masked"] >= 1
    # page web en lecture, SVG : même caviardage
    call(w.svc, {"cast_id": cid, "kind": "web", "url": "https://example.com/a"})
    assert SECRET not in json.dumps(w.svc.bubble_state(cid))
    svg = f'<svg viewBox="0 0 10 10"><text x="1" y="5">{SECRET}</text><script>alert(1)</script></svg>'
    call(w.svc, {"cast_id": cid, "kind": "diagram", "svg": svg})
    html = w.svc.bubble_state(cid)["html"]
    assert SECRET not in html and "alert(1)" not in html


def test_private_flag_forbids_display(world):
    w = world
    text, err = call(w.svc, {"screen": "Salon", "kind": "table", "rows": [["1"]], "private": True})
    assert err and text == T.PRIVATE_REFUSED and FakeDash.loaded == []
    cid = _show(w, rows=[["public"], {"cells": ["salaire 9000"], "private": True}])
    html = w.svc.bubble_state(cid)["html"]
    assert "salaire" not in html and "contenu privé masqué" in html and "public" in html


def test_svg_sanitizer_strips_active_content():
    out = sanitize_svg('<svg xmlns="http://www.w3.org/2000/svg" onload="x()"><a href="javascript:x()">'
                       '<rect width="1" onclick="y()"/></a><foreignObject><p>h</p></foreignObject>'
                       '<rect style="fill:url(https://evil/x)" width="2"/><image href="http://e/i.png"/></svg>',
                       _NoScrub())
    assert all(bad not in out for bad in ("onload", "onclick", "javascript", "foreignObject", "evil", "<image"))
    with pytest.raises(RenderError):
        sanitize_svg('<!DOCTYPE svg [<!ENTITY a "b">]><svg>&a;</svg>', _NoScrub())


class _NoScrub:
    masked = 0

    def text(self, v, limit=0):
        import html

        return html.escape(str(v or ""))


def test_image_must_stay_in_the_workspace(world, tmp_path):
    w = world
    (tmp_path / "dehors.png").write_bytes(PNG)
    text, err = call(w.svc, {"screen": "Salon", "kind": "image", "path": "../dehors.png"})
    assert err and "hors du dossier de travail" in text
    with pytest.raises(RenderError):
        render({"kind": "image", "path": "x"}, scrub=str, read_image=lambda p: b"pas une image")


# ─────────────────────────────── foyer : écran d'un autre membre ───────────────────────────────


def test_member_content_needs_consent_on_another_members_screen(world):
    w = world
    sid = "cc-" + UUID_CHAMBRE
    w.svc.book.remember(w.svc.resolve(sid)[0], by="bob", now=1.0)
    w.svc.book.set(sid, owner="bob")
    text, err = call(w.svc, {"screen": sid, "kind": "table", "rows": [["note d'alice"]]})
    assert not err and T.PENDING_CONSENT.split("{")[0] in text and FakeDash.loaded == []
    st = w.svc.bubble_state(cast_id_of(text))
    assert st["request"]["reason"] == "consent"
    with pytest.raises(PermissionError):
        w.svc.decide(st["request"]["id"], True, "bob")  # le propriétaire de l'écran ne décide pas pour alice
    with pytest.raises(PermissionError):
        w.svc.decide(st["request"]["id"], True, "local-owner", machine_owner=True)  # la machine non plus
    assert w.svc.decide(st["request"]["id"], True, "alice")["status"] == "showing"
    assert w.svc.book.consented("alice", sid) and not w.svc.book.consented("carol", sid)
    # bob sur son propre écran : aucune demande
    text_b, _ = call(w.svc, {"screen": sid, "kind": "table", "rows": [["b"]]}, ctx("th_b", "bob"))
    assert text_b.startswith("affiché sur")


def test_another_thread_cannot_touch_a_cast(world):
    w = world
    cid = _show(w)
    for c in (ctx("th_b", "bob"), ctx("th_a", "bob"), ctx("th_b", "alice")):
        text, err = call(w.svc, {"cast_id": cid, "stop": True}, c)
        assert err and text == T.NO_CAST.format(cast_id=cid)
    assert w.svc.bubble_state(cid)["status"] == "showing"
    with pytest.raises(PermissionError):
        w.svc.bubble_action({"cast_id": cid}, "stop", "bob")


# ─────────────────────────────── AirPlay ───────────────────────────────


def test_airplay_needs_pairing_then_plays_the_image(world):
    w = world
    sid = "atv-AA-BB"
    text, err = call(w.svc, {"screen": sid, "kind": "table", "rows": [["1"]]})
    assert err and text.startswith("« Apple TV Bureau » (airplay) n'affiche que des images")
    text, _ = call(w.svc, {"screen": sid, "kind": "image", "path": "plan.png"})
    text, err = (w.svc.decide(w.svc.bubble_state(cast_id_of(text))["request"]["id"], True, "alice"), None)
    assert text["status"] == "failed" and text["error"] == "pairing" and w.atv.played == []
    assert w.svc.pair(sid, None)["state"] == "pin_shown"
    assert w.svc.pair(sid, "1234")["state"] == "paired"
    assert w.vault == {"cast:airplay:atv-AA-BB": "cred-abc"}  # au coffre, jamais dans screens.json
    assert "cred-abc" not in (w.svc.book.path.read_text())
    text, err = call(w.svc, {"screen": sid, "kind": "image", "path": "plan.png"})
    assert not err and w.atv.played and w.atv.played[0].endswith("/media") and w.atv.conf.creds == "cred-abc"
    status, body, r = get(w.server, w.atv.played[0].split(str(w.server.port), 1)[1])
    assert status == 200 and body == PNG and r.getheader("Content-Type") == "image/png"


# ─────────────────────────────── bulle et drapeau ───────────────────────────────


def test_cast_bubble_follows_the_cast(world):
    w = world
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=lambda *a: None,
                    spawn=lambda fn: fn())
    hub.cast = w.svc
    hub.producers.cast_state = w.svc.bubble_state
    w.svc.on_change = hub.cast_changed
    th = hub.create_thread("Écran", "alice")
    c = ctx(th.id)
    turn = Turn(thread_id=th.id, index=0, input=InputCheck(text="montre"), sender="alice")
    args = {"screen": "Salon", "kind": "table", "rows": [["1"]]}
    tc = ToolCall(id="c1", name="show_on_screen", arguments=args)
    res = w.svc.tool_source().call("show_on_screen", args, c)
    hub.tool_executed(turn, tc, None, res)
    b = next(x for x in hub.bubbles.list_thread(th.id) if x.kind == "cast")
    assert b.status == "idle" and [a["id"] for a in b.state["actions"]] == ["approve", "refuse"]
    out = hub.action(b.id, "alice", "approve")
    assert out["cast"]["status"] == "showing" and "html" not in out["cast"]
    b = hub.bubbles.get(b.id)
    assert b.status == "working" and b.state["html"].startswith("<!doctype html>")
    cid = b.state["cast_id"]
    args2 = {"cast_id": cid, "kind": "table", "rows": [["2"]]}
    res2 = w.svc.tool_source().call("show_on_screen", args2, c)
    extra = hub.tool_executed(turn, ToolCall(id="c2", name="show_on_screen", arguments=args2), None, res2)
    assert extra == T.BUBBLE_UPDATED.format(id=b.id)
    assert len([x for x in hub.bubbles.list_thread(th.id) if x.kind == "cast"]) == 1
    hub.action(b.id, "alice", "stop")
    assert hub.bubbles.get(b.id).state["status"] == "stopped"


def test_flag_off_leaves_the_lokkal_tool_surface_unchanged(tmp_path):
    root = tmp_path / "projets"
    (root / "app").mkdir(parents=True)

    def names(**flags):
        api = LocalApi.from_config({"token_path": str(tmp_path / "tok"), "state_dir": str(tmp_path / "st"),
                                    "code": {"roots": [str(root)], "backend": "local"}, **flags}, port=8742)
        specs = [s for src in api.tool_sources() for s in src.specs()]
        return api, [(s.name, s.description, json.dumps(s.parameters, sort_keys=True)) for s in specs]

    off_api, off = names()
    assert off_api.cast is None
    assert names(cast={"enabled": False})[1] == off and names(cast={})[1] == off
    on_api, on = names(cast={"enabled": True, "bridge_url": "http://127.0.0.1:9"})
    assert sorted({n for n, _, _ in on} - {n for n, _, _ in off}) == sorted(CAST_TOOLS)
    assert [x for x in on if x[0] not in CAST_TOOLS] == off  # le reste, octet pour octet
    assert on_api.handle("GET", "/v1/local/cast/screens", f"Bearer {on_api.token}", {}, relayed=False)[0] in (200,)
    assert off_api.handle("GET", "/v1/local/cast/screens", f"Bearer {off_api.token}", {})[0] == 404
    assert on_api.handle("GET", "/v1/local/cast/screens", f"Bearer {on_api.token}", {}, relayed=True)[0] in (401, 403)


def test_specs_texts_are_the_reported_ones(world):
    specs = {s.name: s for s in world.svc.tool_source().specs()}
    assert specs["show_on_screen"].description == T.SHOW_DESCRIPTION
    assert specs["list_screens"].description == T.LIST_DESCRIPTION
    assert specs["show_on_screen"].effect.value == "write" and specs["list_screens"].effect.value == "read"
    assert not any(s.core for s in specs.values())


def test_airplay_backend_runs_its_own_loop():
    lib = FakePyatv()
    b = AirPlayBackend(lib=lib)
    screens = b.discover()
    assert screens[0].paired is False and screens[0].caps == ("image",)
    assert asyncio.get_event_loop_policy() is not None


# ─────────────────────────────── home-bridge / home-discovery ───────────────────────────────


def _serve_bridge(engine, token):
    """La passerelle commune du foyer (nokker/homebridge) avec le moteur des écrans : mêmes opérations nommées."""
    import threading
    from http.server import ThreadingHTTPServer
    from pathlib import Path

    from nokker.homebridge.audio import AudioRouter, ClipStore
    from nokker.homebridge.config import from_dict
    from nokker.homebridge.eink import FrameCache
    from nokker.homebridge.server import Bridge, api_handler

    clips = ClipStore()
    bridge = Bridge(cfg=from_dict({}, token=token, base_dir=Path(".")), frames=FrameCache(), clips=clips,
                    audio=AudioRouter(rooms={}, clips=clips, lan_url=""), presence_source=None, cast=engine)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), api_handler(bridge))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_core_reaches_the_lan_only_through_named_bridge_operations(world, tmp_path):
    """Le noyau ne parle qu'aux opérations nommées de home-bridge ; jeton exigé ; ni adresse ni lien ne reviennent."""
    import httpx

    from nokker.cast.bridge import HttpBridge

    token = "t" * 32
    httpd = _serve_bridge(world.engine, token)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        svc = CastService(book=world.svc.book, bridge=HttpBridge(base_url=base, token=lambda: token),
                          scrub=world.svc.scrub, root_of=lambda _t: world.root)
        text, _ = call(svc, {"screen": "Salon", "kind": "table", "rows": [["1"]]})
        approve_screen(SimpleNamespace(svc=svc), text)
        st = svc.bubble_state(cast_id_of(text))
        assert st["status"] == "showing" and len(FakeDash.loaded) == 1 and world.cc.unicast == ["127.0.0.1"]
        assert "127.0.0.1" not in json.dumps(svc.screens_view()) and "/cast/" not in json.dumps(st)
        r = httpx.post(base + "/v1/op/cast.screens", json={}, headers={"Authorization": "Bearer faux"})
        assert r.status_code == 401
        r = httpx.post(base + "/v1/op/http.get", json={"url": "http://192.168.1.1"},
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 404  # aucune opération générique
        r = httpx.post(base + "/v1/op/cast.screens", json={},
                       headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": "203.0.113.1"})
        assert r.status_code == 401
        from nokker.homebridge.config import BridgeConfigError, load

        with pytest.raises(BridgeConfigError):
            load({"HOME_CONFIG": str(tmp_path / "absent.yaml")})  # pas de jeton : la passerelle ne démarre pas
    finally:
        httpd.shutdown()


def test_common_bridge_serves_cast_ops_only_when_home_yaml_allows_it(tmp_path):
    """Passerelle commune (nokker/homebridge) : `cast.*` dans sa table OPS ; sans `cast.enabled: true`, aucun moteur
    (aucune écoute de pages) et un refus `disabled` que le client du noyau relit tel quel."""
    import threading
    from http.server import ThreadingHTTPServer

    from nokker.cast.bridge import BridgeError, HttpBridge
    from nokker.homebridge.config import from_dict
    from nokker.homebridge.server import OPS, Bridge, api_handler

    assert {"cast.screens", "cast.show", "cast.update", "cast.stop", "cast.pair"} <= set(OPS)
    token = "t" * 32
    bridge = Bridge.build(from_dict({}, token=token, base_dir=tmp_path))
    assert bridge.cast is None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), api_handler(bridge))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        client = HttpBridge(base_url=f"http://127.0.0.1:{httpd.server_address[1]}", token=lambda: token)
        with pytest.raises(BridgeError) as e:
            client.screens()
        assert e.value.code == "disabled"
        bad = HttpBridge(base_url=f"http://127.0.0.1:{httpd.server_address[1]}", token=lambda: "faux" * 8)
        with pytest.raises(BridgeError) as e:
            bad.screens()
        assert e.value.code == "unauthorized"
    finally:
        httpd.shutdown()


def test_discovery_is_a_subcommand_of_the_common_bridge(monkeypatch):
    import nokker.cast.bridge as cb
    from nokker.homebridge.__main__ import main

    monkeypatch.setattr(cb, "discovery_main", lambda: 7)
    assert main(["discovery"]) == 7


def test_bridge_refuses_cast_until_home_yaml_allows_it(world, tmp_path):
    from nokker.cast.bridge import BridgeError, engine_from_env

    (tmp_path / "home.yaml").write_text("{}\n")
    eng = engine_from_env({"HOME_CONFIG": str(tmp_path / "home.yaml"), "HOME_STATE": str(tmp_path / "st"),
                           "HOME_SOCK": str(tmp_path / "absent.sock"), "CAST_ADVERTISE": "192.168.1.5:8801"})
    with pytest.raises(BridgeError) as e:
        eng.screens()
    assert e.value.code == "disabled"
    (tmp_path / "home.yaml").write_text("cast:\n  enabled: true\n")
    eng = engine_from_env({"HOME_CONFIG": str(tmp_path / "home.yaml"), "HOME_STATE": str(tmp_path / "st"),
                           "HOME_SOCK": str(tmp_path / "absent.sock"), "CAST_ADVERTISE": "192.168.1.5:8801"})
    with pytest.raises(BridgeError) as e:
        eng.screens()
    assert e.value.code == "unavailable"  # profil home-mdns absent : dit clairement


def test_discovery_socket_serves_full_screens_to_the_bridge_only(world, tmp_path):
    import os

    from nokker.cast.bridge import Discovery, DiscoveryClient, serve_discovery

    sock = str(tmp_path / "run" / "discovery.sock")
    srv = serve_discovery(Discovery({"chromecast": ChromecastBackend(lib=world.cc, dashcast=FakeDash)}), sock)
    try:
        assert oct(os.stat(sock).st_mode & 0o777) == "0o600"
        rows = DiscoveryClient(sock).screens(refresh=True)
        assert {r["name"] for r in rows} == {"Salon", "Chambre de Bob"} and rows[0]["host"] == "127.0.0.1"
    finally:
        srv.shutdown()
        srv.server_close()


def test_bridge_ignores_screens_outside_the_lan(world):
    from nokker.cast.bridge import CastEngine

    class Pub:
        def screens(self, refresh=False):
            return [{"id": "cc-x", "name": "Dehors", "kind": "chromecast", "host": "8.8.8.8", "caps": ["page"]}]

    eng = CastEngine(backends=world.engine.backends, server=world.server, discover=Pub())
    assert eng.screens(refresh=True) == []
