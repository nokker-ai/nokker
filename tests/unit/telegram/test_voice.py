"""Réponses vocales Telegram (docs/VOICE.md, `nokker/voice/tts.py`) : nettoyage du texte, secret jamais lu, éteint par
défaut, texte toujours envoyé et repli texte seul en erreur, `/voix on|off`. Aucun appel réseau, aucun modèle de voix :
fournisseurs et bot sont des faux."""
from __future__ import annotations

import io
import json
import subprocess

import pytest

from nokker.core.turn import Turn
from nokker.qa.secrets import redact
from nokker.telegram.adapter import BotApi, TelegramAdapter
from nokker.telegram.pairing import MemoryPairing
from nokker.tenancy import MemoryTenancy
from nokker.voice import tts
from nokker.voice.tts import CommandTTS, OpenAICompatTTS, TTSError, VoiceReplies, clean_for_speech

OGG = b"OggS\x00\x02fake-opus"
SECRET = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"  # faux jeton construit à l'exécution (gitleaks)


def scrub(text: str) -> str:
    return redact(text).text


class FakeProvider:
    name = "fake"

    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def synthesize(self, text, lang):
        self.calls.append((text, lang))
        if self.fail:
            raise TTSError("panne")
        return OGG


class FakeBot:
    def __init__(self, voice_fails=False):
        self.sent, self.voices, self.voice_fails = [], [], voice_fails

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))

    def send_voice(self, chat_id, audio):
        if self.voice_fails:
            raise OSError("réseau")
        self.voices.append((chat_id, audio))

    def typing(self, chat_id):
        pass


def _adapter(voice, reply="Voici **la** réponse.", transcribe=None, bot=None):
    tenancy, pairing, bot = MemoryTenancy(), MemoryPairing(), bot or FakeBot()
    acc = tenancy.create_account("a@x.ch")
    tenancy.bind_identity(acc.account_id, "telegram", "111")
    turns = []

    def run_turn(inp):
        turns.append(inp)
        return Turn(thread_id=inp.thread_id, index=0, input=None, reply=reply)

    ad = TelegramAdapter(tenancy=tenancy, pairing=pairing, run_turn=run_turn, bot=bot, voice=voice,
                         transcribe=transcribe)
    return ad, bot, turns


def _text(text):
    return {"chat": {"id": 111, "type": "private"}, "from": {"id": 111, "language_code": "fr"}, "text": text}


def _voice_msg():
    return {"chat": {"id": 111, "type": "private"}, "from": {"id": 111, "language_code": "fr"},
            "voice": {"file_id": "f", "duration": 2}}


# ─────────────────────────── nettoyage ───────────────────────────

def test_clean_for_speech_strips_markdown_code_links_and_tables():
    text = ("# Titre\n\nVoici **gras** et _italique_, `ls` et `une très longue commande avec plein d'options "
            "--a --b --c`.\n\n```python\nprint('jamais lu')\n```\n\n- un\n- deux\n\n"
            "Voir [la doc](https://docs.example.org/x/y) ou https://example.com/a?b=c\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    out = clean_for_speech(text)
    for bad in ("#", "**", "`", "print", "jamais lu", "https://", "](", "---", "_italique_"):
        assert bad not in out, bad
    assert "bloc de code dans le message écrit" in out
    assert "gras" in out and "italique" in out and "ls" in out and "la doc" in out and "example.com" in out
    assert "un. deux." in out
    assert clean_for_speech("ouvre nom_de_fichier.py") == "ouvre nom de fichier.py"  # pas une emphase


def test_secret_is_never_spoken():
    v = VoiceReplies(provider=FakeProvider(), scrub=scrub, enabled=True)
    spoken = v.speech_text(f"Ton jeton est {SECRET}, garde-le.", "fr")
    assert SECRET not in spoken and "ghp_" not in spoken and "[secret" not in spoken and "secret masqué" in spoken
    v.render(f"Ton jeton est {SECRET}.", "fr")
    assert all(SECRET not in t for t, _ in v.provider.calls)
    assert "[secret:" not in clean_for_speech("a [secret:github-token-x] b")


def test_too_long_is_text_only():
    v = VoiceReplies(provider=FakeProvider(), scrub=scrub, enabled=True, max_chars=50)
    assert v.render("mot " * 40, "fr") is None and v.provider.calls == []


# ─────────────────────────── défaut éteint, config ───────────────────────────

def test_disabled_by_default():
    v = tts.from_config(None, scrub)
    assert not v.enabled and v.provider is None and not v.wanted("1", voice_in=True)
    assert tts.from_config({"provider": "command", "command": {"argv": ["x"]}}, scrub).provider is None  # pas enabled
    on = tts.from_config({"enabled": True, "provider": "openai_compat",
                          "openai_compat": {"base_url": "https://tts.example.eu/v1", "model": "m"}}, scrub)
    assert on.enabled and isinstance(on.provider, OpenAICompatTTS)
    assert not tts.from_config({"enabled": True, "provider": "openai_compat"}, scrub).enabled  # incomplet : coupé
    assert not tts.from_config({"enabled": True, "provider": "inconnu"}, scrub).enabled


def test_default_config_has_no_voice():
    from nokker.config import NokkerConfig

    assert NokkerConfig().tts == {} and not tts.from_config(NokkerConfig().tts, scrub).enabled


# ─────────────────────────── adaptateur ───────────────────────────

def test_no_voice_without_service_or_for_a_typed_message():
    ad, bot, _ = _adapter(None)
    ad._process(_text("salut"))
    assert bot.sent[-1][1] == "Voici **la** réponse." and bot.voices == []
    ad, bot, _ = _adapter(VoiceReplies(provider=FakeProvider(), scrub=scrub, enabled=True))
    ad._process(_text("salut"))  # mode voice_in : un message écrit reçoit du texte seul
    assert bot.voices == []


def test_voice_note_in_gets_text_and_voice():
    prov = FakeProvider()
    ad, bot, turns = _adapter(VoiceReplies(provider=prov, scrub=scrub, enabled=True),
                              transcribe=lambda m: "quelle heure est-il")
    ad._process(_voice_msg())
    assert turns[0].user_text == "quelle heure est-il"
    assert bot.sent[-1][1] == "Voici **la** réponse." and bot.voices == [(111, OGG)]
    assert prov.calls == [("Voici la réponse.", "fr")]


def test_voice_note_without_transcriber_is_unchanged():
    ad, bot, turns = _adapter(VoiceReplies(provider=FakeProvider(), scrub=scrub, enabled=True))
    ad._process(_voice_msg())
    assert turns == [] and "texte" in bot.sent[-1][1] and bot.voices == []


@pytest.mark.parametrize("bot_fails", [False, True])
def test_provider_or_send_error_falls_back_to_text_only(bot_fails):
    prov = FakeProvider(fail=not bot_fails)
    ad, bot, _ = _adapter(VoiceReplies(provider=prov, scrub=scrub, enabled=True, reply_mode="always"),
                          bot=FakeBot(voice_fails=bot_fails))
    ad._process(_text("salut"))
    assert bot.sent[-1][1] == "Voici **la** réponse." and bot.voices == []
    assert "erreur" not in bot.sent[-1][1]


def test_voice_command_toggles_per_chat_without_a_turn():
    v = VoiceReplies(provider=FakeProvider(), scrub=scrub, enabled=True)
    ad, bot, turns = _adapter(v)
    ad._process(_text("/voix on"))
    assert turns == [] and "activées" in bot.sent[-1][1]
    ad._process(_text("bonjour"))
    assert bot.voices == [(111, OGG)]
    ad._process(_text("/voix off"))
    ad._process(_voice_msg())
    assert len(bot.voices) == 1
    ad2, bot2, _ = _adapter(None)
    ad2._process(_text("/voix on"))
    assert "pas disponibles" in bot2.sent[-1][1]


# ─────────────────────────── fournisseurs (faux processus, faux HTTP) ───────────────────────────

def test_command_provider_passes_text_on_stdin_and_checks_ogg():
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, input=kw["input"], env=kw["env"])
        return subprocess.CompletedProcess(argv, 0, stdout=OGG, stderr=b"")

    p = CommandTTS(argv=["synth", "--model", "{voice}", "--lang", "{lang}"], voices={"fr": "/v/fr.onnx"}, run=run)
    assert p.synthesize("bonjour", "fr") == OGG
    assert seen["argv"] == ["synth", "--model", "/v/fr.onnx", "--lang", "fr"] and seen["input"] == b"bonjour"
    assert set(seen["env"]) == {"PATH", "LANG"}  # aucun secret de l'environnement du noyau
    bad = CommandTTS(argv=["x"], run=lambda a, **k: subprocess.CompletedProcess(a, 0, stdout=b"RIFFwav", stderr=b""))
    with pytest.raises(TTSError):
        bad.synthesize("x", "fr")
    with pytest.raises(TTSError):
        CommandTTS(argv=["x"], run=lambda a, **k: subprocess.CompletedProcess(a, 1, b"", b"")).synthesize("x", "fr")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_openai_compat_request_shape_and_key_from_env(monkeypatch):
    monkeypatch.setenv("TTS_TEST_KEY", "k-123")
    seen = {}

    def opener(req, timeout):
        seen.update(url=req.full_url, body=json.loads(req.data), auth=req.get_header("Authorization"))
        return _Resp(OGG)

    p = OpenAICompatTTS(base_url="https://tts.example.eu/v1/", model="m1", voices={"en": "v-en"},
                        api_key_env="TTS_TEST_KEY", opener=opener)
    assert p.synthesize("hello", "en") == OGG
    assert seen["url"] == "https://tts.example.eu/v1/audio/speech" and seen["auth"] == "Bearer k-123"
    assert seen["body"] == {"model": "m1", "input": "hello", "voice": "v-en", "response_format": "opus"}

    def boom(req, timeout):
        raise OSError("k-123 refusée")

    with pytest.raises(TTSError) as e:
        OpenAICompatTTS(base_url="https://x.example/v1", model="m", api_key_env="TTS_TEST_KEY",
                        opener=boom).synthesize("a", "fr")
    assert "k-123" not in str(e.value)
    with pytest.raises(TTSError):  # http en clair hors boucle locale : refusé avant tout envoi
        OpenAICompatTTS(base_url="http://tts.example.eu/v1", model="m", opener=opener).synthesize("a", "fr")


def test_bot_send_voice_is_a_multipart_send_voice(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen.update(url=req.full_url, ctype=req.get_header("Content-type"), data=req.data)
        return _Resp(b"{}")

    monkeypatch.setattr("nokker.telegram.adapter.urllib_request.urlopen", fake_urlopen)
    BotApi("T0K").send_voice(111, OGG)
    assert seen["url"].endswith("/sendVoice") and seen["ctype"].startswith("multipart/form-data; boundary=")
    assert b'name="voice"; filename="reply.ogg"' in seen["data"] and OGG in seen["data"]
