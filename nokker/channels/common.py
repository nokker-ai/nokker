"""Socle commun des canaux Slack et WhatsApp (docs/CHANNELS.md) — le même contrat que l'adaptateur Telegram du noyau
(`nokker/telegram/adapter.py`), écrit une fois :

- **Appairage** : un code à usage unique (10 min, SHA-256 seul en base) émis par l'app web pour UN compte, envoyé en
  message privé au bot (`pair CODE`, `appairer CODE` ou `/start CODE`) ; l'identité du canal est alors liée au compte
  (`account_identities`, une identité → un seul compte). Une identité non liée ne déclenche aucun tour.
- **Locataire fixé à la frontière** : chaque tour tourne dans `tenant_scope(compte.tenant_id)`, sur un fil
  `scoped_thread_id(locataire, "<canal>-…")` : deux comptes n'adressent jamais le même fil.
- **Sortie** : tout texte qui part vers le fournisseur passe par le caviardage du noyau (`scrub_output`), même la
  réponse déjà caviardée par le tour (défense en profondeur) et les cartes.
- **Débit** : seau à jetons par identité de canal (messages entrants) ; au-delà, un seul avertissement puis silence.
- **Rejeu** : chaque événement porte un identifiant du fournisseur ; il n'est traité qu'une fois (`EventDedup`), même
  quand le fournisseur le renvoie (réessai Slack, renvoi Meta) ou qu'une autre réplique l'a déjà reçu (Postgres).
- **Cartes d'approbation** : un message avec un bouton-LIEN vers la carte de l'app web. Aucune carte, forte ou
  standard, ne se décide dans un canal : la preuve (passkey, Mini App, clé d'appareil, TOTP) se donne dans l'app.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from nokker.core.turn import Turn, TurnInput
from nokker.telegram.pairing import TTL, MemoryPairing, PairingStore, _sha, new_code
from nokker.tenancy.context import scoped_thread_id, tenant_of_thread, tenant_scope
from nokker.tenancy.store import IdentityTaken, Tenancy

logger = logging.getLogger("nokker.channels")

CHANNELS = ("slack", "whatsapp")

TEXTS = {
    "fr": {
        "unpaired": ("Ce compte {name} n'est relié à aucun compte Nokker. Dans l'app web Nokker, ouvre « {name} » "
                     "pour obtenir un code, puis envoie-moi en message privé : pair CODE"),
        "paired": "C'est fait : ce compte {name} est relié à ton compte Nokker. Tu peux m'écrire ici.",
        "bad_code": "Ce code n'est pas valable (déjà utilisé ou expiré). Demandes-en un nouveau dans l'app web.",
        "taken": "Ce compte {name} est déjà relié à un autre compte Nokker.",
        "pair_private": "L'appairage se fait en message privé, jamais dans un canal.",
        "text_only": "Pour l'instant, je ne lis ici que les messages texte et les notes vocales.",
        "voice_off": "Les notes vocales ne sont pas activées sur ce canal : écris-moi ton message.",
        "voice_empty": "Je n'ai rien compris dans cette note vocale. Réessaie, ou écris-moi.",
        "voice_big": "Cette note vocale est trop longue pour moi. Coupe-la, ou écris-moi.",
        "rate": "Doucement : trop de messages d'un coup. Je reprends dans une minute.",
        "error": "Une erreur m'a empêché de répondre. Réessaie dans un instant.",
        "card": "Action à approuver : {tool}",
        "card_strong": ("Carte FORTE : elle ne se décide que dans l'app web (passkey) ou la Mini App Telegram, "
                        "jamais ici."),
        "card_standard": "Elle se décide dans l'app web, pas ici.",
        "open": "Ouvrir la carte",
        "decide_in_app": "Les cartes ne se décident pas ici : ouvre-la dans l'app web.",
    },
    "en": {
        "unpaired": ("This {name} account isn't linked to a Nokker account. In the Nokker web app, open “{name}” to "
                     "get a code, then send me as a direct message: pair CODE"),
        "paired": "Done: this {name} account is linked to your Nokker account. You can write to me here.",
        "bad_code": "This code isn't valid (already used or expired). Get a new one in the web app.",
        "taken": "This {name} account is already linked to another Nokker account.",
        "pair_private": "Pairing happens in a direct message, never in a channel.",
        "text_only": "For now I only read text messages and voice notes here.",
        "voice_off": "Voice notes aren't enabled on this channel: please type your message.",
        "voice_empty": "I couldn't make out this voice note. Try again, or type it.",
        "voice_big": "This voice note is too long for me. Split it, or type it.",
        "rate": "Easy: too many messages at once. I'll pick up again in a minute.",
        "error": "An error kept me from answering. Please try again in a moment.",
        "card": "Action awaiting your approval: {tool}",
        "card_strong": "STRONG card: it can only be decided in the web app (passkey) or the Telegram Mini App, never here.",
        "card_standard": "It is decided in the web app, not here.",
        "open": "Open the card",
        "decide_in_app": "Cards aren't decided here: open it in the web app.",
    },
}

_PAIR = re.compile(r"^\s*(?:/start|pair|appairer|link)\s+([A-Za-z0-9]{6,12})\s*$", re.IGNORECASE)


def parse_pair_command(text: str) -> str | None:
    """Le code d'une commande d'appairage (`pair CODE`, `appairer CODE`, `/start CODE`), sinon None."""
    m = _PAIR.match(text or "")
    return m.group(1).upper() if m else None


def texts(lang: str | None) -> dict[str, str]:
    return TEXTS["en" if lang == "en" else "fr"]


def mask(identity: str) -> str:
    """Identité de canal pour le journal : jamais un numéro de téléphone entier."""
    s = str(identity)
    return s[:3] + "…" + s[-2:] if len(s) > 6 else "…"


def split_text(text: str, limit: int) -> list[str]:
    text = text or "…"
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


# ─────────────────────────────── appairage ───────────────────────────────


class PgChannelPairing:
    """Codes d'appairage d'UN canal (table `channel_pairing`, migration 048). Même contrat que `PgPairing`."""

    def __init__(self, dsn: str, channel: str) -> None:
        if channel not in CHANNELS:
            raise ValueError(channel)
        self.dsn, self.channel = dsn, channel

    def _conn(self):
        import psycopg

        return psycopg.connect(self.dsn)

    def issue(self, account_id: str) -> tuple[str, datetime]:
        code, exp = new_code(), datetime.now(timezone.utc) + TTL
        with self._conn() as conn:
            conn.execute("INSERT INTO channel_pairing (channel, code_sha256, account_id, expires_at) "
                         "VALUES (%s,%s,%s,%s)", (self.channel, _sha(code), account_id, exp))
        return code, exp

    def redeem(self, code: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("UPDATE channel_pairing SET used_at=now() WHERE channel=%s AND code_sha256=%s "
                               "AND used_at IS NULL AND expires_at > now() RETURNING account_id",
                               (self.channel, _sha(code))).fetchone()
        return row[0] if row else None


# ─────────────────────────────── rejeu ───────────────────────────────


class EventDedup(Protocol):
    def first_time(self, channel: str, event_id: str) -> bool: ...


@dataclass
class MemoryDedup:
    """Identifiants d'événements déjà vus (une réplique). Borné en nombre et en âge."""

    ttl_s: float = 48 * 3600
    max_items: int = 50_000
    clock: Callable[[], float] = time.time
    _seen: OrderedDict[tuple[str, str], float] = field(default_factory=OrderedDict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def first_time(self, channel: str, event_id: str) -> bool:
        if not event_id:
            return True  # rien pour dédoublonner : traité (l'appelant refuse déjà un événement sans identifiant)
        now = self.clock()
        with self._lock:
            while self._seen and (len(self._seen) > self.max_items or next(iter(self._seen.values())) < now - self.ttl_s):
                self._seen.popitem(last=False)
            key = (channel, event_id)
            if key in self._seen:
                return False
            self._seen[key] = now
            return True


class PgDedup:
    """Table `channel_events` (migration 048) : partagée entre répliques. Purge des lignes de plus de 2 jours."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._n = 0

    def first_time(self, channel: str, event_id: str) -> bool:
        if not event_id:
            return True
        import psycopg

        with psycopg.connect(self.dsn) as conn:
            row = conn.execute("INSERT INTO channel_events (channel, event_id) VALUES (%s,%s) "
                               "ON CONFLICT DO NOTHING RETURNING 1", (channel, event_id[:200])).fetchone()
            self._n += 1
            if self._n % 500 == 0:
                conn.execute("DELETE FROM channel_events WHERE received_at < now() - interval '2 days'")
        return row is not None


# ─────────────────────────────── fenêtre de 24 h (WhatsApp) ───────────────────────────────


class WindowStore(Protocol):
    def touch(self, channel: str, identity: str, at: float) -> None: ...

    def last(self, channel: str, identity: str) -> float | None: ...


@dataclass
class MemoryWindows:
    _last: dict[tuple[str, str], float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def touch(self, channel: str, identity: str, at: float) -> None:
        with self._lock:
            key = (channel, str(identity))
            self._last[key] = max(at, self._last.get(key, 0.0))

    def last(self, channel: str, identity: str) -> float | None:
        return self._last.get((channel, str(identity)))


class PgWindows:
    """Table `channel_windows` (migration 048) : dernier message ENTRANT par identité (clé = empreinte fournie par
    l'adaptateur, jamais le numéro), partagé entre répliques."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def touch(self, channel: str, identity: str, at: float) -> None:
        import psycopg

        ts = datetime.fromtimestamp(at, timezone.utc)
        with psycopg.connect(self.dsn) as conn:
            conn.execute("INSERT INTO channel_windows (channel, identity_sha256, last_inbound_at) VALUES (%s,%s,%s) "
                         "ON CONFLICT (channel, identity_sha256) DO UPDATE SET last_inbound_at = "
                         "GREATEST(channel_windows.last_inbound_at, EXCLUDED.last_inbound_at)", (channel, identity, ts))

    def last(self, channel: str, identity: str) -> float | None:
        import psycopg

        with psycopg.connect(self.dsn) as conn:
            row = conn.execute("SELECT last_inbound_at FROM channel_windows WHERE channel=%s AND identity_sha256=%s",
                               (channel, identity)).fetchone()
        return row[0].timestamp() if row else None


# ─────────────────────────────── débit ───────────────────────────────


@dataclass
class RateLimiter:
    """Seau à jetons par clé : `per_minute` jetons par minute, `burst` au plus. `allow` → (permis, premier refus)."""

    per_minute: float = 20.0
    burst: int = 10
    clock: Callable[[], float] = time.monotonic
    _state: dict[str, tuple[float, float, bool]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def allow(self, key: str) -> tuple[bool, bool]:
        now = self.clock()
        with self._lock:
            tokens, last, warned = self._state.get(key, (float(self.burst), now, False))
            tokens = min(float(self.burst), tokens + (now - last) * self.per_minute / 60.0)
            if tokens >= 1.0:
                self._state[key] = (tokens - 1.0, now, False)
                return True, False
            self._state[key] = (tokens, now, True)
            if len(self._state) > 100_000:  # borne : les seaux pleins s'oublient sans perte
                for k in [k for k, (t, _, w) in self._state.items() if t >= self.burst and not w][:50_000]:
                    del self._state[k]
            return False, not warned


# ─────────────────────────────── voix ───────────────────────────────


class Transcriber(Protocol):
    def transcribe(self, data: bytes, mime: str) -> str: ...


@dataclass
class FasterWhisperTranscriber:
    """Le STT déjà retenu (faster-whisper, extra `stt-whisper` de l'image), dans le processus, sans la passerelle
    amont. Le modèle se charge au premier usage (lent la première fois). Langue détectée automatiquement."""

    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    _impl: Any = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def transcribe(self, data: bytes, mime: str) -> str:
        import os
        import tempfile

        with self._lock:
            if self._impl is None:
                from faster_whisper import WhisperModel  # extra stt-whisper ; absent → l'appelant répond voice_off

                self._impl = WhisperModel(self.model, device=self.device, compute_type=self.compute_type)
        suffix = ".ogg" if "ogg" in (mime or "") else ".m4a" if "mp4" in (mime or "") else ".audio"
        fd, path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            segments, _info = self._impl.transcribe(path, vad_filter=True)
            return " ".join(s.text.strip() for s in segments).strip()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


# ─────────────────────────────── message entrant → tour ───────────────────────────────


@dataclass
class Inbound:
    """Un message entrant déjà authentifié (signature du fournisseur vérifiée) et dédoublonné."""

    channel: str  # slack | whatsapp
    identity: str  # identité du canal liée au compte : "<équipe>:<utilisateur>" (Slack), wa_id (WhatsApp)
    conversation: str  # verrou : un tour à la fois par conversation
    thread_key: str  # id client du fil Nokker (avant `scoped_thread_id`)
    text: str
    reply: Callable[[str], None]  # envoie un texte (déjà caviardé par le socle) dans la conversation
    private: bool = True  # message privé (DM) ; l'appairage ne se fait que là
    voice: Callable[[], tuple[bytes, str]] | None = None  # télécharge la note vocale → (octets, type MIME)
    unsupported_media: bool = False
    typing: Callable[[], None] | None = None
    lang: str | None = None  # langue d'un expéditeur non relié (aucune autre source)


@dataclass
class ChannelCore:
    channel: str
    display_name: str  # « Slack », « WhatsApp » (textes)
    tenancy: Tenancy
    pairing: PairingStore
    run_turn: Callable[[TurnInput], Turn]
    scrub: Callable[[str], str]
    limiter: RateLimiter = field(default_factory=RateLimiter)
    transcriber: Transcriber | None = None
    default_lang: str = "fr"
    _locks: dict[str, threading.Lock] = field(default_factory=dict, repr=False)
    _guard: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _say(self, msg: Inbound, text: str) -> None:
        msg.reply(self.scrub(text))

    def process(self, msg: Inbound) -> None:
        account = None
        t = texts(msg.lang or self.default_lang)
        try:
            ok, first_refusal = self.limiter.allow(f"{self.channel}:{msg.identity}")
            if not ok:
                if first_refusal:
                    self._say(msg, t["rate"])
                return
            code = parse_pair_command(msg.text)
            if code is not None:
                if not msg.private:
                    self._say(msg, t["pair_private"])
                    return
                self._pair(msg, code, t)
                return
            account = self.tenancy.account_for_identity(self.channel, msg.identity)
            if account is None:
                self._say(msg, t["unpaired"].format(name=self.display_name))
                return
            t = texts(account.lang)
            text = (msg.text or "").strip()
            if msg.voice is not None and not text:
                if self.transcriber is None:
                    self._say(msg, t["voice_off"])
                    return
                try:
                    data, mime = msg.voice()
                except VoiceTooBig:
                    self._say(msg, t["voice_big"])
                    return
                text = (self.transcriber.transcribe(data, mime) or "").strip()
                if not text:
                    self._say(msg, t["voice_empty"])
                    return
            if not text:
                self._say(msg, t["text_only"])
                return
            with self._lock(msg.conversation), tenant_scope(account.tenant_id):
                if msg.typing is not None:
                    try:
                        msg.typing()
                    except Exception:  # noqa: BLE001 — confort seulement
                        pass
                inp = TurnInput(thread_id=scoped_thread_id(account.tenant_id, msg.thread_key), user_text=text,
                                channel=self.channel, sender=account.account_id)
                turn = self.run_turn(inp)
                self._say(msg, turn.reply)
        except Exception:  # noqa: BLE001 — un tour raté ne tue pas le pool
            logger.exception("tour %s en échec (%s)", self.channel, mask(msg.identity))
            try:
                self._say(msg, t["error"])
            except Exception:  # noqa: BLE001
                logger.warning("réponse d'erreur %s impossible", self.channel, exc_info=True)

    def _pair(self, msg: Inbound, code: str, t: dict[str, str]) -> None:
        account_id = self.pairing.redeem(code)
        if account_id is None:
            self._say(msg, t["bad_code"])
            return
        try:
            self.tenancy.bind_identity(account_id, self.channel, msg.identity)
        except IdentityTaken:
            self._say(msg, t["taken"].format(name=self.display_name))
            return
        acc = self.tenancy.account(account_id)
        self._say(msg, texts(acc.lang if acc else None)["paired"].format(name=self.display_name))


class VoiceTooBig(Exception):
    """La note vocale dépasse la taille admise : pas de téléchargement complet, pas de transcription."""


@dataclass
class Dispatcher:
    """Pool de traitement : le webhook répond tout de suite, le tour tourne ici. `sync=True` (tests) : en ligne."""

    workers: int = 4
    sync: bool = False
    name: str = "nokker-channel"
    _pool: ThreadPoolExecutor | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.sync:
            self._pool = ThreadPoolExecutor(self.workers, thread_name_prefix=self.name)

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        # Pas de contexte à emporter : le locataire est posé DANS `ChannelCore.process`, après la résolution du compte.
        if self._pool is None:
            fn(*args)
        else:
            self._pool.submit(fn, *args)


# ─────────────────────────────── cartes d'approbation ───────────────────────────────


@dataclass(frozen=True)
class Card:
    text: str
    label: str
    url: str
    strong: bool


def card_for(approval: Any, lang: str, app_url: str, scrub: Callable[[str], str]) -> Card:
    """Message d'une carte : outil, motif, niveau, et le LIEN vers la carte de l'app web. Jamais les arguments (ils
    restent dans l'app, derrière la session), jamais de quoi décider ici."""
    t = texts(lang)
    strong = getattr(approval, "strength", "standard") == "strong"
    lines = [t["card"].format(tool=approval.tool_name)]
    if getattr(approval, "reason", ""):
        lines.append(str(approval.reason)[:500])
    lines.append(t["card_strong"] if strong else t["card_standard"])
    return Card(text=scrub("\n".join(lines)), label=t["open"],
                url=f"{app_url.rstrip('/')}/approvals/{approval.id}", strong=strong)


@dataclass
class ChannelCardNotifier:
    """Carte d'un testeur → ses identités liées sur CE canal, et à personne d'autre (locataire de la carte)."""

    tenancy: Tenancy
    channel: str
    send_card: Callable[[str, Card], None]  # (identité, carte)
    app_url: str
    scrub: Callable[[str], str]

    def __call__(self, approval: Any) -> None:
        account = self.tenancy.account_for_tenant(tenant_of_thread(approval.thread_id))
        if account is None:
            return
        for identity in self.tenancy.identities_of(account.account_id, self.channel):
            try:
                self.send_card(identity, card_for(approval, account.lang, self.app_url, self.scrub))
            except Exception:  # noqa: BLE001 — une carte non notifiée reste dans l'app web
                logger.warning("carte %s non notifiée sur %s (%s)", approval.id, self.channel, mask(identity),
                               exc_info=True)


@dataclass
class FanoutNotifier:
    """Plusieurs notificateurs de testeur (Telegram, Slack, WhatsApp) derrière le seul `tester_notifier`."""

    notifiers: list[Callable[[Any], None]]

    def __call__(self, approval: Any) -> None:
        for n in self.notifiers:
            try:
                n(approval)
            except Exception:  # noqa: BLE001
                logger.exception("notificateur de carte en échec (%s)", type(n).__name__)


__all__ = ["CHANNELS", "Card", "ChannelCardNotifier", "ChannelCore", "Dispatcher", "EventDedup", "FanoutNotifier",
           "FasterWhisperTranscriber", "Inbound", "MemoryDedup", "MemoryPairing", "MemoryWindows", "PgChannelPairing",
           "PgDedup", "PgWindows", "RateLimiter", "TEXTS", "Transcriber", "VoiceTooBig", "WindowStore", "card_for",
           "mask", "parse_pair_command", "split_text", "texts"]
