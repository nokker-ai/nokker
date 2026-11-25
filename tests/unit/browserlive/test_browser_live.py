"""Vue navigateur en direct (lot V3 « Agent qui montre », docs/BROWSER-LIVE.md) : URL de frame signée 60 s, anneau en
mémoire purgé, sensible jamais capturé, `type` sans texte, `ended` toujours émis, passation (CAPTCHA toujours, gestes
refusés), reprise à usage unique réservée au titulaire, `resume` idempotent, bulle `browser` aux transitions, et la
forme `_meta` livrée par sites-chrome (nokker/v3-frames) (fixtures) au travers du client du pool."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import httpx
import pytest

from nokker.browserlive import live as live_mod
from nokker.browserlive.frames import FrameStore, webp_size
from nokker.browserlive.live import BrowserLive
from nokker.core.ports import ToolContext
from nokker.tenancy.context import tenant_scope
from nokker.tools.browser_pool import BrowserPoolSource

ROOT = Path(__file__).resolve().parents[3]


def webp(w: int = 640, h: int = 400, tag: bytes = b"") -> bytes:
    """WebP minimal (en-tête VP8X) de w × h : assez pour l'analyse d'en-tête du noyau."""
    body = b"VP8X" + (10).to_bytes(4, "little") + b"\x00\x00\x00\x00" + (w - 1).to_bytes(3, "little") \
        + (h - 1).to_bytes(3, "little") + tag
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + body


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Bus:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event_type, data):
        self.events.append((event_type, data))

    def of(self, kind):
        return [d for t, d in self.events if t == kind]


class FakeHub:
    def __init__(self):
        self.created, self.updated = [], []

    def create_kernel_bubble(self, thread_id, *, kind, title="", state=None, status="idle", actions=None):
        self.created.append((thread_id, kind, title, dict(state or {}), status))
        return type("B", (), {"id": f"b{len(self.created)}"})()

    def update_from_kernel(self, bubble_id, *, title=None, state=None, status=None, if_version=None):
        self.updated.append((bubble_id, dict(state or {}), status))

    def thread_audience(self, thread_id):
        return {"alice"}


def _live(clock=None, hub=None):
    clock = clock or Clock()
    bus = Bus()
    lv = BrowserLive(publish=bus, hub=hub, clock=clock, frames=FrameStore(clock=clock))
    return lv, bus, clock


def act(tool, url="https://shop.example/cart", label="", sensitive=None, **extra):
    a = {"tool": tool, "ref": "e1", "label": label, "url": url, "t": 0}
    if sensitive is not None:
        a["sensitive"] = sensitive
    a.update(extra)
    return {"nokker_action": a}


def img(data=None):
    return [{"mime": "image/webp", "data": data or webp()}]


# ── frames : signature, expiration, anneau ──

def test_frame_url_signed_and_expires_after_60s():
    clock = Clock()
    store = FrameStore(clock=clock)
    f = store.put("bs_1", webp())
    url = store.url(f)
    m = re.match(r"^/v1/browser/frames/(bf_[^.]+)\.webp\?exp=(\d+)&sig=([A-Za-z0-9_-]+)$", url)
    assert m and m.group(1) == f.id
    assert store.fetch(f.id, m.group(2), m.group(3)) == (200, f.data)
    assert store.fetch(f.id, m.group(2), m.group(3)[:-2] + "AA")[0] == 403  # signature fausse
    assert store.fetch(f.id, str(int(m.group(2)) + 600), m.group(3))[0] == 403  # exp prolongé : signature fausse
    assert store.fetch(f.id, m.group(2), None)[0] == 403
    clock.t += 61
    assert store.fetch(f.id, m.group(2), m.group(3))[0] == 410  # expirée


def test_ring_keeps_20_and_purge_empties():
    store = FrameStore(clock=Clock())
    ids = [store.put("bs_1", webp(tag=bytes([i]))).id for i in range(25)]
    store.put("bs_2", webp())
    assert store.count("bs_1") == 20 and store.last("bs_1").id == ids[-1]
    url = store.url(store.last("bs_1"))
    exp, sig = re.search(r"exp=(\d+)&sig=(.+)$", url).groups()
    store.purge("bs_1")
    assert store.count("bs_1") == 0 and store.count() == 1  # l'autre session garde la sienne
    assert store.fetch(ids[-1], exp, sig)[0] == 410  # purgée : 410


def test_only_webp_up_to_1280px():
    store = FrameStore(clock=Clock())
    assert webp_size(webp(1280, 720)) == (1280, 720)
    assert store.put("s", webp(1281, 720)) is None
    assert store.put("s", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40) is None
    assert store.count() == 0


# ── actions et frames ──

def test_type_never_carries_the_typed_text():
    lv, bus, clock = _live()
    secret_text = "ma réponse très personnelle 42"
    lv.observe("tn_a", "th1", "alice", "browser_type", {"ref": "e3", "element": "Commentaire", "text": secret_text},
               "ok", False, {"images": img(), "meta": act("browser_type", label="Commentaire")})
    a = bus.of("browser.action")[-1]
    assert a["kind"] == "type" and a["target_label"] == f"texte saisi ({len(secret_text)} car.)"
    assert secret_text not in json.dumps(bus.events, ensure_ascii=False)
    snap = lv.get(a["session_id"], "tn_a", "alice")
    assert secret_text not in json.dumps(snap, ensure_ascii=False)


def test_action_shape_and_cursor():
    lv, bus, clock = _live()
    lv.observe("tn_a", "th1", "alice", "browser_click", {"ref": "e1", "element": "Bouton"}, "ok", False,
               {"images": img(), "meta": act("browser_click", label="Ajouter au panier " + "x" * 200,
                                             cursor={"x": 0.25, "y": 0.5})})
    a = bus.of("browser.action")[-1]
    assert set(a) - {"_tenant", "_audience"} == {"session_id", "seq", "ts", "kind", "target_label", "url_host", "cursor",
                                                    "ok"}
    assert a["kind"] == "click" and len(a["target_label"]) <= 80 and a["url_host"] == "shop.example"
    assert a["cursor"] == {"x": 0.25, "y": 0.5}
    f = bus.of("browser.frame")[-1]
    assert f["seq"] > a["seq"] and f["frame_url"].startswith("/v1/browser/frames/") and (f["w"], f["h"]) == (640, 400)
    assert f["sensitive"] is False
    ev = bus.events[0][1]
    assert ev["_tenant"] == "tn_a" and ev["_audience"] == ["alice"]  # flux du titulaire, jamais d'un autre locataire


def test_at_most_one_frame_per_second():
    lv, bus, clock = _live()
    for _ in range(3):
        lv.observe("tn_a", "th1", "alice", "browser_scroll", {}, "ok", False,
                   {"images": img(), "meta": act("browser_scroll")})
        clock.t += 0.3
    assert len(bus.of("browser.frame")) == 1
    clock.t += 1
    lv.observe("tn_a", "th1", "alice", "browser_scroll", {}, "ok", False, {"images": img(), "meta": act("browser_scroll")})
    assert len(bus.of("browser.frame")) == 2


def test_sensitive_from_browser_is_never_captured_even_if_an_image_arrives():
    lv, bus, clock = _live()
    lv.observe("tn_a", "th1", "alice", "browser_click", {"ref": "e2"}, "ok", False,
               {"images": img(webp(tag=b"PIXELS")), "meta": act("browser_click", label="Mot de passe", sensitive=True)})
    f = bus.of("browser.frame")[-1]
    assert f["sensitive"] is True and f["frame_url"] is None and f["w"] is None
    assert lv.frames.count() == 0  # aucun pixel gardé
    sid = f["session_id"]
    assert lv.get(sid, "tn_a", "alice")["last_frame"]["frame_url"] is None


def test_kernel_sensitivity_after_typing_a_password_until_navigation():
    lv, bus, clock = _live()
    lv.observe("tn_a", "th1", "alice", "browser_type", {"ref": "e7", "element": "Mot de passe", "text": "hunter2"},
               "ok", False, {"images": img(), "meta": act("browser_type", url="https://bank.example/login")})
    assert bus.of("browser.frame")[-1]["sensitive"] is True and lv.frames.count() == 0  # la frame qui suit un type
    clock.t += 2
    lv.observe("tn_a", "th1", "alice", "browser_click", {"ref": "e9"}, "ok", False,
               {"images": img(), "meta": act("browser_click", url="https://bank.example/login", label="Suivant")})
    assert bus.of("browser.frame")[-1]["sensitive"] is True and lv.frames.count() == 0  # toujours non vide
    clock.t += 2
    lv.observe("tn_a", "th1", "alice", "browser_type", {"ref": "e7", "element": "Mot de passe", "text": ""},
               "ok", False, {"images": img(), "meta": act("browser_type", url="https://bank.example/login")})
    assert bus.of("browser.frame")[-1]["sensitive"] is False  # champ vidé : levée
    clock.t += 2
    lv.observe("tn_a", "th1", "alice", "browser_type", {"ref": "e8", "element": "Code", "text": "[secret:bank-pin]"},
               "ok", False, {"images": img(), "meta": act("browser_type", url="https://bank.example/login")})
    assert bus.of("browser.frame")[-1]["sensitive"] is True  # valeur du coffre
    clock.t += 2
    lv.observe("tn_a", "th1", "alice", "browser_navigate", {"url": "https://bank.example/home"}, "ok", False,
               {"images": img(), "meta": act("browser_navigate", url="https://bank.example/home")})
    assert bus.of("browser.frame")[-1]["sensitive"] is False and lv.frames.count() == 2  # navigation : levée


# ── fin, passation, reprise ──

def test_ended_emitted_once_on_turn_end_and_on_timeout():
    lv, bus, clock = _live()
    lv.observe("tn_a", "th1", "alice", "browser_navigate", {"url": "https://a.example"}, "ok", False, {})
    assert lv.end_thread("tn_a", "th1", "done") and not lv.end_thread("tn_a", "th1", "done")
    assert [e["reason"] for e in bus.of("browser.ended")] == ["done"]
    lv.observe("tn_a", "th2", "alice", "browser_click", {"ref": "e1"}, "ok", False, {"images": img()})
    sid = bus.of("browser.action")[-1]["session_id"]
    assert lv.frames.count(sid) == 1
    clock.t += 301
    lv.sweep()
    ended = bus.of("browser.ended")[-1]
    assert ended == {"session_id": sid, "seq": ended["seq"], "reason": "timeout", "_tenant": "tn_a",
                     "_audience": ["alice"]}
    assert lv.frames.count(sid) == 0  # purgé à la fin
    assert lv.get(sid, "tn_a", "alice")["state"] == "ended"


def test_captcha_always_hands_off_and_gestures_are_refused():
    lv, bus, clock = _live()
    note = lv.observe("tn_a", "th1", "alice", "browser_click", {"ref": "e1"}, "ok", False,
                      {"images": img(), "meta": {**act("browser_click"),
                                                 "nokker_block": {"kind": "captcha", "vendor": "recaptcha"}}})
    assert note and "Ne tente PAS" in note
    h = bus.of("browser.handoff")[-1]
    assert h["reason"] == "captcha" and h["takeover_url"].startswith("/v1/browser/takeover/") and h["expires_at"]
    assert set(h) >= {"session_id", "seq", "reason", "url_host", "takeover_url", "expires_at"}
    assert lv.refuse("tn_a", "th1", "browser_click") and lv.refuse("tn_a", "th1", "browser_type")
    assert lv.refuse("tn_a", "th1", "browser_snapshot") and lv.refuse("tn_a", "th1", "browser_read_text")
    # un CHALLENGE annoncé dans le texte (browser_handoff du pool) passe aussi la main
    lv2, bus2, _ = _live()
    lv2.observe("tn_a", "th1", "alice", "browser_navigate", {"url": "https://x.example"},
                "⚠ CHALLENGE — intervention humaine requise (DataDome)", False, {})
    assert bus2.of("browser.handoff")[-1]["reason"] == "captcha"


def _handoff(lv, bus, reason="2fa"):
    lv.observe("tn_a", "th1", "alice", "browser_click", {"ref": "e1"}, "ok", False,
               {"meta": {**act("browser_click"), "nokker_block": {"kind": reason}}})
    h = bus.of("browser.handoff")[-1]
    return h["session_id"], h["takeover_url"].rsplit("/", 1)[1]


def test_takeover_is_single_use_and_owner_only():
    lv, bus, clock = _live()
    lv.screen_ticket = lambda pool_sid: f"https://pool.example/vnc?ticket={pool_sid}"
    sid, code = _handoff(lv, bus)
    lv.current("tn_a", "th1").pool_session_id = "s1"
    assert lv.takeover(code, "tn_a", "mallory")[0] == 403  # autre identité : refusé, code intact
    assert lv.takeover(code, "tn_b", "alice")[0] == 403  # autre locataire
    status, out = lv.takeover(code, "tn_a", "alice")
    assert status == 200 and out["session_id"] == sid and out["cookie"]
    assert lv.takeover(code, "tn_a", "alice")[0] == 410  # rejouée
    assert lv.screen(sid, out["cookie"]) == (302, "https://pool.example/vnc?ticket=s1")
    assert lv.screen(sid, "faux")[0] == 403
    clock.t += 3600
    assert lv.takeover(code, "tn_a", "alice")[0] in (403, 410)


def test_resume_owner_only_and_idempotent():
    lv, bus, clock = _live()
    sid, _code = _handoff(lv, bus, "login")
    assert lv.resume(sid, "tn_a", "bob")[0] == 404  # hors audience : inconnue
    lv.current("tn_a", "th1").audience.add("bob")
    assert lv.resume(sid, "tn_a", "bob")[0] == 403  # membre du fil, pas titulaire
    assert lv.resume(sid, "tn_a", "alice") == (200, {"session_id": sid, "state": "running",
                                                     "seq": bus.of("browser.resumed")[-1]["seq"]})
    assert lv.resume(sid, "tn_a", "alice")[1]["state"] == "running"
    assert len(bus.of("browser.resumed")) == 1  # idempotent : un seul événement
    assert lv.takeover(_code, "tn_a", "alice")[0] == 410  # le lien meurt à la reprise
    assert lv.refuse("tn_a", "th1", "browser_click") is None


def test_bubble_browser_only_on_transitions():
    hub = FakeHub()
    lv, bus, clock = _live(hub=hub)
    for _ in range(4):
        lv.observe("tn_a", "th1", "alice", "browser_scroll", {}, "ok", False, {"images": img(), "meta": act("browser_scroll")})
        clock.t += 2
    assert len(hub.created) == 1 and hub.updated == []  # aucune mise à jour par frame
    th, kind, title, state, status = hub.created[0]
    assert kind == "browser" and state["state"] == "running" and state["session_id"].startswith("bs_")
    lv.handoff(lv.current("tn_a", "th1"), "2fa")
    lv.resume(state["session_id"], "tn_a", "alice")
    lv.end_thread("tn_a", "th1", "done")
    assert [u[1]["state"] for u in hub.updated] == ["handoff", "running", "ended"]
    assert hub.updated[-1][2] == "done"


def test_approval_context_browser():
    lv, bus, clock = _live()
    lv.observe("tn_a", "th1", "alice", "browser_navigate", {"url": "https://a.example"}, "ok", False, {})
    s = lv.current("tn_a", "th1")
    clock.t += 1
    assert lv.context_for("th1", clock.t) == {"session_id": s.id, "seq": s.seq}
    assert lv.context_for("th-autre", clock.t) is None


# ── au travers du client du pool (forme `_meta` de sites-chrome (nokker/v3-frames), fixtures) ──

class Pool:
    def __call__(self, req):
        assert req.url.path == "/sessions", req.url.path  # le pool de la bêta n'a ni /frame ni /screen-ticket
        return httpx.Response(201, json={"session_id": "s1", "mcp_url": "http://pool/s/s1/mcp", "token": "tok",
                                         "expires_at": "2099-01-01T00:00:00Z"})


class SharedBrowserMcp:
    """Résultats MCP tels que sites-chrome (nokker/v3-frames) les rend : (texte, erreur, code, {images, meta})."""

    script: list = []

    def __init__(self, url, headers):
        pass

    def call(self, name, args):
        meta = SharedBrowserMcp.script.pop(0) if SharedBrowserMcp.script else {}
        images = [] if (meta.get("nokker_action") or {}).get("sensitive") else img()
        return f"- Page URL: https://shop.example/p\n- Page Title: Boutique\n- button \"Ajouter\" [ref=e1]\nok {name}", \
            False, None, \
            {"images": images, "meta": meta}


@pytest.fixture
def pooled():
    lv, bus, clock = _live()
    live_mod.configure(lv)
    src = BrowserPoolSource(base_url="http://pool", api_token=lambda: "k",
                            client=httpx.Client(transport=httpx.MockTransport(Pool())), mcp_factory=SharedBrowserMcp)
    yield src, lv, bus, clock
    live_mod.configure(None)


def test_pool_source_feeds_live_and_turn_done_ends(pooled):
    src, lv, bus, clock = pooled
    ctx = ToolContext(thread_id="th1", turn_index=0, channel="api", sender="alice")
    SharedBrowserMcp.script = [act("browser_navigate", url="https://shop.example/p")]
    with tenant_scope("tn_a"):
        src.call("browser_navigate", {"url": "https://shop.example/p"}, ctx)
        f = bus.of("browser.frame")[-1]
        assert f["title"] == "Boutique" and f["url_host"] == "shop.example" and f["frame_url"]
        clock.t += 2
        SharedBrowserMcp.script = [{**act("browser_click"), "nokker_block": {"kind": "captcha", "vendor": "hcaptcha"}}]
        r = src.call("browser_click", {"ref": "e1"}, ctx)
        assert "Passation à l'humain (captcha)" in r.content
        refused = src.call("browser_click", {"ref": "e1"}, ctx)
        assert refused.is_error and "Refusé" in refused.content
        sid = bus.of("browser.handoff")[-1]["session_id"]
        code = bus.of("browser.handoff")[-1]["takeover_url"].rsplit("/", 1)[1]
        status, out = lv.takeover(code, "tn_a", "alice")
        assert lv.screen(sid, out["cookie"]) == (503, None)  # repli du pool : pas d'écran avant l'après-démo
        src.turn_done("th1")
    assert bus.of("browser.ended")[-1]["reason"] == "done" and lv.frames.count(sid) == 0


# ── rule_label ──

def _known_rules() -> set[str]:
    rules: set[str] = set()
    for p in (ROOT / "nokker").rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        rules |= set(re.findall(r'rule="([a-z_][a-z0-9_:\-]*)"', text))
        rules |= set(re.findall(r'^[A-Z_]*RULE[A-Z_]* = "([a-z_][a-z0-9_:\-]*)"', text, re.MULTILINE))
    return rules


def test_rule_label_for_every_known_rule():
    from nokker.approvals.rule_labels import LANGS, rule_label, rule_labels, rule_source

    rules = _known_rules() | {"effect:send_external:relayed", "effect:destructive:relayed", "jobs:apply:workday",
                              "relay:mail:mail_send", "provenance:mail", "permissions:accept_edits",
                              "effect:send_external:sous-agent", "effect:write:mention:mail",
                              "household:turns_per_day", "family:calendar:invalid"}
    assert {"effect:send_external", "effect:spend", "effect:destructive:go", "family:calendar",
            "household:guardian", "payments:to_pay"} <= rules
    missing = sorted(r for r in rules for lg in LANGS if rule_label(r, lg) == r or not rule_label(r, lg).strip())
    assert missing == []
    # une carte DEMANDE un accord, elle ne refuse pas (relecture de la vidéo V3) : jamais un libellé de refus
    assert rule_label("effect:send_external", "fr") == "Envoi vers l'extérieur — ton accord est requis"
    assert rule_label("effect:send_external:allowlist", "en") == "Sending outside — needs your OK"
    assert rule_label("effect:send_external", "de") == "Versand nach aussen — braucht dein OK"
    assert rule_label("effect:spend", "it") == "Spesa — serve il tuo OK"
    assert rule_label("effect:send_external:relayed", "fr") == \
        "Envoi vers l'extérieur (demandé à distance) — ton accord est requis"
    refusal = re.compile(r"refus|interdit|invalide|bloqu|rejected|refused|forbidden|denied|verboten|abgelehnt|vietat|"
                         r"rifiut", re.IGNORECASE)
    assert [r for r in rules for lg in LANGS if refusal.search(rule_label(r, lg))] == []
    assert rule_label("inconnue:totalement") == "inconnue:totalement"  # repli sur la règle
    assert set(rule_labels("effect:spend")) == set(LANGS)
    assert rule_source("household:guardian") == {"kind": "system"}


def test_request_lang():
    from nokker.approvals.rule_labels import rule_label, set_request_lang

    set_request_lang("it-CH,it;q=0.9,en;q=0.5", "/v1/approvals")
    assert rule_label("effect:spend") == "Spesa — serve il tuo OK"
    set_request_lang("en", "/v1/approvals?lang=de")
    assert rule_label("effect:spend") == "Ausgabe — braucht dein OK"
    set_request_lang(None, None)
    assert rule_label("effect:spend") == "Dépense — ton accord est requis"


def test_frame_bytes_survive_base64_from_mcp():
    from nokker.tools.browser_pool import _extra_of

    class Block:
        type = "image"
        mime_type = "image/webp"
        data = base64.b64encode(webp()).decode()

    class Result:
        content = [Block()]
        meta = {"nokker_action": {"tool": "browser_click"}}

    extra = _extra_of(Result())
    assert extra["images"][0]["data"] == webp() and extra["meta"]["nokker_action"]["tool"] == "browser_click"


# ── contrat final sites-chrome (nokker/v3-frames 39db2b2) ──

def _gesture_result(sensitive=False, with_image=True):
    import mcp.types as mt

    content = [{"type": "text", "text": "- Page URL: https://shop.example/p\n- button \"OK\" [ref=e1]\nok"}]
    if with_image:
        content.append({"type": "image", "data": base64.b64encode(webp()).decode(), "mimeType": "image/webp"})
    return mt.CallToolResult.model_validate({"content": content, "_meta": {"nokker_action": {
        "seq": 3, "tool": "browser_click", "label": "OK", "url": "https://shop.example/p", "t": 1, "ok": True,
        "sensitive": sensitive, "frame": True}}})


class FakeIo:
    def __init__(self, result):
        self.result = result

    def run(self, coro):
        coro.close()
        return self.result


def _real_caller(result):
    from nokker.tools.browser_pool import _McpCaller

    def factory(url, headers):
        c = object.__new__(_McpCaller)
        c._io = FakeIo(result)
        return c
    return factory


def test_model_never_sees_image_placeholder_of_a_gesture(pooled):
    src, lv, bus, clock = pooled
    src.mcp_factory = _real_caller(_gesture_result())
    ctx = ToolContext(thread_id="th1", turn_index=0, channel="api", sender="alice")
    with tenant_scope("tn_a"):
        src.call("browser_snapshot", {}, ctx)  # instantané : la référence e1 existe
        r = src.call("browser_click", {"ref": "e1"}, ctx)
    assert "[ImageContent]" not in r.content and "ImageContent" not in r.content
    assert bus.of("browser.frame")[-1]["frame_url"]  # la capture est bien lue à part, pour la vue en direct
    from nokker.tools.mcp import text_of

    assert "[ImageContent]" in text_of(_gesture_result())  # sans l'option, l'ancienne trace (captures demandées)


def test_pool_fallback_actions_from_what_the_kernel_knows(pooled):
    """Pool de la bêta tel qu'il est : texte seul, ni `_meta` ni image → `browser.action` depuis l'outil et ses
    arguments, aucune frame, aucune erreur."""
    src, lv, bus, clock = pooled

    class PlainPool:
        def __init__(self, url, headers):
            pass

        def call(self, name, args):
            return "- Page URL: https://shop.example/p\n- button \"OK\" [ref=e1]\nok", False, None

    src.mcp_factory = PlainPool
    ctx = ToolContext(thread_id="th1", turn_index=0, channel="api", sender="alice")
    with tenant_scope("tn_a"):
        r1 = src.call("browser_navigate", {"url": "https://shop.example/p"}, ctx)
        r2 = src.call("browser_click", {"ref": "e1", "element": "Bouton OK"}, ctx)
    assert not r1.is_error and not r2.is_error
    kinds = [(a["kind"], a["target_label"], a["url_host"]) for a in bus.of("browser.action")]
    assert kinds == [("navigate", "shop.example", "shop.example"), ("click", "Bouton OK", "shop.example")]
    assert all(f["frame_url"] is None for f in bus.of("browser.frame")) and lv.frames.count() == 0


# ── passation : tout refusé ; valeurs sensibles masquées avant le modèle (§4) ──

def test_snapshot_refused_during_handoff_and_browser_not_called(pooled):
    src, lv, bus, clock = pooled
    ctx = ToolContext(thread_id="th1", turn_index=0, channel="api", sender="alice")
    SharedBrowserMcp.script = [{**act("browser_navigate"), "nokker_block": {"kind": "2fa", "detail": "code"}}]
    with tenant_scope("tn_a"):
        src.call("browser_navigate", {"url": "https://bank.example/login"}, ctx)
        assert bus.of("browser.handoff")
        before = len(SharedBrowserMcp.script)
        for tool in ("browser_snapshot", "browser_read_text", "browser_find", "browser_take_screenshot"):
            r = src.call(tool, {}, ctx)
            assert r.is_error and "la personne a la main" in r.content and "lecture comprise" in r.content
        assert len(SharedBrowserMcp.script) == before  # rien n'a atteint le navigateur
        sid = bus.of("browser.handoff")[-1]["session_id"]
        lv.resume(sid, "tn_a", "alice")
        assert not src.call("browser_snapshot", {}, ctx).is_error


def test_sensitive_value_never_reaches_the_model_even_if_raw_has_it(pooled):
    src, lv, bus, clock = pooled
    raw = ("- Page URL: https://bank.example/login\n"
           '- textbox "Identifiant" [ref=e1]: alex\n'
           '- textbox "Mot de passe" [ref=e2] [password]: hunter2-secret\n'
           '- textbox "Code de vérification" [ref=e3]: 482913\n'
           '- textbox "Card number" [ref=e4] [autocomplete=cc-number]: 4242424242424242\n'
           '- textbox "Code" [ref=e5] [data-nk-secret]: 771100\n'
           '- heading "Code de vérification" [level=1] [ref=e6]')

    class RawLeak:
        def __init__(self, url, headers):
            pass

        def call(self, name, args):
            return raw, False, None

    src.mcp_factory = RawLeak
    ctx = ToolContext(thread_id="th1", turn_index=0, channel="api", sender="alice")
    with tenant_scope("tn_a"):
        out = src.call("browser_snapshot", {}, ctx).content + src.call("browser_read_text", {}, ctx).content
    for secret in ("hunter2-secret", "482913", "4242424242424242", "771100"):
        assert secret not in out
    assert out.count("[masqué]") == 8 and "alex" in out and 'heading "Code de vérification"' in out


# ── browser.action : seulement après une tentative réelle, avec `ok` ──

def test_failed_gesture_emits_ok_false():
    lv, bus, clock = _live()
    lv.observe("tn_a", "th1", "alice", "browser_click", {"ref": "e1"}, "élément introuvable", True,
               {"meta": act("browser_click", ok=False)})
    lv.observe("tn_a", "th1", "alice", "browser_scroll", {}, "erreur", True, {})  # sans _meta : l'erreur fait foi
    lv.observe("tn_a", "th1", "alice", "browser_click", {"ref": "e1"}, "ok", False, {"meta": act("browser_click", ok=True)})
    assert [a["ok"] for a in bus.of("browser.action")] == [False, False, True]


def test_gesture_refused_before_the_browser_emits_no_action(pooled):
    src, lv, bus, clock = pooled
    ctx = ToolContext(thread_id="th1", turn_index=0, channel="api", sender="alice")
    with tenant_scope("tn_a"):
        r = src.call("browser_click", {"ref": "e1"}, ctx)  # référence absente : refusé
    assert r.is_error and not bus.of("browser.action")


def test_gesture_waiting_for_a_card_emits_no_action_until_executed(pooled):
    """Porte HITL du noyau sur un clic : la carte porte context.browser, aucun browser.action ; exécuté après
    l'approbation (même chemin que l'exécuteur), l'action part avec son `ok`."""
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.core.ports import Completion
    from nokker.core.turn import Message, Tier, ToolCall, TurnInput, Usage
    from nokker.tools.registry import Registry
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals

    import time

    src, lv, bus, clock = pooled
    lv.clock = time.time  # même horloge que les cartes, comme en production
    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)

    class Ship:
        def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
            if not any(m.get("role") == "tool" for m in messages):
                calls = [ToolCall(id="c0", name="browser_snapshot", arguments={}),
                         ToolCall(id="c1", name="browser_click", arguments={"ref": "e1"})]
                return Completion(message=Message(role="assistant", tool_calls=calls), usage=Usage(prompt=5),
                                  model="t", latency_s=0.0)
            return Completion(message=Message(role="assistant", content="en attente"), usage=Usage(prompt=5),
                              model="t", latency_s=0.0)

    from nokker.core.turn import Decision, Gate, Verdict

    class ClickNeedsCard:
        def action(self, call, spec, turn):
            if call.name == "browser_click":
                return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:spend",
                                reason="test", strength="strong")]
            return [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="allow")]

        def switch(self, reply, turn, tier):
            return [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=1, rule="ok")]

    kernel.gates = ClickNeedsCard()
    kernel.tools = Registry(sources=[src])
    kernel.providers = {Tier.SHIP: Ship()}
    SharedBrowserMcp.script = []
    with tenant_scope("tn_a"):
        kernel.run_turn(TurnInput(thread_id="th1", user_text="accepte la boîte", channel="api", sender="alice"))
    cards = approvals.pending()
    assert [c.tool_name for c in cards] == ["browser_click"]
    assert [a["kind"] for a in bus.of("browser.action")] == ["read"]  # l'instantané, pas le geste en attente
    from nokker.api.server import approval_to_json

    assert approval_to_json(cards[0])["context"]["browser"]["session_id"] == bus.of("browser.action")[0]["session_id"]
    # carte approuvée : l'exécuteur appelle l'outil avec approval_id → l'action part à ce moment-là
    with tenant_scope("tn_a"):
        # (le tour fini a rendu ses références : le pool r44 exige un instantané avant le clic approuvé)
        src.call("browser_snapshot", {}, ToolContext(thread_id="th1", turn_index=0, channel="api", sender="alice"))
        src.call("browser_click", {"ref": "e1"},
                 ToolContext(thread_id="th1", turn_index=0, channel="api", sender="alice", approval_id=cards[0].id))
    last = bus.of("browser.action")[-1]
    assert last["kind"] == "click" and last["ok"] is True
