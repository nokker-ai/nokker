"""Réponses vocales (TTS) du canal Telegram — docs/VOICE.md.

Le texte de la réponse part TOUJOURS ; la note vocale ne vient qu'en plus, et toute erreur (fournisseur absent, en
panne, sortie qui n'est pas de l'OGG/Opus, texte trop long) retombe en silence sur le texte seul.

Chemin d'un texte vers la voix, dans cet ordre :
  1. `scrub` (le caviardage de sortie du noyau, `invariants.scrub_output`) : un secret devient `[secret:nom]` ;
  2. `clean_for_speech` : plus de markdown, plus de blocs de code (annoncés, jamais lus), plus de poignées de secret
     (remplacées par « (secret masqué) »), URL abrégées en leur domaine ;
  3. longueur plafonnée : au-delà de `max_chars`, AUCUNE voix (texte seul), jamais une lecture tronquée au milieu.

Fournisseurs derrière `TTSProvider` (aucun n'est choisi par défaut, `tts.enabled: false`) :
  - `command` : un programme local lancé par l'opérateur (ex. Piper + un encodeur Opus, installés hors de l'image) ;
    le texte entre par l'entrée standard, l'OGG/Opus sort par la sortie standard ;
  - `openai_compat` : un serveur HTTP compatible `POST /v1/audio/speech` (`response_format: opus`), clé lue dans une
    variable d'environnement nommée par la config, jamais dans la config elle-même ;
  - `elevenlabs` : l'API ElevenLabs (`POST /v1/text-to-speech/{voice_id}`, sortie Opus 48 kHz), clé `xi-api-key` lue
    dans une variable d'environnement nommée par la config ; une sortie qui n'est pas de l'OGG est réencodée par ffmpeg.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

logger = logging.getLogger("nokker.voice")

DEFAULT_MAX_CHARS = 600
MAX_AUDIO_BYTES = 20 * 1024 * 1024  # bien sous la limite d'envoi d'un bot
OGG_MAGIC = b"OggS"


class TTSError(Exception):
    """Synthèse impossible : l'appelant retombe sur le texte seul."""


class TTSProvider(Protocol):
    name: str

    def synthesize(self, text: str, lang: str) -> bytes:
        """OGG/Opus (ce que `sendVoice` affiche comme une note vocale). Lève `TTSError`."""
        ...


# ─────────────────────────── nettoyage du texte ───────────────────────────

_FENCE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]{1,40})`|`[^`\n]*`")
_SECRET = re.compile(r"\[secret:[^\]\s]*\]")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\(([^)\s]*)\)")
_URL = re.compile(r"https?://([^/\s)]+)[^\s)]*")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_QUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.MULTILINE)
_EMPH = re.compile(r"(\*\*|\*|~~)(?=\S)(.+?)(?<=\S)\1|(?<!\w)(__|_)(?=\S)(.+?)(?<=\S)\3(?!\w)")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}.*$", re.MULTILINE)
_HTML = re.compile(r"</?[a-zA-Z][^>]*>")
_SPACES = re.compile(r"[ \t]+")

_WORDS = {
    "fr": {"code": "(bloc de code dans le message écrit)", "secret": "(secret masqué)"},
    "en": {"code": "(code block in the written message)", "secret": "(hidden secret)"},
}


def clean_for_speech(text: str, lang: str = "fr") -> str:
    """Texte lisible à voix haute : sans markdown, sans code, sans secret. Ne sert QUE à la voix (le texte écrit
    garde sa forme). À appeler APRÈS le caviardage du noyau : une poignée `[secret:…]` n'est jamais lue."""
    w = _WORDS.get(lang, _WORDS["fr"])
    out = _FENCE.sub(f" {w['code']} ", text or "")
    out = _SECRET.sub(f" {w['secret']} ", out)
    out = _INLINE_CODE.sub(lambda m: m.group(1) or "", out)  # identifiant court lu, extrait long coupé
    out = _MD_LINK.sub(lambda m: m.group(1) or "", out)
    out = _URL.sub(lambda m: m.group(1), out)
    out = _TABLE_SEP.sub("", out)
    out = out.replace("|", ", ")
    out = _HTML.sub("", out)
    out = _HEADING.sub("", out)
    out = _QUOTE.sub("", out)
    out = _BULLET.sub("", out)
    for _ in range(2):  # emphases imbriquées
        out = _EMPH.sub(lambda m: m.group(2) or m.group(4) or "", out)
    out = out.replace("*", "").replace("#", "").replace("_", " ")  # nom_de_fichier se lit « nom de fichier »
    lines = [_SPACES.sub(" ", ln).strip() for ln in out.splitlines()]
    paragraphs: list[str] = []
    for ln in lines:
        if not ln:
            continue
        if paragraphs and not re.search(r"[.!?:;…]$", paragraphs[-1]):
            paragraphs[-1] += "."
        paragraphs.append(ln)
    return _SPACES.sub(" ", " ".join(paragraphs)).strip()


# ─────────────────────────── fournisseurs ───────────────────────────

@dataclass
class CommandTTS:
    """TTS local : un programme installé par l'opérateur, hors de l'image (Piper est GPL-3.0, docs/VOICE.md).
    `argv` sans shell ; `{lang}` et `{voice}` remplacés dans chaque argument ; le texte entre par stdin."""

    argv: list[str]
    voices: dict[str, str] = field(default_factory=dict)  # langue -> voix (chemin du modèle, nom…)
    timeout_s: float = 30.0
    name: str = "command"
    run: Callable[..., Any] = subprocess.run  # remplaçable en test

    def synthesize(self, text: str, lang: str) -> bytes:
        if not self.argv:
            raise TTSError("tts.command.argv vide")
        voice = self.voices.get(lang) or self.voices.get("fr") or ""
        argv = [a.replace("{lang}", lang).replace("{voice}", voice) for a in self.argv]
        try:
            done = self.run(argv, input=text.encode("utf-8"), capture_output=True, timeout=self.timeout_s,
                            check=False, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"})
        except (OSError, subprocess.TimeoutExpired) as e:
            raise TTSError(f"programme TTS : {type(e).__name__}") from e
        if done.returncode != 0:
            raise TTSError(f"programme TTS : code {done.returncode}")
        return _checked_ogg(done.stdout)


@dataclass
class OpenAICompatTTS:
    """`POST {base_url}/audio/speech` (forme OpenAI) : `{model, input, voice, response_format: "opus"}` → OGG/Opus.
    Aucun fournisseur choisi ici : l'URL, le modèle et la variable de la clé viennent de la config."""

    base_url: str
    model: str
    voices: dict[str, str] = field(default_factory=dict)
    api_key_env: str = ""
    timeout_s: float = 30.0
    name: str = "openai_compat"
    opener: Callable[..., Any] = urllib_request.urlopen  # remplaçable en test

    def synthesize(self, text: str, lang: str) -> bytes:
        _check_base_url(self.base_url, "tts.openai_compat.base_url")
        voice = self.voices.get(lang) or self.voices.get("fr") or "default"
        headers = {"Content-Type": "application/json", "Accept": "audio/ogg"}
        key = os.environ.get(self.api_key_env, "") if self.api_key_env else ""
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = json.dumps({"model": self.model, "input": text, "voice": voice,
                           "response_format": "opus"}).encode("utf-8")
        req = urllib_request.Request(self.base_url.rstrip("/") + "/audio/speech", data=body, method="POST",
                                     headers=headers)
        try:
            with self.opener(req, timeout=self.timeout_s) as resp:
                data = resp.read(MAX_AUDIO_BYTES + 1)
        except Exception as e:
            raise TTSError(f"fournisseur TTS : {type(e).__name__}") from e
        return _checked_ogg(data)


ELEVENLABS_URL = "https://api.elevenlabs.io"
ELEVENLABS_MODEL = "eleven_flash_v2_5"  # multilingue (32 langues), la plus rapide
ELEVENLABS_FORMAT = "opus_48000_64"  # Opus 48 kHz : ce que `sendVoice` attend, sans réencodage s'il arrive en OGG
ELEVENLABS_KEY_ENV = "NOKKER_ELEVENLABS_API_KEY"
# Modèles qui refusent `language_code` (doc de l'API : « not supported for multilingual_v2 models ») : on n'en envoie pas.
_NO_LANGUAGE_CODE = ("eleven_multilingual_v1", "eleven_multilingual_v2", "eleven_monolingual_v1")
_VOICE_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
_LANG = re.compile(r"^[a-z]{2}$")


def _check_base_url(url: str, what: str) -> None:
    if not url.startswith("https://") and not url.startswith("http://127.0.0.1") \
            and not url.startswith("http://localhost"):
        raise TTSError(f"{what} : https exigé (ou boucle locale)")


def ffmpeg_to_ogg_opus(data: bytes, run: Callable[..., Any] = subprocess.run, timeout_s: float = 15.0) -> bytes:
    """Audio quelconque (MP3, Opus nu…) → OGG/Opus mono 48 kHz, par le ffmpeg de l'image. Lève `TTSError`."""
    argv = ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-vn", "-ac", "1", "-ar", "48000", "-c:a", "libopus",
            "-b:a", "48k", "-f", "ogg", "pipe:1"]
    try:
        done = run(argv, input=data, capture_output=True, timeout=timeout_s, check=False,
                   env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TTSError(f"réencodage OGG/Opus : {type(e).__name__}") from e
    if done.returncode != 0:
        raise TTSError(f"réencodage OGG/Opus : code {done.returncode}")
    return done.stdout


@dataclass
class ElevenLabsTTS:
    """`POST {base_url}/v1/text-to-speech/{voice_id}?output_format=…` : `{text, model_id, language_code?,
    voice_settings?}` → audio. Clé en `xi-api-key`, lue à chaque appel dans la variable nommée par `api_key_env`
    (jamais dans la config, jamais dans un message). Le texte n'est jamais journalisé. Une sortie qui ne commence pas
    par `OggS` (format MP3 choisi, ou Opus livré hors conteneur OGG) passe par `transcode` (ffmpeg) ; sans lui, erreur
    (texte seul)."""

    voice_id: str
    voices: dict[str, str] = field(default_factory=dict)  # langue -> voice_id, sinon `voice_id`
    model_id: str = ELEVENLABS_MODEL
    api_key_env: str = ELEVENLABS_KEY_ENV
    base_url: str = ELEVENLABS_URL
    output_format: str = ELEVENLABS_FORMAT
    language_code: bool = True  # envoie la langue (fr, en, de, it…) quand le modèle l'accepte
    zero_retention: bool = False  # `enable_logging=false` : mode « rétention zéro » (offre Entreprise d'ElevenLabs)
    voice_settings: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 10.0
    name: str = "elevenlabs"
    opener: Callable[..., Any] = urllib_request.urlopen  # remplaçable en test
    transcode: Callable[[bytes], bytes] | None = ffmpeg_to_ogg_opus

    def _voice(self, lang: str) -> str:
        voice = self.voices.get(lang) or self.voice_id or self.voices.get("fr") or ""
        if not _VOICE_ID.match(voice):
            raise TTSError("tts.elevenlabs.voice_id absent ou invalide")
        return voice

    def _request(self, text: str, lang: str, *, output_format: str, stream: bool) -> Any:
        _check_base_url(self.base_url, "tts.elevenlabs.base_url")
        key = os.environ.get(self.api_key_env, "") if self.api_key_env else ""
        if not key:
            raise TTSError(f"clé ElevenLabs absente ({self.api_key_env or 'api_key_env vide'})")
        voice = self._voice(lang)
        query = {"output_format": output_format}
        if self.zero_retention:
            query["enable_logging"] = "false"
        payload: dict[str, Any] = {"text": text, "model_id": self.model_id}
        if self.language_code and _LANG.match(lang or "") and self.model_id not in _NO_LANGUAGE_CODE:
            payload["language_code"] = lang
        if self.voice_settings:
            payload["voice_settings"] = dict(self.voice_settings)
        path = f"/v1/text-to-speech/{urllib_parse.quote(voice, safe='')}" + ("/stream" if stream else "")
        url = f"{self.base_url.rstrip('/')}{path}?{urllib_parse.urlencode(query)}"
        return urllib_request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                      headers={"Content-Type": "application/json", "Accept": "audio/*",
                                               "xi-api-key": key})

    def stream(self, text: str, lang: str, output_format: str = "mp3_44100_128",
               chunk_bytes: int = 4096) -> Iterator[bytes]:
        """`POST …/v1/text-to-speech/{voice_id}/stream` : l'audio au fil des octets (MP3 par défaut, lisible par
        MediaSource). La connexion s'ouvre au PREMIER `next()` : une erreur (clé, HTTP, réseau) y lève `TTSError`,
        avant que l'appelant n'ait envoyé quoi que ce soit. Rien de la requête (texte, clé) dans les erreurs."""
        req = self._request(text, lang, output_format=output_format, stream=True)
        try:
            resp = self.opener(req, timeout=self.timeout_s)
        except urllib_error.HTTPError as e:
            raise TTSError(f"ElevenLabs : HTTP {e.code}") from None
        except Exception as e:  # noqa: BLE001
            raise TTSError(f"ElevenLabs : {type(e).__name__}") from None
        total = 0
        read = getattr(resp, "read1", None) or resp.read  # read1 : ce qui est arrivé, sans attendre `chunk_bytes`
        try:
            while True:
                try:
                    chunk = read(chunk_bytes)
                except Exception as e:  # noqa: BLE001 — coupure en cours de flux
                    raise TTSError(f"ElevenLabs : {type(e).__name__}") from None
                if not chunk:
                    return
                total += len(chunk)
                if total > MAX_AUDIO_BYTES:
                    raise TTSError("audio trop gros")
                yield chunk
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    def synthesize(self, text: str, lang: str) -> bytes:
        _check_base_url(self.base_url, "tts.elevenlabs.base_url")
        key = os.environ.get(self.api_key_env, "") if self.api_key_env else ""
        if not key:
            raise TTSError(f"clé ElevenLabs absente ({self.api_key_env or 'api_key_env vide'})")
        voice = self._voice(lang)
        query = {"output_format": self.output_format}
        if self.zero_retention:
            query["enable_logging"] = "false"
        payload: dict[str, Any] = {"text": text, "model_id": self.model_id}
        if self.language_code and _LANG.match(lang or "") and self.model_id not in _NO_LANGUAGE_CODE:
            payload["language_code"] = lang
        if self.voice_settings:
            payload["voice_settings"] = dict(self.voice_settings)
        url = (f"{self.base_url.rstrip('/')}/v1/text-to-speech/{urllib_parse.quote(voice, safe='')}"
               f"?{urllib_parse.urlencode(query)}")
        req = urllib_request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "audio/*",
                                              "xi-api-key": key})
        try:
            with self.opener(req, timeout=self.timeout_s) as resp:
                data = resp.read(MAX_AUDIO_BYTES + 1)
        except urllib_error.HTTPError as e:  # code seul : ni le corps (qui peut citer la requête), ni la clé
            raise TTSError(f"ElevenLabs : HTTP {e.code}") from None
        except Exception as e:  # noqa: BLE001 — repli texte seul, rien de la requête dans le message
            raise TTSError(f"ElevenLabs : {type(e).__name__}") from None
        if data and not data.startswith(OGG_MAGIC) and len(data) <= MAX_AUDIO_BYTES:
            if self.transcode is None:
                raise TTSError("sortie ElevenLabs hors OGG et réencodage coupé")
            data = self.transcode(data)
        return _checked_ogg(data)


def _checked_ogg(data: bytes | None) -> bytes:
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise TTSError("audio vide ou trop gros")
    if not data.startswith(OGG_MAGIC):
        raise TTSError("sortie qui n'est pas de l'OGG (sendVoice attend OGG/Opus)")
    return data


# ─────────────────────────── service ───────────────────────────

REPLY_MODES = ("voice_in", "always", "off")


@dataclass
class VoiceReplies:
    """Décide s'il faut une note vocale, prépare le texte et appelle le fournisseur. `render` ne lève jamais."""

    provider: TTSProvider | None
    scrub: Callable[[str], str]
    enabled: bool = False
    reply_mode: str = "voice_in"  # voice_in : en réponse à une note vocale ; always : à chaque réponse ; off
    max_chars: int = DEFAULT_MAX_CHARS
    # Lexique du foyer + normalisation fr-CH (`speak_lexicon.SpeakLexicon.prepare`, docs/VOICE.md §6), appliqués au
    # texte déjà nettoyé, juste avant le fournisseur. None (défaut, drapeau `speak_lexicon` éteint) : texte inchangé.
    prepare: Callable[..., str] | None = None
    # Réglage « réponses vocales » par chat (`/voix on|off`) : en mémoire du processus (docs/VOICE.md, limites).
    _chat_pref: dict[str, bool] = field(default_factory=dict, repr=False)

    def set_chat_pref(self, chat_key: str, on: bool) -> None:
        self._chat_pref[chat_key] = on

    def wanted(self, chat_key: str, voice_in: bool) -> bool:
        if not self.enabled or self.provider is None:
            return False
        pref = self._chat_pref.get(chat_key)
        if pref is not None:  # `/voix on` : à chaque réponse ; `/voix off` : jamais, même à une note vocale
            return pref
        if self.reply_mode == "always":
            return True
        return voice_in and self.reply_mode == "voice_in"

    def speech_text(self, reply: str, lang: str) -> str | None:
        """Texte à dire, ou None (vide, ou trop long : texte seul)."""
        spoken = clean_for_speech(self.scrub(reply or ""), lang)
        if not spoken or len(spoken) > self.max_chars:
            return None
        return spoken

    def render(self, reply: str, lang: str, scope: Any = None) -> bytes | None:
        if not self.enabled or self.provider is None:
            return None
        spoken = self.speech_text(reply, lang)
        if spoken is None:
            return None
        if self.prepare is not None:  # après le plafond de longueur : la même réponse reçoit ou non une voix
            spoken = self.prepare(spoken, lang, scope)
        try:
            return self.provider.synthesize(spoken, lang)
        except TTSError as e:
            logger.warning("TTS indisponible (%s), texte seul : %s", self.provider.name, e)
        except Exception:
            logger.exception("TTS en échec (%s), texte seul", self.provider.name)
        return None


def provider_from_config(raw: dict[str, Any]) -> TTSProvider | None:
    kind = str(raw.get("provider") or "")
    if kind == "command":
        c = raw.get("command") or {}
        return CommandTTS(argv=[str(a) for a in c.get("argv") or []], voices=dict(c.get("voices") or {}),
                          timeout_s=float(c.get("timeout_s", 30.0)))
    if kind == "openai_compat":
        c = raw.get("openai_compat") or {}
        if not c.get("base_url") or not c.get("model"):
            logger.warning("tts.openai_compat : base_url et model requis — voix coupée")
            return None
        return OpenAICompatTTS(base_url=str(c["base_url"]), model=str(c["model"]),
                               voices=dict(c.get("voices") or {}), api_key_env=str(c.get("api_key_env") or ""),
                               timeout_s=float(c.get("timeout_s", 30.0)))
    if kind == "elevenlabs":
        return _elevenlabs_from_config(raw.get("elevenlabs") or {})
    if kind:
        logger.warning("tts.provider inconnu : %r — voix coupée", kind)
    return None


def _elevenlabs_from_config(c: dict[str, Any]) -> ElevenLabsTTS | None:
    voice_id, voices = str(c.get("voice_id") or ""), {str(k): str(v) for k, v in (c.get("voices") or {}).items() if v}
    bad = [v for v in [voice_id, *voices.values()] if v and not _VOICE_ID.match(v)]
    if (not voice_id and not voices) or bad:
        logger.warning("tts.elevenlabs : voice_id absent ou invalide — voix coupée")
        return None
    key_env = str(c.get("api_key_env") or ELEVENLABS_KEY_ENV)
    if not os.environ.get(key_env):
        logger.warning("tts.elevenlabs : variable %s vide — voix coupée", key_env)  # le nom, jamais la valeur
        return None
    base_url = str(c.get("base_url") or ELEVENLABS_URL)
    try:
        _check_base_url(base_url, "tts.elevenlabs.base_url")
    except TTSError as e:
        logger.warning("%s — voix coupée", e)
        return None
    transcode = ffmpeg_to_ogg_opus if c.get("transcode", True) else None
    return ElevenLabsTTS(voice_id=voice_id, voices=voices, model_id=str(c.get("model_id") or ELEVENLABS_MODEL),
                         api_key_env=key_env, base_url=base_url,
                         output_format=str(c.get("output_format") or ELEVENLABS_FORMAT),
                         language_code=bool(c.get("language_code", True)),
                         zero_retention=bool(c.get("zero_retention", False)),
                         voice_settings=dict(c.get("voice_settings") or {}),
                         timeout_s=float(c.get("timeout_s", 10.0)), transcode=transcode)


def from_config(raw: dict[str, Any] | None, scrub: Callable[[str], str],
                prepare: Callable[..., str] | None = None) -> VoiceReplies:
    """`tts:` de la config. Absent ou `enabled: false` (défaut) : aucune voix, aucun fournisseur construit."""
    raw = raw or {}
    enabled = bool(raw.get("enabled", False))
    provider = provider_from_config(raw) if enabled else None
    mode = str(raw.get("reply_mode") or "voice_in")
    return VoiceReplies(provider=provider, scrub=scrub, enabled=enabled and provider is not None,
                        reply_mode=mode if mode in REPLY_MODES else "voice_in",
                        max_chars=int(raw.get("max_chars", DEFAULT_MAX_CHARS)), prepare=prepare)


__all__ = ["CommandTTS", "ElevenLabsTTS", "OpenAICompatTTS", "TTSError", "TTSProvider", "VoiceReplies",
           "clean_for_speech", "ffmpeg_to_ogg_opus", "from_config", "provider_from_config"]
