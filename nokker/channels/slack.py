"""Canal Slack du noyau (docs/CHANNELS.md §2) — Events API, webhook HTTP signé.

Pourquoi l'Events API et pas le Socket Mode : le noyau reçoit déjà ses canaux par webhook (Telegram, Stripe), sans
connexion sortante longue ; plusieurs répliques derrière l'équilibreur reçoivent chacune une partie des événements,
le rejeu est dédoublonné en base (`channel_events`). Le Socket Mode demanderait un jeton d'application de plus
(`xapp-…`), une WebSocket par processus, et Slack distribuerait les événements entre les connexions ouvertes : à
plusieurs répliques, chacune devrait tenir la sienne et la reprise après coupure serait à notre charge.

- **Signature v0** : `X-Slack-Signature = "v0=" + HMAC-SHA256(signing_secret, "v0:" + ts + ":" + corps brut)`,
  comparée en temps constant ; `X-Slack-Request-Timestamp` à ±5 min (anti-rejeu), sinon 401.
- **Entrées** : message privé au bot (`message`, `channel_type = im`) et mention dans un canal (`app_mention`). Les
  messages de bots, les éditions et suppressions (`subtype`) sont ignorés, comme les autres équipes Slack.
- **Fils** : en message privé, la conversation est UN fil Nokker, et un fil Slack ouvert dans ce message privé en est
  un autre ; dans un canal, chaque fil Slack (racine = le message mentionné) est un fil Nokker, et la réponse part dans
  ce fil. Le fil est toujours celui du LOCATAIRE de l'auteur (`scoped_thread_id`) : deux comptes qui mentionnent le bot
  dans le même fil Slack ont deux fils Nokker distincts.
- **Sortie** : `chat.postMessage` seulement (et le téléchargement d'une note vocale sur `files.slack.com`, si la voix
  est activée). Les cartes partent en message privé, jamais dans un canal.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
import urllib.error
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib import request as urllib_request

from nokker.channels.common import Card, ChannelCore, Dispatcher, EventDedup, Inbound, MemoryDedup, VoiceTooBig, \
    mask, split_text

logger = logging.getLogger("nokker.channels.slack")

API = "https://slack.com/api/"
MAX_SKEW_S = 300
MAX_LEN = 3500  # Slack accepte 40 000 caractères mais conseille 4 000 ; marge pour couper sur un saut de ligne
OPEN_CARD_ACTION = "nokker_open_card"
FILE_HOSTS = ("files.slack.com",)
_MENTION = re.compile(r"<@[A-Z0-9]+(?:\|[^>]*)?>")

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                     timeout_s: float = 15.0, max_bytes: int = 20_000_000) -> tuple[int, dict[str, str], bytes]:
    req = urllib_request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib_request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 — hôtes fixes (appelants)
            return resp.status, dict(resp.headers.items()), resp.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()) if e.headers else {}, e.read(65536)


def verify_signature(signing_secret: str, timestamp: str, signature: str, body: bytes,
                     now: float | None = None, max_skew_s: int = MAX_SKEW_S) -> bool:
    """Signature v0 d'une requête Slack. Horodatage hors de ±`max_skew_s` : refusée (rejeu)."""
    if not signing_secret or not timestamp or not signature.startswith("v0="):
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((time.time() if now is None else now) - ts) > max_skew_s:
        return False
    base = b"v0:" + str(ts).encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode())


def sign(signing_secret: str, timestamp: int, body: bytes) -> str:
    """Signature v0 (tests et outillage)."""
    return "v0=" + hmac.new(signing_secret.encode(), b"v0:" + str(timestamp).encode() + b":" + body,
                            hashlib.sha256).hexdigest()


@dataclass
class SlackApi:
    """`chat.postMessage` (et le téléchargement d'un fichier audio privé). Jeton relu à chaque appel."""

    token: Callable[[], str]
    transport: Transport = urllib_transport
    sleep: Callable[[float], None] = time.sleep
    max_retry_after_s: float = 30.0

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {self.token()}"}
        for attempt in range(2):
            status, h, raw = self.transport("POST", API + method, headers, body)
            if status == 429 and attempt == 0:
                # Refusé AVANT publication : le réessai ne double rien. Une seule fois, borné.
                wait = min(float(h.get("Retry-After") or h.get("retry-after") or 1), self.max_retry_after_s)
                self.sleep(wait)
                continue
            break
        if status != 200:
            raise RuntimeError(f"slack {method}: HTTP {status}")
        data = json.loads(raw or b"{}")
        if not data.get("ok"):
            raise RuntimeError(f"slack {method}: {data.get('error', 'erreur')}")
        return data

    def post(self, channel: str, text: str, thread_ts: str | None = None) -> None:
        for part in split_text(text, MAX_LEN):
            payload: dict[str, Any] = {"channel": channel, "text": part, "unfurl_links": False, "unfurl_media": False}
            if thread_ts:
                payload["thread_ts"] = thread_ts
            self._post("chat.postMessage", payload)

    def post_card(self, user_id: str, card: Card) -> None:
        """Carte en message privé : texte + UN bouton-lien vers l'app web. Aucun bouton « approuver »/« refuser »."""
        self._post("chat.postMessage", {
            "channel": user_id, "text": f"{card.text}\n{card.url}", "unfurl_links": False,
            "blocks": [
                {"type": "section", "text": {"type": "plain_text", "text": card.text[:2900]}},
                {"type": "actions", "elements": [
                    {"type": "button", "action_id": OPEN_CARD_ACTION, "url": card.url,
                     "text": {"type": "plain_text", "text": card.label[:70]}}]},
            ]})

    def download(self, url: str, max_bytes: int) -> bytes:
        host = urllib.parse.urlsplit(url).hostname or ""
        if urllib.parse.urlsplit(url).scheme != "https" or host not in FILE_HOSTS:
            raise ValueError("fichier Slack hors de files.slack.com : refusé (le jeton n'en sort pas)")
        status, _h, raw = self.transport("GET", url, {"Authorization": f"Bearer {self.token()}"}, None)
        if status != 200:
            raise RuntimeError(f"slack fichier : HTTP {status}")
        if len(raw) > max_bytes:
            raise VoiceTooBig()
        return raw


@dataclass
class SlackAdapter:
    core: ChannelCore
    api: Any  # SlackApi ou faux : post(channel, text, thread_ts), post_card(user, card), download(url, max)
    team_id: str
    app_id: str = ""  # vide = pas de contrôle d'`api_app_id`
    bot_user_id: str = ""  # vide = pas de filtrage de ses propres messages (les bots sont ignorés de toute façon)
    channel_mentions: bool = True
    max_voice_bytes: int = 8_000_000
    dedup: EventDedup = field(default_factory=MemoryDedup)
    dispatcher: Dispatcher = field(default_factory=Dispatcher)

    def accepts(self, payload: dict[str, Any]) -> bool:
        if payload.get("team_id") != self.team_id:
            return False
        return not self.app_id or payload.get("api_app_id") == self.app_id

    def handle_event(self, payload: dict[str, Any]) -> None:
        """Un `event_callback` déjà signé. Rien de lent : le tour part au pool."""
        if payload.get("type") != "event_callback" or not self.accepts(payload):
            return
        event_id = str(payload.get("event_id") or "")
        if not event_id or not self.dedup.first_time("slack", event_id):
            return  # rejeu Slack (X-Slack-Retry-Num) ou événement déjà reçu par une autre réplique
        msg = self._inbound(payload.get("event") or {})
        if msg is not None:
            self.dispatcher.submit(self.core.process, msg)

    def _inbound(self, ev: dict[str, Any]) -> Inbound | None:
        kind = ev.get("type")
        user, channel = str(ev.get("user") or ""), str(ev.get("channel") or "")
        if not user or not channel or ev.get("bot_id") or (self.bot_user_id and user == self.bot_user_id):
            return None
        if ev.get("subtype") not in (None, "file_share"):
            return None  # éditions, suppressions, messages de bots, arrivées dans un canal…
        if kind == "message" and ev.get("channel_type") == "im":
            private = True
            root = ev.get("thread_ts")
            thread_key = f"sl-{self.team_id}-{channel}" + (f"-{root}" if root else "")
            reply_ts = root
        elif kind == "app_mention" and self.channel_mentions:
            private = False
            root = ev.get("thread_ts") or ev.get("ts")
            thread_key, reply_ts = f"sl-{self.team_id}-{channel}-{root}", root
        else:
            return None
        text = _MENTION.sub("", str(ev.get("text") or "")).strip()
        voice, unsupported = None, False
        files = ev.get("files") or []
        if files and not text:
            audio = next((f for f in files if str(f.get("mimetype", "")).startswith("audio/")), None)
            if audio is not None:
                voice = self._voice(audio)  # sans transcripteur, le socle répond voice_off sans télécharger
            else:
                unsupported = True
        api = self.api

        def reply(out: str) -> None:
            api.post(channel, out, reply_ts)

        msg = Inbound(channel="slack", identity=f"{self.team_id}:{user}", conversation=f"slack:{thread_key}",
                      thread_key=thread_key, text=text, reply=reply, private=private, voice=voice,
                      unsupported_media=unsupported)
        return msg

    def _voice(self, f: dict[str, Any]) -> Callable[[], tuple[bytes, str]]:
        url, mime, size = str(f.get("url_private_download") or ""), str(f.get("mimetype") or ""), f.get("size") or 0

        def fetch() -> tuple[bytes, str]:
            if int(size) > self.max_voice_bytes:
                raise VoiceTooBig()
            return self.api.download(url, self.max_voice_bytes), mime

        return fetch

    def handle_interaction(self, payload: dict[str, Any]) -> None:
        """`block_actions` d'une carte (bouton-lien) : rien n'est décidé ici, jamais. Un `action_id` inconnu
        (« approve », « reject »… forgés ou venus d'un vieux message) est journalisé et ignoré."""
        if (payload.get("team") or {}).get("id") != self.team_id:
            return
        for action in payload.get("actions") or []:
            if action.get("action_id") != OPEN_CARD_ACTION:
                logger.warning("slack : action %r ignorée (aucune carte ne se décide dans Slack) — %s",
                               str(action.get("action_id"))[:40], mask(str((payload.get("user") or {}).get("id", ""))))

    def send_card(self, identity: str, card: Card) -> None:
        team, _, user = identity.partition(":")
        if team != self.team_id or not user:
            return
        self.api.post_card(user, card)


__all__ = ["OPEN_CARD_ACTION", "SlackAdapter", "SlackApi", "sign", "urllib_transport", "verify_signature"]
