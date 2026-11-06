"""« Écouter » une réponse dans le web (docs/VOICE.md §7) : le noyau lit le texte de la réponse d'un tour (le client
n'envoie JAMAIS de texte), le prépare comme pour Telegram (`scrub_output` puis `clean_for_speech`, lexique du foyer
s'il est allumé) et relaie l'audio MP3 au fil des octets depuis ElevenLabs (`/v1/text-to-speech/{voice_id}/stream`,
`mp3_44100_128`, lisible par MediaSource).

Contrat (nokker-front, 03.11.2026) :
  - `GET /v1/speech` → 200 `{available: true, max_chars, format: "audio/mpeg"}` ; 404 si la voix est éteinte ou sans
    fournisseur qui sait streamer ;
  - `POST /v1/threads/{thread_id}/messages/{index}/speech` `{lang?}` → 200 `audio/mpeg`, `Cache-Control: no-store`,
    chunked ; erreurs JSON `{error: {code}}` AVANT le premier octet seulement : 404 `not_found`, 409 `not_a_reply`,
    413 `too_long`, 429 `rate_limited` (+ `Retry-After`), 502 `provider_failed`, 503 `tts_off`.
Journal : le nombre de caractères, jamais le texte. Compteur par compte et par jour (`tenant_usage.speech_chars`).
Telegram n'est pas touché (note vocale OGG entière, `tts.VoiceReplies`).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from nokker.voice.tts import TTSError, clean_for_speech, provider_from_config

logger = logging.getLogger("nokker.voice.web")

WEB_FORMAT = "mp3_44100_128"
WEB_MIME = "audio/mpeg"
WEB_MAX_CHARS = 4000
RATE_MAX, RATE_WINDOW_S = 12, 60.0  # demandes par compte et par minute
_LANG = re.compile(r"^[a-z]{2}$")
_ZURICH = ZoneInfo("Europe/Zurich")


class SpeechError(Exception):
    def __init__(self, status: int, code: str, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.status, self.code, self.retry_after = status, code, retry_after


@dataclass
class WebSpeech:
    provider: Any  # fournisseur qui a `stream(text, lang, output_format)` (ElevenLabsTTS)
    scrub: Callable[[str], str]
    max_chars: int = WEB_MAX_CHARS
    prepare: Callable[..., str] | None = None  # lexique du foyer (speak_lexicon), None : texte inchangé
    usage: Any = None  # tenancy.limits.MemoryUsage | PgUsage (méthode `add`)
    output_format: str = WEB_FORMAT
    rate_max: int = RATE_MAX
    rate_window_s: float = RATE_WINDOW_S
    clock: Callable[[], float] = time.monotonic
    _hits: dict[str, deque[float]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def capability(self) -> dict[str, Any]:
        return {"available": True, "max_chars": self.max_chars, "format": WEB_MIME}

    def _rate(self, account: str) -> None:
        now = self.clock()
        with self._lock:
            q = self._hits.setdefault(account, deque())
            while q and now - q[0] > self.rate_window_s:
                q.popleft()
            if len(q) >= self.rate_max:
                raise SpeechError(429, "rate_limited", retry_after=max(1, int(self.rate_window_s - (now - q[0])) + 1))
            q.append(now)

    def text_for(self, reply: str, lang: str, scope: Any = None) -> str:
        """Texte à dire : 409 `not_a_reply` (vide une fois nettoyé), 413 `too_long` (plafond APRÈS nettoyage : ce qui est
        réellement lu ; jamais de lecture tronquée)."""
        spoken = clean_for_speech(self.scrub(reply or ""), lang)
        if not spoken:
            raise SpeechError(409, "not_a_reply")
        if len(spoken) > self.max_chars:
            raise SpeechError(413, "too_long")
        if self.prepare is not None:
            spoken = self.prepare(spoken, lang, scope)
        return spoken

    def open(self, *, account: str, tenant: str, reply: str, lang: str | None,
             scope: Any = None) -> Iterator[bytes]:
        """Vérifie, lance la synthèse et rend un itérateur dont le PREMIER morceau est déjà arrivé : toute erreur part en
        `SpeechError` avant le moindre octet envoyé au client."""
        lang = lang if lang and _LANG.match(lang) else "fr"
        spoken = self.text_for(reply, lang, scope)
        self._rate(account)
        chunks = self.provider.stream(spoken, lang, output_format=self.output_format)
        try:
            first = next(chunks)
        except StopIteration:
            raise SpeechError(502, "provider_failed") from None
        except TTSError as e:
            logger.warning("voix web indisponible (%s) : %s", getattr(self.provider, "name", "?"), e)
            raise SpeechError(502, "provider_failed") from None
        n = len(spoken)
        logger.info("voix web : %d caractères (compte %s)", n, account)  # jamais le texte
        if self.usage is not None:
            try:
                self.usage.add(tenant, account, datetime.now(_ZURICH).date(), "speech_chars", n)
            except Exception:  # noqa: BLE001 — le compteur ne bloque jamais la lecture
                logger.warning("compteur de caractères lus non mis à jour", exc_info=True)

        def rest() -> Iterator[bytes]:
            yield first
            try:
                yield from chunks
            except TTSError as e:  # coupure en cours de flux : le client a déjà des octets, on s'arrête là
                logger.warning("voix web interrompue : %s", e)

        return rest()


def from_config(raw: dict[str, Any] | None, scrub: Callable[[str], str], *, prepare: Callable[..., str] | None = None,
                usage: Any = None) -> WebSpeech | None:
    """`tts:` de la config. Éteinte (défaut), sans fournisseur, ou fournisseur qui ne sait pas streamer : None (404)."""
    raw = raw or {}
    if not raw.get("enabled", False):
        return None
    provider = provider_from_config(raw)
    if provider is None or not callable(getattr(provider, "stream", None)):
        return None
    return WebSpeech(provider=provider, scrub=scrub, prepare=prepare, usage=usage,
                     max_chars=int(raw.get("web_max_chars", WEB_MAX_CHARS)),
                     output_format=str(raw.get("web_output_format") or WEB_FORMAT))


__all__ = ["WEB_FORMAT", "WEB_MAX_CHARS", "WEB_MIME", "SpeechError", "WebSpeech", "configure", "from_config", "service"]


_svc: WebSpeech | None = None


def configure(svc: WebSpeech | None) -> None:
    global _svc
    _svc = svc


def service() -> WebSpeech | None:
    return _svc
