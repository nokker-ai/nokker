"""Canal WhatsApp du noyau (docs/CHANNELS.md §3) — WhatsApp Business Cloud API (Meta), webhook HTTP.

- **Vérification du webhook** (GET) : `hub.mode=subscribe`, `hub.verify_token` comparé en temps constant au jeton de
  vérification de la config → on renvoie `hub.challenge` en texte ; sinon 403.
- **Signature** (POST) : `X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(app_secret, corps brut)`, temps constant ;
  sinon 401. Seuls les événements du numéro configuré (`metadata.phone_number_id`) sont lus.
- **Entrées** : texte, note vocale (`audio`) → STT du noyau. Un bouton de réponse (`interactive`/`button`) ne décide
  jamais rien : Nokker n'envoie que des boutons-LIENS (`cta_url`) ; un clic forgé reçoit « ouvre l'app web ».
- **Fenêtre de 24 h** : WhatsApp n'autorise un message libre que dans les 24 h qui suivent le dernier message de
  l'utilisateur. La réponse à un message est toujours dans la fenêtre. Une carte d'approbation hors fenêtre part
  par un modèle approuvé chez Meta (`card_template`) s'il est configuré, sinon elle n'est PAS envoyée (journalisé) :
  elle reste dans l'app web. Le dernier message entrant est gardé par identité (`channel_windows`, empreinte seule).
- **Fils** : WhatsApp n'a pas de fils ; une conversation (un numéro) = un fil Nokker du locataire du compte.
- **Sortie** : `POST /{version}/{phone_number_id}/messages` (texte, `interactive` `cta_url`, modèle) et la lecture
  d'un média reçu (`GET /{version}/{media_id}` puis son URL, sur les hôtes de Meta seulement).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.channels.common import Card, ChannelCore, Dispatcher, EventDedup, Inbound, MemoryDedup, MemoryWindows, \
    VoiceTooBig, WindowStore, mask, split_text, texts
from nokker.channels.slack import Transport, urllib_transport

logger = logging.getLogger("nokker.channels.whatsapp")

GRAPH = "https://graph.facebook.com"
MAX_LEN = 4000  # 4096 côté Meta
WINDOW_S = 24 * 3600
WINDOW_MARGIN_S = 300  # on ne joue pas la dernière minute : une carte envoyée à 23 h 59 arriverait hors fenêtre
MEDIA_HOST_SUFFIXES = (".fbcdn.net", ".whatsapp.net", ".facebook.com", ".fbsbx.com")


def verify_signature(app_secret: str, header: str, body: bytes) -> bool:
    if not app_secret or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), header.strip().encode())


def sign(app_secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()


def verify_challenge(params: dict[str, list[str]], verify_token: str) -> str | None:
    """`hub.challenge` si la requête GET de Meta porte le bon jeton de vérification, sinon None."""
    def one(k: str) -> str:
        v = params.get(k) or [""]
        return v[0]

    if not verify_token or one("hub.mode") != "subscribe":
        return None
    if not hmac.compare_digest(one("hub.verify_token").encode(), verify_token.encode()):
        return None
    challenge = one("hub.challenge")
    return challenge if challenge and len(challenge) <= 200 and challenge.isprintable() else None


def _window_key(identity: str) -> str:
    """La fenêtre est rangée sous une empreinte : pas de numéro de téléphone hors de `account_identities`."""
    return hashlib.sha256(f"whatsapp:{identity}".encode()).hexdigest()


@dataclass
class WhatsAppApi:
    phone_number_id: str
    token: Callable[[], str]
    version: str = "v21.0"
    transport: Transport = urllib_transport
    sleep: Callable[[float], None] = time.sleep
    max_retry_after_s: float = 30.0

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"}

    def _messages(self, payload: dict[str, Any]) -> None:
        body = json.dumps({"messaging_product": "whatsapp", "recipient_type": "individual", **payload},
                          ensure_ascii=False).encode()
        url = f"{GRAPH}/{self.version}/{self.phone_number_id}/messages"
        for attempt in range(2):
            status, h, raw = self.transport("POST", url, self._headers(), body)
            if status == 429 and attempt == 0:  # refusé avant envoi : réessai sans doublon, une fois, borné
                self.sleep(min(float(h.get("Retry-After") or h.get("retry-after") or 1), self.max_retry_after_s))
                continue
            break
        if status != 200:
            try:
                err = (json.loads(raw or b"{}").get("error") or {}).get("code")
            except (ValueError, AttributeError):
                err = None
            raise RuntimeError(f"whatsapp messages: HTTP {status} (code {err})")

    def send_text(self, to: str, text: str) -> None:
        for part in split_text(text, MAX_LEN):
            self._messages({"to": to, "type": "text", "text": {"body": part, "preview_url": False}})

    def send_cta(self, to: str, body: str, label: str, url: str) -> None:
        """Message interactif à UN bouton-lien (`cta_url`) : il ouvre l'app web, il ne décide rien."""
        self._messages({"to": to, "type": "interactive", "interactive": {
            "type": "cta_url", "body": {"text": body[:1024]},
            "action": {"name": "cta_url", "parameters": {"display_text": label[:20], "url": url}}}})

    def send_template(self, to: str, name: str, lang: str, body_params: list[str], url_suffix: str) -> None:
        """Modèle approuvé chez Meta (seul envoi permis hors fenêtre de 24 h) : texte paramétré + bouton-lien dont la
        fin d'URL est l'identifiant de la carte."""
        self._messages({"to": to, "type": "template", "template": {
            "name": name, "language": {"code": lang},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": p[:200]} for p in body_params]},
                {"type": "button", "sub_type": "url", "index": "0",
                 "parameters": [{"type": "text", "text": url_suffix}]}]}})

    def media(self, media_id: str, max_bytes: int) -> tuple[bytes, str]:
        status, _h, raw = self.transport("GET", f"{GRAPH}/{self.version}/{urllib.parse.quote(media_id, safe='')}",
                                         self._headers(), None)
        if status != 200:
            raise RuntimeError(f"whatsapp média : HTTP {status}")
        meta = json.loads(raw or b"{}")
        if int(meta.get("file_size") or 0) > max_bytes:
            raise VoiceTooBig()
        url = str(meta.get("url") or "")
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if parts.scheme != "https" or not any(host.endswith(s) for s in MEDIA_HOST_SUFFIXES):
            raise ValueError("URL de média hors des hôtes de Meta : refusée (le jeton n'en sort pas)")
        status, _h, data = self.transport("GET", url, {"Authorization": f"Bearer {self.token()}"}, None)
        if status != 200:
            raise RuntimeError(f"whatsapp média : HTTP {status}")
        if len(data) > max_bytes:
            raise VoiceTooBig()
        return data, str(meta.get("mime_type") or "audio/ogg")


@dataclass
class WhatsAppAdapter:
    core: ChannelCore
    api: Any  # WhatsAppApi ou faux
    phone_number_id: str
    windows: WindowStore = field(default_factory=MemoryWindows)
    dedup: EventDedup = field(default_factory=MemoryDedup)
    dispatcher: Dispatcher = field(default_factory=Dispatcher)
    card_template: dict[str, Any] | None = None  # {name, lang} : modèle approuvé pour une carte hors fenêtre
    max_voice_bytes: int = 8_000_000
    max_age_s: float = 24 * 3600  # message plus vieux (renvoi tardif de Meta) : ignoré
    clock: Callable[[], float] = time.time
    skipped_cards: int = 0  # cartes non envoyées (hors fenêtre, sans modèle) — compteur pour la supervision

    def handle(self, payload: dict[str, Any]) -> None:
        """Un POST déjà signé. Rien de lent : chaque message part au pool."""
        if payload.get("object") != "whatsapp_business_account":
            return
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                if str((value.get("metadata") or {}).get("phone_number_id") or "") != self.phone_number_id:
                    continue
                for m in value.get("messages") or []:
                    self._message(m)

    def _message(self, m: dict[str, Any]) -> None:
        wamid, sender = str(m.get("id") or ""), str(m.get("from") or "")
        if not wamid or not sender.isdigit():
            return
        try:
            sent_at = float(m.get("timestamp") or 0)
        except (TypeError, ValueError):
            return
        if sent_at < self.clock() - self.max_age_s:
            return
        if not self.dedup.first_time("whatsapp", wamid):
            return  # renvoi de Meta, ou déjà reçu par une autre réplique
        self.windows.touch("whatsapp", _window_key(sender), sent_at)
        kind = m.get("type")
        text, voice = "", None
        if kind == "text":
            text = str((m.get("text") or {}).get("body") or "")
        elif kind == "audio":
            media_id, api, cap = str((m.get("audio") or {}).get("id") or ""), self.api, self.max_voice_bytes
            voice = (lambda: api.media(media_id, cap)) if media_id else None
        elif kind in ("interactive", "button"):
            # Nokker n'envoie aucun bouton de réponse : celui-ci est forgé ou vient d'ailleurs. Jamais une décision.
            self.dispatcher.submit(self._decide_in_app, sender)
            return
        api = self.api

        def reply(out: str) -> None:
            api.send_text(sender, out)

        self.dispatcher.submit(self.core.process, Inbound(
            channel="whatsapp", identity=sender, conversation=f"whatsapp:{sender}", thread_key=f"wa-{sender}",
            text=text, reply=reply, private=True, voice=voice, unsupported_media=kind not in ("text", "audio")))

    def _decide_in_app(self, sender: str) -> None:
        account = self.core.tenancy.account_for_identity("whatsapp", sender)
        if account is None:
            return
        if self.core.limiter.allow(f"whatsapp:{sender}")[0]:
            self.api.send_text(sender, self.core.scrub(texts(account.lang)["decide_in_app"]))

    def in_window(self, identity: str) -> bool:
        last = self.windows.last("whatsapp", _window_key(identity))
        return last is not None and self.clock() - last < WINDOW_S - WINDOW_MARGIN_S

    def send_card(self, identity: str, card: Card) -> None:
        if self.in_window(identity):
            self.api.send_cta(identity, card.text, card.label, card.url)
            return
        tpl = self.card_template or {}
        if tpl.get("name"):
            tool = card.text.splitlines()[0] if card.text else ""
            self.api.send_template(identity, str(tpl["name"]), str(tpl.get("lang") or "fr"), [tool],
                                   card.url.rsplit("/", 1)[-1])
            return
        self.skipped_cards += 1
        logger.info("whatsapp : carte non envoyée à %s, hors de la fenêtre de 24 h et sans modèle approuvé "
                    "(elle reste dans l'app web)", mask(identity))


__all__ = ["WINDOW_S", "WhatsAppAdapter", "WhatsAppApi", "sign", "verify_challenge", "verify_signature"]
