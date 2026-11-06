"""Fournisseur de voix `elevenlabs` (docs/VOICE.md §3.3, `nokker/voice/tts.py::ElevenLabsTTS`) contre un FAUX serveur
HTTP local : forme de la requête, clé en en-tête et jamais ailleurs, langue, réencodage, délais, repli texte seul.
Aucun appel réel à ElevenLabs, aucun ffmpeg lancé."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from nokker.qa.secrets import redact
from nokker.voice import tts
from nokker.voice.tts import ElevenLabsTTS, TTSError, ffmpeg_to_ogg_opus

OGG = b"OggS\x00\x02fake-opus"
KEY = "sk_" + "test0000000000000000000000000000"  # fausse clé construite à l'exécution
KEY_ENV = "NOKKER_ELEVENLABS_API_KEY"
SECRET = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"


class FakeEleven:
    """Serveur HTTP sur 127.0.0.1 : note chaque requête, répond `body` avec `status` après `delay` secondes."""

    def __init__(self, body=OGG, status=200, delay=0.0):
        self.body, self.status, self.delay, self.requests = body, status, delay, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                       "body": json.loads(self.rfile.read(n) or b"{}")})
                if outer.delay:
                    time.sleep(outer.delay)
                self.send_response(outer.status)
                self.send_header("Content-Type", "audio/ogg" if outer.status == 200 else "application/json")
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    made = []

    def make(**kw):
        s = FakeEleven(**kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    return KEY


def _cfg(url, **extra):
    c = {"voice_id": "NinaVoice123", "base_url": url}
    c.update(extra)
    return {"enabled": True, "provider": "elevenlabs", "elevenlabs": c}


def test_request_shape_key_in_header_only_language_and_ogg_passthrough(server, key):
    s = server()
    p = tts.provider_from_config(_cfg(s.url))
    assert isinstance(p, ElevenLabsTTS) and p.model_id == "eleven_flash_v2_5" and p.timeout_s == 10.0
    assert p.synthesize("Bonjour Léa.", "fr") == OGG
    (r,) = s.requests
    u = urlsplit(r["path"])
    assert u.path == "/v1/text-to-speech/NinaVoice123"
    assert parse_qs(u.query) == {"output_format": ["opus_48000_64"]}  # pas d'enable_logging par défaut
    assert r["headers"]["xi-api-key"] == KEY and r["headers"]["content-type"] == "application/json"
    assert r["body"] == {"text": "Bonjour Léa.", "model_id": "eleven_flash_v2_5", "language_code": "fr"}
    assert KEY not in r["path"] and KEY not in json.dumps(r["body"])
    assert "authorization" not in r["headers"]


def test_voice_per_language_settings_and_zero_retention(server, key):
    s = server()
    p = tts.provider_from_config(_cfg(s.url, voices={"de": "NinaDe456"}, zero_retention=True,
                                      voice_settings={"stability": 0.5, "speed": 1.0}, model_id="eleven_turbo_v2_5",
                                      output_format="opus_48000_32"))
    p.synthesize("Guten Tag.", "de")
    p.synthesize("Hello.", "en")
    de, en = s.requests
    assert urlsplit(de["path"]).path.endswith("/NinaDe456") and urlsplit(en["path"]).path.endswith("/NinaVoice123")
    assert parse_qs(urlsplit(de["path"]).query) == {"output_format": ["opus_48000_32"], "enable_logging": ["false"]}
    assert de["body"] == {"text": "Guten Tag.", "model_id": "eleven_turbo_v2_5", "language_code": "de",
                          "voice_settings": {"stability": 0.5, "speed": 1.0}}
    assert en["body"]["language_code"] == "en"


def test_no_language_code_for_multilingual_v2_or_when_turned_off(server, key):
    s = server()
    tts.provider_from_config(_cfg(s.url, model_id="eleven_multilingual_v2")).synthesize("Salut.", "fr")
    tts.provider_from_config(_cfg(s.url, language_code=False)).synthesize("Salut.", "fr")
    tts.provider_from_config(_cfg(s.url)).synthesize("Salut.", "fr-CH")  # pas un code ISO 639-1 nu : rien envoyé
    assert all("language_code" not in r["body"] for r in s.requests)


def test_config_refuses_missing_key_missing_or_bad_voice_and_plain_http(server, monkeypatch, caplog):
    s = server()
    monkeypatch.delenv(KEY_ENV, raising=False)
    with caplog.at_level(logging.WARNING, logger="nokker.voice"):
        assert tts.provider_from_config(_cfg(s.url)) is None  # clé absente : voix coupée, nom de variable journalisé
    assert KEY_ENV in caplog.text
    monkeypatch.setenv(KEY_ENV, KEY)
    assert tts.provider_from_config({"provider": "elevenlabs", "elevenlabs": {"base_url": s.url}}) is None
    assert tts.provider_from_config(_cfg(s.url, voice_id="../../v1/user")) is None
    assert tts.provider_from_config(_cfg("http://tts.example.com")) is None
    assert tts.provider_from_config(_cfg(s.url, api_key_env="OTHER_UNSET_ENV")) is None
    assert s.requests == []
    assert KEY not in caplog.text


def test_enabled_false_builds_nothing_and_flag_stays_off(key):
    raw = {"enabled": False, "provider": "elevenlabs", "elevenlabs": {"voice_id": "NinaVoice123"}}
    v = tts.from_config(raw, scrub=lambda t: t)
    assert v.enabled is False and v.provider is None and v.render("Bonjour", "fr") is None


def test_key_removed_after_start_falls_back_to_text(server, key, monkeypatch):
    s = server()
    p = tts.provider_from_config(_cfg(s.url))
    monkeypatch.delenv(KEY_ENV)
    with pytest.raises(TTSError, match=KEY_ENV):
        p.synthesize("Bonjour", "fr")
    assert s.requests == []


@pytest.mark.parametrize("status", [401, 422, 429, 500])
def test_http_error_is_text_only_and_never_logs_text_or_key(server, key, caplog, status):
    s = server(body=json.dumps({"detail": {"message": "quota"}}).encode(), status=status)
    voice = tts.from_config(_cfg(s.url), scrub=lambda t: redact(t).text)
    with caplog.at_level(logging.DEBUG):
        assert voice.render("Ton code est 4321, à demain.", "fr") is None
    assert f"HTTP {status}" in caplog.text
    assert "4321" not in caplog.text and KEY not in caplog.text and "demain" not in caplog.text


def test_short_timeout_falls_back_quickly(server, key, caplog):
    s = server(delay=1.5)
    voice = tts.from_config(_cfg(s.url, timeout_s=0.3), scrub=lambda t: t)
    t0 = time.monotonic()
    with caplog.at_level(logging.WARNING):
        assert voice.render("Bonjour", "fr") is None
    assert time.monotonic() - t0 < 1.4
    assert "Bonjour" not in caplog.text


def test_scrub_and_cleaning_happen_before_the_provider(server, key):
    s = server()
    voice = tts.from_config(_cfg(s.url), scrub=lambda t: redact(t).text)
    assert voice.render(f"**Voici** la clé `{SECRET}` et https://exemple.ch/a/b", "fr") == OGG
    sent = s.requests[0]["body"]["text"]
    assert SECRET not in sent and "**" not in sent and "secret masqué" in sent and "exemple.ch" in sent


def test_too_long_reply_never_reaches_the_server(server, key):
    s = server()
    voice = tts.from_config({**_cfg(s.url), "max_chars": 20}, scrub=lambda t: t)
    assert voice.render("x " * 50, "fr") is None and s.requests == []


def test_non_ogg_output_is_transcoded_or_refused(server, key):
    s = server(body=b"ID3\x04fake-mp3")
    seen = []

    def fake_transcode(data):
        seen.append(data)
        return OGG

    p = tts.provider_from_config(_cfg(s.url, output_format="mp3_44100_64"))
    p.transcode = fake_transcode
    assert p.synthesize("Bonjour", "fr") == OGG and seen == [b"ID3\x04fake-mp3"]
    off = tts.provider_from_config(_cfg(s.url, transcode=False))
    assert off.transcode is None
    with pytest.raises(TTSError, match="hors OGG"):
        off.synthesize("Bonjour", "fr")


def test_empty_or_too_big_output_is_refused(server, key, monkeypatch):
    s = server(body=b"")
    with pytest.raises(TTSError):
        tts.provider_from_config(_cfg(s.url)).synthesize("Bonjour", "fr")
    monkeypatch.setattr(tts, "MAX_AUDIO_BYTES", 8)
    big = server(body=OGG + b"x" * 64)
    with pytest.raises(TTSError, match="trop gros"):
        tts.provider_from_config(_cfg(big.url)).synthesize("Bonjour", "fr")


def test_ffmpeg_transcode_argv_env_and_errors():
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, stdout=OGG, stderr=b"")

    assert ffmpeg_to_ogg_opus(b"ID3mp3", run=run) == OGG
    argv, kw = calls[0]
    assert argv[0] == "ffmpeg" and "libopus" in argv and argv[-3:] == ["-f", "ogg", "pipe:1"]
    assert kw["input"] == b"ID3mp3" and set(kw["env"]) == {"PATH", "LANG"}  # aucune clé ne passe à ffmpeg

    def boom(argv, **kw):
        raise FileNotFoundError("ffmpeg")

    with pytest.raises(TTSError, match="FileNotFoundError"):
        ffmpeg_to_ogg_opus(b"x", run=boom)
    with pytest.raises(TTSError, match="code 1"):
        ffmpeg_to_ogg_opus(b"x", run=lambda a, **k: subprocess.CompletedProcess(a, 1, b"", b""))


def test_room_audio_wiring_uses_the_same_provider(key):
    """La voix de la pièce (heurtoir compris) construit son fournisseur par `provider_from_config(tts)`."""
    p = tts.provider_from_config(_cfg("https://api.elevenlabs.io"))
    assert isinstance(p, ElevenLabsTTS) and p.base_url == "https://api.elevenlabs.io"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg absent (présent dans l'image)")
def test_real_ffmpeg_turns_mp3_into_ogg_opus():
    mp3 = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                          "-c:a", "libmp3lame", "-f", "mp3", "pipe:1"], capture_output=True, timeout=30, check=True).stdout
    out = ffmpeg_to_ogg_opus(mp3)
    assert out.startswith(b"OggS") and b"OpusHead" in out[:200]
