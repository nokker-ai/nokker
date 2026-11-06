"""« Écouter » dans le web (docs/VOICE.md §7, `nokker/voice/web.py`) : flux MP3 relayé depuis un FAUX ElevenLabs local
(`/v1/text-to-speech/{voice}/stream`), sur le vrai `make_handler`. Contrôle d'accès du fil, le noyau lit la réponse
(le client n'envoie jamais de texte), erreurs JSON avant le premier octet, compteur de caractères, journal sans texte,
temps au premier octet audio. Aucun appel réel à ElevenLabs."""
from __future__ import annotations

import http.client
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from nokker.approvals.memory import InMemoryApprovals
from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.core.turn import InputCheck, Turn
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.limits import MemoryUsage
from nokker.tenancy.store import MemoryTenancy
from nokker.threads.memory import InMemoryThreads
from nokker.voice import web as voice_web
from nokker.voice.tts import ElevenLabsTTS, TTSError
from nokker.voice.web import SpeechError

KEY = "sk_" + "test0000000000000000000000000000"
KEY_ENV = "NOKKER_ELEVENLABS_API_KEY"
SECRET = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"
MP3 = [b"\xff\xfb\x90\x00" + bytes(413), b"\xff\xfb\x90\x00" + bytes(413), b"\xff\xfb\x90\x00" + bytes(413)]
REPLY = "**Bonjour** Alice. Ta bouilloire arrive demain, voir https://shop.example.ch/suivi/123 ."


class FakeStream:
    """Faux ElevenLabs : chunked, un morceau MP3 toutes les `gap` secondes après `first` secondes (latence du modèle)."""

    def __init__(self, status=200, first=0.0, gap=0.0, chunks=MP3):
        self.status, self.first, self.gap, self.chunks, self.requests = status, first, gap, chunks, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                       "body": json.loads(self.rfile.read(n) or b"{}")})
                if outer.status != 200:
                    body = b'{"detail":"quota"}'
                    self.send_response(outer.status)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                time.sleep(outer.first)
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for i, c in enumerate(outer.chunks):
                    if i:
                        time.sleep(outer.gap)
                    self.wfile.write(f"{len(c):x}\r\n".encode() + c + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake():
    made = []

    def make(**kw):
        s = FakeStream(**kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)


def _scrub(text):
    return text.replace(SECRET, "[secret:github]")


def _speech(url, **kw):
    cfg = {"enabled": True, "provider": "elevenlabs", "elevenlabs": {"voice_id": "NinaVoice123", "base_url": url}}
    cfg.update(kw)
    return voice_web.from_config(cfg, _scrub, usage=MemoryUsage())


# ───────────────────────── fournisseur et service ─────────────────────────

def test_stream_request_shape_and_chunks(fake):
    s = fake()
    p = ElevenLabsTTS(voice_id="NinaVoice123", base_url=s.url)
    got = b"".join(p.stream("Bonjour.", "fr"))
    assert got == b"".join(MP3)
    r = s.requests[0]
    u = urlsplit(r["path"])
    assert u.path == "/v1/text-to-speech/NinaVoice123/stream"
    assert parse_qs(u.query)["output_format"] == ["mp3_44100_128"]
    assert r["headers"]["xi-api-key"] == KEY and r["body"]["model_id"] == "eleven_flash_v2_5"
    assert r["body"]["language_code"] == "fr" and r["body"]["text"] == "Bonjour."


def test_stream_error_is_raised_at_the_first_next_and_says_nothing_of_the_request(fake):
    s = fake(status=401)
    it = ElevenLabsTTS(voice_id="NinaVoice123", base_url=s.url).stream("texte privé", "fr")
    with pytest.raises(TTSError) as e:
        next(it)
    assert str(e.value) == "ElevenLabs : HTTP 401" and KEY not in str(e.value)


def test_off_or_without_a_streaming_provider_builds_nothing(fake):
    s = fake()
    assert voice_web.from_config({"enabled": False, "provider": "elevenlabs",
                                  "elevenlabs": {"voice_id": "NinaVoice123", "base_url": s.url}}, _scrub) is None
    assert voice_web.from_config({"enabled": True, "provider": "command", "command": {"argv": ["x"]}}, _scrub) is None
    assert voice_web.from_config(None, _scrub) is None


def test_text_is_scrubbed_then_cleaned_and_capped(fake):
    s = fake()
    svc = _speech(s.url, web_max_chars=60)
    assert svc.text_for(f"Le jeton {SECRET} est là.", "fr") == "Le jeton (secret masqué) est là."
    with pytest.raises(SpeechError) as e:
        svc.text_for("x " * 40, "fr")
    assert (e.value.status, e.value.code) == (413, "too_long")
    with pytest.raises(SpeechError) as e:
        svc.text_for("   ", "fr")
    assert (e.value.status, e.value.code) == (409, "not_a_reply")


def test_provider_failure_is_502_before_any_byte(fake):
    s = fake(status=500)
    with pytest.raises(SpeechError) as e:
        _speech(s.url).open(account="us_a", tenant="tn_a", reply="Bonjour.", lang="fr")
    assert (e.value.status, e.value.code) == (502, "provider_failed")


def test_rate_limit_with_retry_after(fake):
    s = fake()
    svc = _speech(s.url)
    svc.rate_max = 2
    for _ in range(2):
        list(svc.open(account="us_a", tenant="tn_a", reply="Bonjour.", lang="fr"))
    with pytest.raises(SpeechError) as e:
        svc.open(account="us_a", tenant="tn_a", reply="Bonjour.", lang="fr")
    assert (e.value.status, e.value.code) == (429, "rate_limited") and 1 <= e.value.retry_after <= 61
    list(svc.open(account="us_b", tenant="tn_b", reply="Bonjour.", lang="fr"))  # un autre compte passe


def test_characters_are_counted_and_the_log_never_has_the_text(fake, caplog):
    s = fake()
    svc = _speech(s.url)
    caplog.set_level(logging.INFO, logger="nokker.voice.web")
    list(svc.open(account="us_a", tenant="tn_a", reply="Bonjour Alice, c'est prêt.", lang="fr"))
    from datetime import datetime
    from zoneinfo import ZoneInfo

    day = datetime.now(ZoneInfo("Europe/Zurich")).date()
    assert svc.usage.read("tn_a", "us_a", day, "speech_chars") == len("Bonjour Alice, c'est prêt.")
    assert "26 caractères" in caplog.text and "Alice" not in caplog.text and KEY not in caplog.text


# ───────────────────────── routes, vrai make_handler ─────────────────────────

@pytest.fixture
def world(fake):
    from nokker.api.server import make_handler
    from tests.unit.api.test_server import make_kernel
    from tests.unit.team.test_http import _session

    s = fake(first=0.30, gap=0.05)
    voice_web.configure(_speech(s.url))
    tenancy = MemoryTenancy()
    accs = {k: tenancy.create_account(f"{k}@example.org") for k in ("alice", "bob")}
    approvals = InMemoryApprovals()
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=lambda *a: None,
                    known_users=frozenset(a.account_id for a in accs.values()), spawn=lambda fn: fn())
    kernel = make_kernel()
    handler = make_handler(kernel, {}, approvals=approvals, hub=hub, tenancy=tenancy, open_api=False,
                           internal_tenant=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tokens = {k: _session(approvals, a.account_id) for k, a in accs.items()}
    port = httpd.server_address[1]
    alice = accs["alice"]
    with tenant_scope(alice.tenant_id):
        t = hub.create_thread("Voix", alice.account_id)
        kernel.store.append(Turn(thread_id=t.id, index=0, input=InputCheck(text="Où en est ma bouilloire ?"),
                                 sender=alice.account_id, reply=REPLY))
        kernel.store.append(Turn(thread_id=t.id, index=1, input=InputCheck(text="Merci"), sender=alice.account_id,
                                 reply=""))
    client_id = t.id.split(":", 1)[1] if ":" in t.id else t.id

    def call(method, path, body=None, who="alice"):
        h = {"Content-Type": "application/json"}
        if who:
            h["Authorization"] = f"Bearer {tokens[who]}"
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    yield call, client_id, s, port, tokens, accs, tenancy, approvals
    httpd.shutdown()
    voice_web.configure(None)


def test_capability(world):
    call, *_ = world
    st, _, body = call("GET", "/v1/speech")
    assert st == 200 and json.loads(body) == {"available": True, "max_chars": 4000, "format": "audio/mpeg"}
    assert call("GET", "/v1/speech", who=None)[0] == 401
    voice_web.configure(None)
    st, _, body = call("GET", "/v1/speech")
    assert st == 404 and json.loads(body)["error"]["code"] == "tts_off"


def test_the_kernel_reads_the_reply_and_streams_mp3(world):
    call, tid, fake_srv, *_ = world
    st, headers, body = call("POST", f"/v1/threads/{tid}/messages/0/speech", {"lang": "fr",
                                                                              "text": "ignore-moi"})
    assert st == 200 and body == b"".join(MP3)
    assert headers["Content-Type"] == "audio/mpeg" and headers["Cache-Control"] == "no-store"
    assert headers.get("Transfer-Encoding") == "chunked"
    sent = fake_srv.requests[-1]["body"]["text"]
    assert sent == "Bonjour Alice. Ta bouilloire arrive demain, voir shop.example.ch ."  # nettoyé, jamais le corps
    assert "ignore-moi" not in json.dumps(fake_srv.requests)


@pytest.mark.parametrize("path,who,status,code", [
    ("/v1/threads/{tid}/messages/7/speech", "alice", 404, "not_found"),
    ("/v1/threads/{tid}/messages/1/speech", "alice", 409, "not_a_reply"),
])
def test_json_errors_before_any_audio(world, path, who, status, code):
    call, tid, *_ = world
    st, headers, body = call("POST", path.format(tid=tid), {}, who=who)
    assert st == status and json.loads(body)["error"]["code"] == code and headers["Cache-Control"] == "no-store"


def test_another_account_never_hears_the_thread(world):
    call, tid, fake_srv, *_ = world
    n = len(fake_srv.requests)
    st, _, _ = call("POST", f"/v1/threads/{tid}/messages/0/speech", {}, who="bob")
    assert st in (403, 404) and len(fake_srv.requests) == n


def test_tts_off_is_503(world):
    call, tid, *_ = world
    voice_web.configure(None)
    st, _, body = call("POST", f"/v1/threads/{tid}/messages/0/speech", {})
    assert st == 503 and json.loads(body)["error"]["code"] == "tts_off"


def test_time_to_first_audio_byte_follows_the_provider_not_the_whole_audio(world):
    """Faux ElevenLabs : 300 ms avant le premier morceau, puis 50 ms entre morceaux. Le premier octet audio arrive
    au client avec le premier morceau (relais au fil des octets), pas après l'audio entier."""
    _, tid, _, port, tokens, *_ = world
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    t0 = time.monotonic()
    conn.request("POST", f"/v1/threads/{tid}/messages/0/speech", body=b"{}",
                 headers={"Authorization": f"Bearer {tokens['alice']}", "Content-Type": "application/json"})
    resp = conn.getresponse()
    first = resp.read1(1)
    ttfb = time.monotonic() - t0
    rest = resp.read()
    total = time.monotonic() - t0
    conn.close()
    assert resp.status == 200 and first == MP3[0][:1] and len(first + rest) == sum(map(len, MP3))
    assert 0.30 <= ttfb < 0.30 + 0.09, ttfb  # premier morceau relayé tout de suite
    assert total >= 0.30 + 2 * 0.05



@pytest.mark.parametrize("accepted,blocked", [("old", True), ("current", False), (None, False)])
def test_no_text_reaches_the_provider_until_the_current_terms_are_accepted(world, accepted, blocked):
    """§5 (lecture à voix haute, sous-traitant) : conditions à réaccepter → 403 `terms_outdated` sur la lecture et
    `available: false` sur la capacité, sans AUCUN appel au fournisseur ; à jour, ou sans aucune acceptation (compte
    créé hors invitation) : comme avant."""
    from nokker.onboarding import http as onboarding_http
    from nokker.onboarding import terms
    from nokker.onboarding.acceptances import MemoryAcceptances
    from nokker.onboarding.front import MemoryFront
    from nokker.onboarding.service import Onboarding

    call, tid, fake_srv, _, _, accs, tenancy, approvals = world
    acceptances = MemoryAcceptances()
    onboarding_http.configure(Onboarding(MemoryFront({}), tenancy, approvals, acceptances))
    try:
        if accepted is not None:
            version = "0" * 64 if accepted == "old" else terms.load("fr").sha256
            acceptances.record(accs["alice"].account_id, "inv-1", version, "fr", "198.51.100.1")
        n = len(fake_srv.requests)
        st, _, body = call("GET", "/v1/speech")
        cap = json.loads(body)
        st2, _, body2 = call("POST", f"/v1/threads/{tid}/messages/0/speech", {"lang": "fr"})
        if blocked:
            assert st == 200 and cap["available"] is False and cap["reason"] == "terms_outdated"
            assert st2 == 403 and json.loads(body2)["error"]["code"] == "terms_outdated"
            assert len(fake_srv.requests) == n  # rien n'est parti chez le fournisseur
        else:
            assert st == 200 and cap["available"] is True
            assert st2 == 200 and body2 == b"".join(MP3) and len(fake_srv.requests) == n + 1
    finally:
        onboarding_http.configure(None)


def test_capability_reads_the_acceptances_under_the_caller_tenant(world):
    """Rév. 33 : `GET /v1/speech` lisait les acceptations (table sous RLS) sans `tenant_scope` → NoTenant, 502 à chaque
    écran de l'app. Un magasin qui exige un locataire fixé, comme `store.conn.connect` en prod, doit répondre 200."""
    from nokker.onboarding import http as onboarding_http
    from nokker.onboarding import terms
    from nokker.onboarding.acceptances import MemoryAcceptances
    from nokker.onboarding.front import MemoryFront
    from nokker.onboarding.service import Onboarding
    from nokker.tenancy.context import current_tenant

    class TenantStrictAcceptances(MemoryAcceptances):
        def for_account(self, account_id):
            current_tenant()  # NoTenant hors tenant_scope, comme la connexion Postgres
            return super().for_account(account_id)

    call, tid, fake_srv, _, _, accs, tenancy, approvals = world
    acceptances = TenantStrictAcceptances()
    onboarding_http.configure(Onboarding(MemoryFront({}), tenancy, approvals, acceptances))
    try:
        acceptances.record(accs["alice"].account_id, "inv-1", terms.load("fr").sha256, "fr", "198.51.100.1")
        st, _, body = call("GET", "/v1/speech")
        assert st == 200 and json.loads(body)["available"] is True
    finally:
        onboarding_http.configure(None)
