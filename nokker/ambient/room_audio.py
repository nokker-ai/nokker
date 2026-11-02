"""Réponse dite dans la pièce (docs/AMBIENT.md §2) : `RoomAudio.say(room, texte | audio)`.

Le noyau décide QUOI dire et OÙ ; la passerelle du foyer (`home-bridge`, opération `audio.play`) joue l'audio sur
l'enceinte de la pièce (Music Assistant via Home Assistant, Snapcast, Sonos, AirPlay). Le moteur de voix est celui
du noyau (`nokker/voice/tts.py`, `TTSProvider`) : ce module ne choisit aucun fournisseur.

Confidentialité (déterministe, sans modèle), par niveau du contenu :
  - `private`  : JAMAIS lu à voix haute. Toujours remplacé par l'annonce neutre.
  - `personal` (défaut) : lu seulement si la présence ne voit QUE le destinataire dans la pièce, hors mode invités ;
    sinon annonce neutre. Sans destinataire, sans présence lisible : annonce neutre.
  - `common`   : contenu du foyer (« le dîner est prêt ») : lu, invités présents ou non.
Mode « invités présents » : interrupteur du foyer (Home Assistant, via la présence) ou réglage du noyau
(`set_guests`) ; tout contenu non `common` devient l'annonce neutre. Un audio déjà synthétisé (octets) est opaque :
il n'est joué tel quel que pour `common`, ou `personal` quand le destinataire est seul.
Heures calmes (`quiet_hours`, « 22:00-07:00 ») : rien n'est dit.

Texte lu : caviardage du noyau (`scrub`) PUIS `clean_for_speech` (ni markdown, ni code, ni poignée de secret), plafond
`max_chars` (au-delà : annonce neutre, jamais une lecture coupée). Le journal ne garde jamais le contenu.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.ambient.presence import UNKNOWN, Presence, valid_room
from nokker.voice.tts import TTSError, clean_for_speech

logger = logging.getLogger("nokker.ambient.room_audio")

PRIVACY = ("common", "personal", "private")
AUTO = "auto"
DEFAULT_MAX_CHARS = 400
NEUTRAL = {
    "fr": "Un message vous attend sur votre téléphone.",
    "en": "A message is waiting for you on your phone.",
}
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class SayResult:
    status: str  # spoken | neutral | skipped | failed
    room: str | None
    reason: str  # ok, private, guests, not_alone, no_presence, too_long, quiet, no_room, off, tts, bridge…
    spoken: str | None = None  # texte effectivement dit (None pour un audio fourni) — jamais journalisé

    @property
    def played(self) -> bool:
        return self.status in ("spoken", "neutral")


def parse_quiet(spec: str | None) -> tuple[int, int] | None:
    m = _HHMM.match((spec or "").strip())
    if not m:
        return None
    return int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])


def in_quiet(now: dt.datetime, window: tuple[int, int] | None) -> bool:
    if window is None:
        return False
    start, end = window
    t = now.hour * 60 + now.minute
    return start <= t < end if start <= end else (t >= start or t < end)


@dataclass
class RoomAudio:
    bridge: Any  # HomeBridge | FakeBridge : presence(), play(room, audio, mime)
    tts: Any  # TTSProvider (synthesize(text, lang) -> bytes) ou None : texte impossible, audio fourni seulement
    scrub: Callable[[str], str]
    rooms: frozenset[str] = frozenset()  # pièces connues (config) ; vide = celles que la présence rapporte
    enabled: bool = False
    max_chars: int = DEFAULT_MAX_CHARS
    quiet: tuple[int, int] | None = None
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now().astimezone()
    clock: Callable[[], float] = time.time
    _guests_until: float = field(default=0.0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── mode invités (réglage du noyau, en plus de l'interrupteur du foyer) ──
    def set_guests(self, on: bool, hours: float = 6.0) -> float:
        with self._lock:
            self._guests_until = self.clock() + max(0.25, min(48.0, float(hours))) * 3600 if on else 0.0
            return self._guests_until

    def guests_forced(self) -> bool:
        with self._lock:
            return self._guests_until > self.clock()

    # ── le point d'entrée ──
    def say(self, room: str | None, content: str | bytes, *, recipient: str | None = None,
            privacy: str = "personal", lang: str = "fr", mime: str = "audio/ogg") -> SayResult:
        """Dit `content` (texte, ou audio OGG/Opus déjà synthétisé) dans `room` (`auto` / None : la pièce où la
        présence voit `recipient`). Ne lève jamais : le résultat dit ce qui a été fait et pourquoi."""
        if not self.enabled or self.bridge is None:
            return SayResult("skipped", None, "off")
        if privacy not in PRIVACY:
            privacy = "private"  # niveau inconnu : le plus strict
        lang = lang if lang in NEUTRAL else "fr"
        if in_quiet(self.now(), self.quiet):
            return self._log(SayResult("skipped", None, "quiet"))
        presence = self._presence()
        target = room if room and room != AUTO else (presence.room_of(recipient) if recipient else None)
        if not target or not valid_room(target) or (self.rooms and target not in self.rooms) \
                or (not self.rooms and target not in presence.rooms):
            return self._log(SayResult("skipped", None, "no_room"))
        verdict = self._verdict(presence, target, recipient, privacy)
        if verdict != "ok":
            return self._speak_neutral(target, lang, verdict)
        if isinstance(content, (bytes, bytearray)):
            return self._play(target, bytes(content), mime, SayResult("spoken", target, "ok"))
        spoken = clean_for_speech(self.scrub(str(content or "")), lang)
        if not spoken:
            return self._log(SayResult("skipped", target, "empty"))
        if len(spoken) > self.max_chars:
            return self._speak_neutral(target, lang, "too_long")
        return self._speak(target, spoken, lang, SayResult("spoken", target, "ok", spoken))

    # ── décisions ──
    def _presence(self) -> Presence:
        try:
            p = self.bridge.presence()
        except Exception:  # noqa: BLE001 — présence illisible : prudence
            p = UNKNOWN
        return p if isinstance(p, Presence) else UNKNOWN

    def _verdict(self, presence: Presence, room: str, recipient: str | None, privacy: str) -> str:
        if privacy == "private":
            return "private"
        if privacy == "common":
            return "ok"
        if presence.guests or self.guests_forced():
            return "guests"
        if not presence.ok:
            return "no_presence"
        if not recipient or not presence.alone_with(room, recipient):
            return "not_alone"
        return "ok"

    def _speak_neutral(self, room: str, lang: str, reason: str) -> SayResult:
        text = NEUTRAL[lang]
        return self._speak(room, text, lang, SayResult("neutral", room, reason, text))

    def _speak(self, room: str, text: str, lang: str, ok: SayResult) -> SayResult:
        if self.tts is None:
            return self._log(SayResult("failed", room, "tts"))
        try:
            audio = self.tts.synthesize(text, lang)
        except TTSError:
            return self._log(SayResult("failed", room, "tts"))
        except Exception:
            logger.exception("voix de la pièce : synthèse en échec")
            return self._log(SayResult("failed", room, "tts"))
        return self._play(room, audio, "audio/ogg", ok)

    def _play(self, room: str, audio: bytes, mime: str, ok: SayResult) -> SayResult:
        try:
            out = self.bridge.play(room, audio, mime)
        except Exception as e:  # noqa: BLE001 — BridgeError : message sans jeton
            logger.warning("voix de la pièce : %s", type(e).__name__)
            return self._log(SayResult("failed", room, "bridge"))
        if isinstance(out, dict) and out.get("played") is False:
            return self._log(SayResult("failed", room, str(out.get("reason") or "bridge")))
        return self._log(ok)

    @staticmethod
    def _log(r: SayResult) -> SayResult:
        logger.info("voix de la pièce : %s (%s) pièce=%s", r.status, r.reason, r.room)  # jamais le contenu
        return r


def from_config(raw: dict[str, Any] | None, *, bridge: Any, tts: Any, scrub: Callable[[str], str]) -> RoomAudio:
    """`ambient.room_audio` de la config. Éteint par défaut ; allumé sans passerelle ni voix : reste inerte."""
    raw = raw or {}
    rooms = frozenset(str(r) for r in (raw.get("rooms") or {}) if valid_room(str(r)))
    return RoomAudio(bridge=bridge, tts=tts, scrub=scrub, rooms=rooms,
                     enabled=bool(raw.get("enabled", False)) and bridge is not None,
                     max_chars=int(raw.get("max_chars", DEFAULT_MAX_CHARS)),
                     quiet=parse_quiet(raw.get("quiet_hours")))


__all__ = ["AUTO", "NEUTRAL", "PRIVACY", "RoomAudio", "SayResult", "from_config", "in_quiet", "parse_quiet"]
