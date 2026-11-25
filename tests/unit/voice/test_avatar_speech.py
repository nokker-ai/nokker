"""Avatar parlant (V6, docs/AVATAR.md) : parole HORODATÉE depuis un FAUX ElevenLabs local (`…/with-timestamps`), « Ton
Nokker » (nom, voix, avatar en catalogues fermés), et annonce du brief par un MUR (jeton nkg_). Vrai `make_handler`,
aucun appel réel au fournisseur."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from nokker import persona as persona_mod
from nokker.ambient import glance as gl
from nokker.config import NokkerConfig
from nokker.tenancy.limits import MemoryUsage
from nokker.voice import web as voice_web
from nokker.voice.timed import words_from_alignment
from nokker.voice.tts import ElevenLabsTTS
from tests.unit.voice.test_web_speech import KEY, KEY_ENV, REPLY, _scrub
from tests.unit.voice.test_web_speech import fake  # noqa: F401 — fixture réutilisée
from tests.unit.voice.test_web_speech import world as _world  # noqa: F401 — fixture réutilisée

MP3 = b"\xff\xfb\x90\x00" + bytes(200)


def _alignment(text):
    chars = list(text)
    return {"characters": chars, "character_start_times_seconds": [i * 0.05 for i in range(len(chars))],
            "character_end_times_seconds": [i * 0.05 + 0.05 for i in range(len(chars))]}


class FakeTimed:
    """Faux ElevenLabs `with-timestamps` : JSON {audio_base64, alignment} ; garde chemins et corps reçus."""

    def __init__(self, status=200):
        self.status, self.requests = status, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.requests.append({"path": self.path, "body": body,
                                       "headers": {k.lower(): v for k, v in self.headers.items()}})
                if outer.status != 200 or "with-timestamps" not in self.path:
                    self.send_response(outer.status if outer.status != 200 else 404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                out = json.dumps({"audio_base64": base64.b64encode(MP3).decode(),
                                  "alignment": _alignment(body["text"])}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)


@pytest.fixture
def timed():
    s = FakeTimed()
    yield s
    s.close()


def _speech(url):
    return voice_web.from_config({"enabled": True, "provider": "elevenlabs",
                                  "elevenlabs": {"voice_id": "NinaVoice123", "base_url": url}}, _scrub,
                                 usage=MemoryUsage())


PERSONA = {"enabled": True, "default_name": "Nina",
           "voices": [{"id": "nina", "label": "Nina", "langs": ["fr", "en"]},
                      {"id": "leo", "label": "Léo", "langs": ["fr"], "elevenlabs": {"fr": "LeoVoice456"}}]}


@pytest.fixture
def persona():
    persona_mod.configure(persona_mod.from_config(PERSONA, {}, None))
    yield persona_mod.service()
    persona_mod.configure(None)


# ───────────────────────── alignement et fournisseur ─────────────────────────

def test_words_from_alignment_groups_characters_into_timed_words():
    words, wtimes, wdurations = words_from_alignment(_alignment("Bon  jour Ana."))
    assert words == ["Bon", "jour", "Ana."]
    assert wtimes == [0, 250, 500] and wdurations == [150, 200, 200]


@pytest.mark.parametrize("bad", [None, {}, {"characters": ["a"], "character_start_times_seconds": [],
                                            "character_end_times_seconds": []},
                                 {"characters": ["a"], "character_start_times_seconds": ["x"],
                                  "character_end_times_seconds": [1]}])
def test_a_broken_alignment_gives_no_words_never_an_error(bad):
    assert words_from_alignment(bad) == ([], [], [])


def test_timed_request_shape_and_voice_override(timed):
    tts = ElevenLabsTTS(voice_id="NinaVoice123", base_url=timed.url)
    out = tts.timed("Bonjour Ana.", "fr", voice="LeoVoice456")
    req = timed.requests[-1]
    assert req["path"].startswith("/v1/text-to-speech/LeoVoice456/with-timestamps?")
    assert req["headers"]["accept"] == "application/json" and req["headers"]["xi-api-key"] == KEY
    assert base64.b64decode(out["audio"]) == MP3 and out["mime"] == "audio/mpeg"
    assert out["words"] == ["Bonjour", "Ana."] and len(out["wtimes"]) == len(out["wdurations"]) == 2
    tts.timed("Salut", "fr")
    assert timed.requests[-1]["path"].startswith("/v1/text-to-speech/NinaVoice123/with-timestamps?")


# ───────────────────────── « Ton Nokker » : catalogue, choix, erreurs ─────────────────────────

def test_catalog_is_closed_and_defaults_apply_until_a_choice(persona):
    cat = persona.catalog.to_json("fr")
    assert [v["id"] for v in cat["voices"]] == ["nina", "leo"]
    assert "LeoVoice456" not in json.dumps(cat)  # l'id fournisseur ne sort jamais
    assert [(a["url"], a["thumb"], a["kind"]) for a in cat["avatars"]] == [
        ("/avatars/nina-v1/", "/avatars/nina-v1.webp", "rig2d")]
    assert cat["defaults"] == {"name": "Nina", "voice_id": "nina", "avatar": {"id": "nina", "version": 1}}
    assert persona.get("us_alice") == {"name": "Nina", "voice_id": "nina",
                                       "avatar": {"id": "nina", "version": 1}, "defaulted": True}


@pytest.mark.parametrize("body, code", [
    ({"name": ""}, "bad_name"), ({"name": "x" * 25}, "bad_name"), ({"name": "R2D2"}, "bad_name"),
    ({"name": "<b>Nina</b>"}, "bad_name"), ({"name": 7}, "bad_name"),
    ({"voice_id": "LeoVoice456"}, "bad_voice"), ({"voice_id": "inconnue"}, "bad_voice"),
    ({"avatar": {"id": "brunette", "version": 1}}, "bad_avatar"),
    ({"avatar": {"id": "nina", "version": 2}}, "bad_avatar"), ({"avatar": {"id": "nokker-nuit", "version": 1}}, "bad_avatar"),
    ({"avatar": {"id": "nina", "version": True}}, "bad_avatar"), ({"avatar": "nina"}, "bad_avatar"),
    ({}, "bad_request"), ({"soul": "x"}, "bad_request")])
def test_values_outside_the_catalogs_are_refused(persona, body, code):
    with pytest.raises(persona_mod.PersonaError) as e:
        persona.put("us_alice", "tn_a", body)
    assert e.value.status == 400 and e.value.code == code
    assert persona.get("us_alice")["defaulted"] is True


def test_a_choice_is_kept_and_the_chosen_voice_drives_synthesis(persona):
    out = persona.put("us_alice", "tn_a", {"name": "  Léa-Marie  ", "voice_id": "leo",
                                           "avatar": {"id": "nina", "version": 1}})
    assert out == {"name": "Léa-Marie", "voice_id": "leo", "avatar": {"id": "nina", "version": 1},
                   "defaulted": False}
    assert persona.provider_voice("us_alice", "fr") == "LeoVoice456"
    assert persona.provider_voice("us_alice", "en") is None  # pas de voix anglaise : la voix configurée
    assert persona.provider_voice("us_bob", "fr") is None
    assert persona.export("us_alice") == {"name": "Léa-Marie", "voice_id": "leo",
                                          "avatar": {"id": "nina", "version": 1}}
    assert persona.export("us_bob") is None


def test_preview_texts_are_frozen():
    """Phrases d'aperçu dites à voix haute : texte revu, empreinte gelée (tout changement repasse par la revue)."""
    got = hashlib.sha256(json.dumps(persona_mod.PREVIEW_TEXT, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert got == PREVIEW_SHA256


PREVIEW_SHA256 = "7ef80d4db99dbdfc69f4749530339333723c4475be4270d11e4d208185d31533"


def test_flag_off_builds_nothing():
    assert persona_mod.from_config({}, {}, None) is None
    assert persona_mod.from_config({"enabled": False}, {}, None) is None


# ───────────────────────── routes, vrai make_handler ─────────────────────────

@pytest.fixture
def world(_world, timed, persona):  # noqa: F811 — fixture de test_web_speech enveloppée
    voice_web.configure(_speech(timed.url))
    yield _world
    voice_web.configure(None)


def test_persona_routes(world):
    call = world[0]
    st, _, body = call("GET", "/v1/persona/catalog")
    assert st == 200 and json.loads(body)["defaults"]["voice_id"] == "nina"
    st, _, body = call("PUT", "/v1/persona", {"name": "Ada", "avatar": {"id": "nina", "version": 1}})
    assert st == 200 and json.loads(body)["name"] == "Ada"
    assert json.loads(call("GET", "/v1/persona")[2])["avatar"]["id"] == "nina"
    assert json.loads(call("GET", "/v1/persona", who="bob")[2])["defaulted"] is True  # chacun le sien
    st, _, body = call("PUT", "/v1/persona", {"voice_id": "zz"})
    assert st == 400 and json.loads(body)["error"]["code"] == "bad_voice"
    assert call("GET", "/v1/persona", who=None)[0] == 401


def test_preview_says_the_fixed_sentence_with_the_chosen_name_and_voice(world, timed):
    call = world[0]
    call("PUT", "/v1/persona", {"name": "Ada"})
    st, headers, body = call("POST", "/v1/persona/preview", {"voice_id": "leo", "lang": "fr"})
    out = json.loads(body)
    assert st == 200 and headers.get("Cache-Control") == "no-store" and out["words"][:4] == ["Bonjour,", "je", "suis",
                                                                                                "Ada."]
    assert timed.requests[-1]["path"].startswith("/v1/text-to-speech/LeoVoice456/with-timestamps")
    st, _, body = call("POST", "/v1/persona/preview", {"voice_id": "nope"})
    assert st == 400 and json.loads(body)["error"]["code"] == "bad_voice"


def test_timed_speech_of_a_reply_uses_the_chosen_voice(world, timed):
    call, tid = world[0], world[1]
    call("PUT", "/v1/persona", {"voice_id": "leo"})
    st, headers, body = call("POST", f"/v1/threads/{tid}/messages/0/speech/timed", {"lang": "fr"})
    out = json.loads(body)
    assert st == 200 and headers.get("Cache-Control") == "no-store"
    assert "Bonjour" in out["words"] and "**Bonjour**" not in out["words"]  # nettoyé comme « Écouter »
    assert timed.requests[-1]["path"].startswith("/v1/text-to-speech/LeoVoice456/with-timestamps")
    assert "https://" not in timed.requests[-1]["body"]["text"]
    assert call("POST", f"/v1/threads/{tid}/messages/1/speech/timed", {})[0] == 409
    assert call("POST", f"/v1/threads/{tid}/messages/0/speech/timed", {}, who="bob")[0] in (403, 404)
    assert REPLY


def test_persona_routes_are_absent_when_the_flag_is_off(world):
    persona_mod.configure(None)
    assert world[0]("GET", "/v1/persona")[0] == 404


# ───────────────────────── annonce du mur (jeton nkg_) ─────────────────────────

class FakePlan:
    def __init__(self):
        self.brief, self.delivery = SimpleNamespace(id="ag_brief"), None

    def get_brief(self, sender):
        return self.brief


class FakeAgents:
    def __init__(self):
        self.summary, self.run_id = "Bonjour. Trois rendez-vous aujourd'hui.", "run_1"

    def list_runs(self, agent_id, limit=1):
        return [SimpleNamespace(id=self.run_id, summary=self.summary)], None


@pytest.fixture
def wall(monkeypatch, timed, persona):
    from nokker.api import server as srv
    from nokker.api.server import configure_ambient, make_handler
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.tenancy.store import MemoryTenancy
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.team.test_http import _session

    monkeypatch.delenv("NOKKER_PG_DSN", raising=False)
    configure_ambient(NokkerConfig(ambient={"glance": {"enabled": True}}),
                      SimpleNamespace(invariants=SimpleNamespace(scrub_output=lambda s: s)))
    voice_web.configure(_speech(timed.url))
    plan, agents = FakePlan(), FakeAgents()
    monkeypatch.setattr(srv.plan_http, "service", lambda: plan)
    monkeypatch.setattr(srv, "_BRIEF_SPEECH", gl.BriefSpeech())
    tenancy = MemoryTenancy()
    ana = tenancy.create_account("ana@example.org")
    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    kernel.tools.connectors = SimpleNamespace(sources_for=lambda tid: [])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {}, approvals=approvals, tenancy=tenancy,
                                                               open_api=False, internal_tenant=False,
                                                               agents=agents))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield SimpleNamespace(port=httpd.server_address[1], ana=ana, sa=_session(approvals, ana.account_id),
                          agents=agents, timed=timed)
    httpd.shutdown()
    voice_web.configure(None)


def _pair(w, surface=None):
    from tests.unit.ambient.test_glance import _call

    body = {"name": "Cuisine"} | ({"surface": surface} if surface else {})
    offer = _call(w.port, "/v1/glance/pair", token=w.sa, body=body)[1]
    claimed = _call(w.port, "/v1/glance/claim", body={"code": offer["code"]})[1]
    return claimed["token"], claimed["device"]["id"]


def test_only_a_wall_that_shows_the_brief_can_have_it_read(wall):
    from tests.unit.ambient.test_glance import _call

    w = wall
    glasses, _ = _pair(w)
    st, out = _call(w.port, gl.BRIEF_SPEECH_PATH, token=glasses)
    assert st == 403 and out["error"]["code"] == "not_wall"
    token, did = _pair(w, "wall")
    st, out = _call(w.port, gl.BRIEF_SPEECH_PATH, token=token)
    assert st == 403 and out["error"]["code"] == "brief_hidden"  # show_brief faux à l'appairage d'un mur
    assert w.timed.requests == []  # aucun texte parti au fournisseur
    assert _call(w.port, gl.BRIEF_SPEECH_PATH, token="nkg_" + "x" * 40)[0] == 401
    st, out = _call(w.port, "/v1/persona", token=token)
    assert st == 403 and out["error"]["code"] == "glance_scope"  # le jeton nkg_ ne lit jamais « Ton Nokker »


def test_a_wall_reads_the_brief_once_per_brief_with_the_owner_voice_and_a_rate(wall):
    from tests.unit.ambient.test_glance import _call

    w = wall
    token, did = _pair(w, "wall")
    _call(w.port, f"/v1/glance/devices/{did}", token=w.sa, body={"show_brief": True}, method="PATCH")
    _call(w.port, "/v1/persona", token=w.sa, body={"voice_id": "leo",
                                                   "avatar": {"id": "nina", "version": 1}}, method="PUT")
    view = _call(w.port, "/v1/glance", token=token)[1]
    assert view["avatar"] == {"id": "nina", "version": 1} and view["lang"]
    st, out = _call(w.port, gl.BRIEF_SPEECH_PATH, token=token)
    assert st == 200 and out["words"][0] == "Bonjour." and len(w.timed.requests) == 1
    assert w.timed.requests[0]["path"].startswith("/v1/text-to-speech/LeoVoice456/with-timestamps")
    assert w.timed.requests[0]["body"]["text"] == w.agents.summary
    for _ in range(gl.BRIEF_SPEECH_PER_HOUR - 1):
        assert _call(w.port, gl.BRIEF_SPEECH_PATH, token=token)[0] == 200
    assert len(w.timed.requests) == 1  # une seule synthèse par brief : le reste vient du cache
    st, out = _call(w.port, gl.BRIEF_SPEECH_PATH, token=token)
    assert st == 429 and out["error"]["code"] == "glance_speech_rate"


def test_no_brief_is_404(wall):
    from tests.unit.ambient.test_glance import _call

    w = wall
    token, did = _pair(w, "wall")
    _call(w.port, f"/v1/glance/devices/{did}", token=w.sa, body={"show_brief": True}, method="PATCH")
    w.agents.summary = "  "
    st, out = _call(w.port, gl.BRIEF_SPEECH_PATH, token=token)
    assert st == 404 and out["error"]["code"] == "no_brief"


def test_glasses_view_has_no_avatar_field(wall):
    from tests.unit.ambient.test_glance import _call

    w = wall
    token, _ = _pair(w)
    assert "avatar" not in _call(w.port, "/v1/glance", token=token)[1]
