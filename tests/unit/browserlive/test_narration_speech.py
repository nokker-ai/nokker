"""Narration d'une désignation du navigateur (`POST /v1/browser/sessions/{id}/narrations/{seq}/speech/timed`) PAR LE
SERVEUR HTTP RÉEL. Prouve : le texte synthétisé est CELUI que le noyau a émis pour ce `seq` (`why`, repli `label`),
jamais un texte du client ; titulaire 200 (corps TimedSpeech, no-store) ; autre compte 404 ; seq inconnu ou qui n'est
pas une désignation 404 ; parole éteinte 503 tts_off ; erreur du fournisseur relayée (429 + Retry-After) ; cache par
(session, seq) : un rejeu ne resynthétise pas ; sur Lokkal, passerelle 403, titulaire local sur la machine 200.
La capacité `browser_narration` est tenue par tests/unit/api/test_capabilities.py."""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from nokker.api import server as server_mod
from nokker.api.server import make_handler
from nokker.browserlive import live as live_mod
from nokker.browserlive.frames import FrameStore
from nokker.browserlive.live import BrowserLive
from nokker.tenancy.context import INTERNAL_TENANT
from nokker.voice import web as voice_web
from tests.unit.browserlive.test_browser_live import act, img
from tests.unit.browserlive.test_browser_live_http import _session, req

TIMED = {"audio": "QUJD", "mime": "audio/mpeg", "words": ["Ici"], "wtimes": [0], "wdurations": [200]}


class FakeSpeech:
    def __init__(self):
        self.calls, self.fail = [], None

    def timed(self, **kw):
        self.calls.append(kw)
        if self.fail is not None:
            raise self.fail
        return dict(TIMED)


@pytest.fixture
def speech(monkeypatch):
    sp = FakeSpeech()
    monkeypatch.setattr(voice_web, "service", lambda: sp)
    server_mod._NARRATION_CACHE.clear()
    yield sp
    server_mod._NARRATION_CACHE.clear()


@pytest.fixture
def srv():
    from tests.unit.api.test_server import make_kernel

    lv = BrowserLive(publish=lambda *a, **k: None, frames=FrameStore())
    live_mod.configure(lv)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {"tok-a": "alice", "tok-b": "bob"}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address[1], lv
    finally:
        httpd.shutdown()
        live_mod.configure(None)
        from nokker import persona as persona_mod

        persona_mod.configure(None)


def _with_points(lv):
    s = _session(lv)
    s.points.append({"seq": 41, "ts": "2026-11-11T09:00:00Z", "box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1},
                     "label": "Bouton Réserver", "why": "Ici, le bouton pour réserver la chambre."})
    s.points.append({"seq": 44, "ts": "2026-11-11T09:00:05Z", "box": {"x": 0.3, "y": 0.3, "w": 0.2, "h": 0.1},
                     "label": "Prix total", "why": ""})
    return s


def _url(s, seq):
    return f"/v1/browser/sessions/{s.id}/narrations/{seq}/speech/timed"


def test_holder_gets_the_kernel_text_spoken_never_the_client_text(srv, speech):
    port, lv = srv
    s = _with_points(lv)
    st, h, raw = req(port, "POST", _url(s, 41), "tok-a", {"lang": "fr", "text": "Dis autre chose", "reply": "pirate"})
    assert st == 200 and json.loads(raw) == TIMED and h["Cache-Control"] == "no-store"
    assert speech.calls[-1]["reply"] == "Ici, le bouton pour réserver la chambre."  # le texte émis, rien du client
    assert speech.calls[-1]["account"] == "alice" and speech.calls[-1]["lang"] == "fr"
    st, _, _ = req(port, "POST", _url(s, 44), "tok-a", {})
    assert st == 200 and speech.calls[-1]["reply"] == "Prix total"  # sans `why` : le libellé émis


def test_replay_is_cached_and_not_resynthesized(srv, speech):
    port, lv = srv
    s = _with_points(lv)
    for _ in range(3):
        assert req(port, "POST", _url(s, 41), "tok-a", {"lang": "fr"})[0] == 200
    assert len(speech.calls) == 1
    assert req(port, "POST", _url(s, 41), "tok-a", {"lang": "en"})[0] == 200  # autre langue : autre entrée
    assert len(speech.calls) == 2


def test_refusals(srv, speech):
    port, lv = srv
    s = _with_points(lv)
    assert req(port, "POST", _url(s, 41), "tok-b", {})[0] == 404  # autre compte
    assert req(port, "POST", _url(s, 7), "tok-a", {})[0] == 404  # seq inconnu
    assert req(port, "POST", _url(s, s.actions[-1]["seq"]), "tok-a", {})[0] == 404  # une action, pas une désignation
    assert req(port, "POST", "/v1/browser/sessions/bs_0123456789abcdef/narrations/41/speech/timed", "tok-a", {})[0] == 404
    assert req(port, "POST", _url(s, 41))[0] == 401
    assert req(port, "GET", _url(s, 41), "tok-a")[0] in (404, 405)
    assert speech.calls == []


def test_speech_off_and_provider_errors(srv, speech, monkeypatch):
    port, lv = srv
    s = _with_points(lv)
    speech.fail = voice_web.SpeechError(429, "rate_limited", 30)
    st, h, raw = req(port, "POST", _url(s, 41), "tok-a", {})
    assert st == 429 and h.get("Retry-After") == "30" and json.loads(raw)["error"]["code"] == "rate_limited"
    speech.fail = voice_web.SpeechError(502, "provider_failed")
    assert req(port, "POST", _url(s, 41), "tok-a", {})[0] == 502
    assert server_mod._NARRATION_CACHE == {}  # un échec ne se met jamais en cache
    monkeypatch.setattr(voice_web, "service", lambda: None)
    st, _, raw = req(port, "POST", _url(s, 41), "tok-a", {})
    assert st == 503 and json.loads(raw)["error"]["code"] == "tts_off"


def test_lokkal_machine_only(speech, tmp_path):
    from nokker.local.api import LOCAL_OWNER
    from tests.unit.api.test_server import make_kernel
    from tests.unit.test_local_api import _api

    api = _api(tmp_path)
    lv = BrowserLive(publish=lambda *a, **k: None, frames=FrameStore())
    live_mod.configure(lv)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {api.token: LOCAL_OWNER}, local_api=api))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        lv.observe(INTERNAL_TENANT, "th1", LOCAL_OWNER, "browser_click", {"ref": "e1"}, "ok", False,
                   {"images": img(), "meta": act("browser_click", url="https://shop.example/p")}, pool_session_id="s1")
        s = lv.current(INTERNAL_TENANT, "th1")
        s.points.append({"seq": 41, "label": "Bouton", "why": "Ici, le bouton."})
        host = {"Host": "127.0.0.1:8742"}
        from nokker import persona as persona_mod
        from tests.unit.voice.test_avatar_speech import PERSONA

        persona_mod.configure(persona_mod.from_config(PERSONA, {}, None))
        persona_mod.service().put(LOCAL_OWNER, INTERNAL_TENANT, {"voice_id": "leo"})
        st, _, _ = req(port, "POST", _url(s, 41), api.token, {}, headers=host)
        assert st == 200 and speech.calls[-1]["reply"] == "Ici, le bouton." and speech.calls[-1]["account"] == LOCAL_OWNER
        assert speech.calls[-1]["voice"] == "LeoVoice456"  # la voix choisie par le titulaire local
        st, _, _ = req(port, "POST", _url(s, 41), api.token, {}, headers={**host, "X-Nokker-Relay": "1"})
        assert st == 403  # par la passerelle : jamais
        assert req(port, "POST", _url(s, 41), "faux", {}, headers=host)[0] == 401
    finally:
        httpd.shutdown()
        live_mod.configure(None)
