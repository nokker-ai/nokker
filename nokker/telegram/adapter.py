"""Bot Telegram de la bêta → noyau (webhook). Un message privé d'un id Telegram appairé tourne dans le locataire de son
compte (`tenant_scope`), sur un fil propre au chat (`scoped_thread_id(tenant, "tg-<chat>")`). Un id non appairé ne
déclenche aucun tour : il reçoit la marche à suivre. `/start <code>` relie l'id au compte (contrat C4).

Ce module n'appelle que `sendMessage`, `sendChatAction` et `sendVoice` (réponse vocale, docs/VOICE.md, éteinte par
défaut) : jamais `getUpdates` (le webhook est la seule lecture).
Questions à choix (drapeau `host_clarify_options`, docs/NOKKAN-HOST-API.md §1) : avec un tableau des questions posé
(`questions`), une question de `clarify` part avec un bouton par option et « Autre (à préciser) », et un appui
(`callback_query`, à ajouter aux `allowed_updates` du webhook) répond comme un message. Sans tableau : rien ne change.
Le webhook répond tout de suite ; le tour tourne dans un pool, un tour à la fois par chat.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib import request as urllib_request

from nokker.approvals.rule_labels import norm_lang
from nokker.core.turn import Turn, TurnInput
from nokker.tenancy.context import scoped_thread_id, tenant_scope
from nokker.tenancy.store import IdentityTaken, Tenancy
from nokker.telegram.pairing import PairingStore

logger = logging.getLogger("nokker.telegram")

CALLBACK_PREFIX = "q:"  # `callback_data` d'un bouton de question : q:<question_id>:<option> (64 octets au plus)
MAX_LEN = 4000  # Telegram coupe à 4096 ; marge pour les découpes sur un saut de ligne

TEXTS = {
    "fr": {
        "unpaired": ("Ce compte Telegram n'est relié à aucun compte Nokker. Dans l'app web Nokker, ouvre « Telegram » "
                     "pour obtenir un code, puis envoie ici : /start CODE"),
        "paired": "C'est fait : ce Telegram est relié à ton compte Nokker. Tu peux m'écrire ici.",
        "bad_code": "Ce code n'est pas valable (déjà utilisé ou expiré). Demandes-en un nouveau dans l'app web.",
        "taken": "Ce Telegram est déjà relié à un autre compte Nokker.",
        "text_only": "Pour l'instant, je ne lis que les messages texte ici.",
        "error": "Une erreur m'a empêché de répondre. Réessaie dans un instant.",
        "feedback_ok": "Merci ! Ton retour est transmis à l'équipe Nokker.",
        "feedback_empty": "Écris ton retour après la commande, par exemple : /retour la réponse sur l'agenda était fausse",
        "voice_on": "Réponses vocales activées : je réponds aussi par une note vocale.",
        "voice_off": "Réponses vocales coupées : je réponds par écrit seulement.",
        "voice_unavailable": "Les réponses vocales ne sont pas disponibles sur cette instance.",
        "question_other": "D'accord : écris ta réponse ici.",
        "question_closed": "Cette question n'attend plus de réponse.",
    },
    "en": {
        "unpaired": ("This Telegram account isn't linked to a Nokker account. In the Nokker web app, open “Telegram” "
                     "to get a code, then send here: /start CODE"),
        "paired": "Done: this Telegram is linked to your Nokker account. You can write to me here.",
        "bad_code": "This code isn't valid (already used or expired). Get a new one in the web app.",
        "taken": "This Telegram is already linked to another Nokker account.",
        "text_only": "For now I only read text messages here.",
        "error": "An error kept me from answering. Please try again in a moment.",
        "feedback_ok": "Thanks! Your feedback reached the Nokker team.",
        "feedback_empty": "Write your feedback after the command, e.g. /feedback the calendar answer was wrong",
        "voice_on": "Voice replies on: I'll also answer with a voice note.",
        "voice_off": "Voice replies off: I'll answer in writing only.",
        "voice_unavailable": "Voice replies aren't available on this instance.",
        "question_other": "OK: write your answer here.",
        "question_closed": "This question is no longer waiting for an answer.",
    },
    "de": {
        "unpaired": ("Dieses Telegram-Konto ist mit keinem Nokker-Konto verknüpft. Öffne in der Nokker-Web-App "
                     "«Telegram», um einen Code zu erhalten, und sende ihn dann hier: /start CODE"),
        "paired": "Erledigt: Dieses Telegram-Konto ist mit deinem Nokker-Konto verknüpft. Du kannst mir hier schreiben.",
        "bad_code": "Dieser Code ist nicht gültig (bereits verwendet oder abgelaufen). Fordere in der Web-App einen "
                    "neuen an.",
        "taken": "Dieses Telegram-Konto ist bereits mit einem anderen Nokker-Konto verknüpft.",
        "text_only": "Im Moment lese ich hier nur Textnachrichten.",
        "error": "Ein Fehler hat mich an der Antwort gehindert. Versuche es gleich noch einmal.",
        "feedback_ok": "Danke! Dein Feedback ist beim Nokker-Team angekommen.",
        "feedback_empty": "Schreibe dein Feedback nach dem Befehl, zum Beispiel: /feedback Die Antwort zum Kalender "
                          "war falsch",
        "voice_on": "Sprachantworten an: Ich antworte zusätzlich mit einer Sprachnachricht.",
        "voice_off": "Sprachantworten aus: Ich antworte nur schriftlich.",
        "voice_unavailable": "Sprachantworten sind auf dieser Instanz nicht verfügbar.",
        "question_other": "In Ordnung: Schreibe deine Antwort hier.",
        "question_closed": "Diese Frage wartet nicht mehr auf eine Antwort.",
    },
    "it": {
        "unpaired": ("Questo account Telegram non è collegato ad alcun account Nokker. Nell'app web di Nokker apri "
                     "«Telegram» per ottenere un codice, poi invialo qui: /start CODE"),
        "paired": "Fatto: questo Telegram è collegato al tuo account Nokker. Puoi scrivermi qui.",
        "bad_code": "Questo codice non è valido (già usato o scaduto). Chiedine uno nuovo nell'app web.",
        "taken": "Questo Telegram è già collegato a un altro account Nokker.",
        "text_only": "Per ora qui leggo solo messaggi di testo.",
        "error": "Un errore mi ha impedito di rispondere. Riprova tra un momento.",
        "feedback_ok": "Grazie! Il tuo feedback è arrivato al team di Nokker.",
        "feedback_empty": "Scrivi il tuo feedback dopo il comando, per esempio: /feedback la risposta sull'agenda "
                          "era sbagliata",
        "voice_on": "Risposte vocali attive: rispondo anche con una nota vocale.",
        "voice_off": "Risposte vocali disattivate: rispondo solo per iscritto.",
        "voice_unavailable": "Le risposte vocali non sono disponibili su questa istanza.",
        "question_other": "D'accordo: scrivi qui la tua risposta.",
        "question_closed": "Questa domanda non attende più una risposta.",
    },
}


@dataclass
class BotApi:
    """`sendMessage` / `sendChatAction` / `sendVoice` seulement."""

    token: str
    timeout_s: float = 15.0

    def _call(self, method: str, payload: dict[str, Any]) -> None:
        req = urllib_request.Request(f"https://api.telegram.org/bot{self.token}/{method}",
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib_request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310 — hôte fixe
            resp.read()

    def send(self, chat_id: int | str, text: str) -> None:
        for part in split_message(text):
            self._call("sendMessage", {"chat_id": chat_id, "text": part, "disable_web_page_preview": True})

    def send_with_button(self, chat_id: int | str, text: str, label: str, url: str) -> None:
        self._call("sendMessage", {"chat_id": chat_id, "text": text[:MAX_LEN], "disable_web_page_preview": True,
                                   "reply_markup": {"inline_keyboard": [[{"text": label, "url": url}]]}})

    def send_with_buttons(self, chat_id: int | str, text: str, buttons: list[dict[str, Any]]) -> None:
        """Une rangée de boutons inline : `{"text", "url"}` ou `{"text", "web_app": {"url"}}` (Mini App). `sendMessage`."""
        self._call("sendMessage", {"chat_id": chat_id, "text": text[:MAX_LEN], "disable_web_page_preview": True,
                                   "reply_markup": {"inline_keyboard": [buttons]}})

    def send_keyboard(self, chat_id: int | str, text: str, rows: list[list[dict[str, Any]]]) -> None:
        """Boutons inline en plusieurs rangées, `{"text", "callback_data"}` (question à choix). `sendMessage`."""
        self._call("sendMessage", {"chat_id": chat_id, "text": text[:MAX_LEN], "disable_web_page_preview": True,
                                   "reply_markup": {"inline_keyboard": rows}})

    def answer_callback(self, callback_id: str) -> None:
        """Accuse réception d'un appui sur un bouton (sinon Telegram laisse le bouton « en attente »)."""
        try:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id})
        except Exception:  # noqa: BLE001 — confort seulement
            pass

    def send_voice(self, chat_id: int | str, ogg_opus: bytes) -> None:
        """Note vocale (OGG/Opus), en `multipart/form-data` : `sendVoice`."""
        boundary = "nokker-" + secrets.token_hex(12)
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'.encode(),
                 (f'--{boundary}\r\nContent-Disposition: form-data; name="voice"; filename="reply.ogg"\r\n'
                  "Content-Type: audio/ogg\r\n\r\n").encode() + ogg_opus + b"\r\n",
                 f"--{boundary}--\r\n".encode()]
        req = urllib_request.Request(f"https://api.telegram.org/bot{self.token}/sendVoice", data=b"".join(parts),
                                     method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib_request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310 — hôte fixe
            resp.read()

    def typing(self, chat_id: int | str) -> None:
        try:
            self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception:  # noqa: BLE001 — confort seulement
            pass


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    text = text or "…"
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


@dataclass
class TelegramAdapter:
    tenancy: Tenancy
    pairing: PairingStore
    run_turn: Callable[[TurnInput], Turn]  # le même chemin que l'API (verrou de fil du hub compris)
    bot: Any  # BotApi ou faux de test : send(chat_id, text), typing(chat_id)
    feedback: Any = None  # nokker/feedback.py::Feedback ; None = /retour indisponible
    # Réponses vocales (docs/VOICE.md, `nokker.voice.tts.VoiceReplies`) ; None = texte seul, comme avant.
    voice: Any = None
    # Foyer du compte pour le lexique de prononciation (docs/VOICE.md §6) : `(compte) -> SpeakScope | None`. None
    # (défaut) : rien n'est passé au fournisseur de voix, comme avant.
    speak_scope: Callable[[Any], Any] | None = None
    # Conditions à réaccepter (`onboarding.http.terms_outdated_for`) : `(account_id) -> bool`. Vrai : AUCUNE note vocale
    # (aucun texte chez le fournisseur de voix), texte seul. None (défaut) : rien ne change.
    terms_outdated: Callable[[str], bool] | None = None
    # Transcription d'une note vocale entrante : `(message Telegram) -> texte | None`. None (défaut du noyau) = une note
    # vocale reçoit « je ne lis que le texte », comme avant ; la STT de la passerelle amont n'est pas dans le noyau.
    transcribe: Callable[[dict[str, Any]], str | None] | None = None
    # Questions à choix (`nokker.tools.clarify.QuestionBoard`, drapeau `host_clarify_options`) : None (défaut) = la
    # réponse part en texte comme avant et les `callback_query` sont ignorés.
    questions: Any = None
    workers: int = 4
    _pool: ThreadPoolExecutor = field(init=False, repr=False)
    _chat_locks: dict[str, threading.Lock] = field(default_factory=dict, repr=False)
    _locks_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self._pool = ThreadPoolExecutor(self.workers, thread_name_prefix="nokker-telegram")

    def _lock(self, chat_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._chat_locks.setdefault(chat_id, threading.Lock())

    def handle_update(self, update: dict[str, Any]) -> None:
        """Appelé par le webhook : rien de lent ici, le travail part au pool."""
        cq = update.get("callback_query")
        if isinstance(cq, dict) and self.questions is not None:
            cq_chat = (cq.get("message") or {}).get("chat") or {}
            if cq_chat.get("type") == "private" and str(cq.get("data") or "").startswith(CALLBACK_PREFIX):
                self._pool.submit(self._process_choice, cq)
            return
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        if not msg or chat.get("type") != "private" or sender.get("is_bot"):
            return  # bêta : conversations privées seulement ; groupes et canaux ignorés
        self._pool.submit(self._process, msg)

    def _process(self, msg: dict[str, Any]) -> None:
        chat_id = msg["chat"]["id"]
        tg_id = str(msg["from"]["id"])
        code = str(msg["from"].get("language_code", "fr"))
        lang = "fr" if code.startswith("fr") else "en"  # langue de la synthèse vocale (fr/en seulement)
        t = TEXTS[norm_lang(code) or "en"]  # textes du bot : fr/en/de/it, toute autre langue d'un client : anglais
        text = (msg.get("text") or "").strip()
        try:
            if text.startswith("/start"):
                self._pair(chat_id, tg_id, text[len("/start"):].strip(), t)
                return
            account = self.tenancy.account_for_identity("telegram", tg_id)
            if account is None:
                self.bot.send(chat_id, t["unpaired"])
                return
            voice_in = False
            if not text and (msg.get("voice") or msg.get("audio")) and self.transcribe is not None:
                text = (self.transcribe(msg) or "").strip()
                voice_in = bool(text)
            if not text:
                self.bot.send(chat_id, t["text_only"])
                return
            head, _, rest = text.partition(" ")
            if head.split("@")[0] in ("/voix", "/voice"):
                self._voice_pref(chat_id, rest.strip().lower(), t)
                return
            if head.split("@")[0] in ("/retour", "/feedback") and self.feedback is not None:
                with tenant_scope(account.tenant_id):
                    fid = self.feedback.submit(account, "telegram", rest)
                self.bot.send(chat_id, t["feedback_ok"] if fid else t["feedback_empty"])
                return
            with self._lock(str(chat_id)), tenant_scope(account.tenant_id):
                self.bot.typing(chat_id)
                tg_lang = norm_lang(msg["from"].get("language_code"))  # langue du client Telegram, quand il en annonce une
                inp = TurnInput(thread_id=scoped_thread_id(account.tenant_id, f"tg-{chat_id}"), user_text=text,
                                channel="telegram", sender=account.account_id, lang=tg_lang,
                                lang_recognized=tg_lang is not None)
                turn = self.run_turn(inp)
                self._send_reply(chat_id, turn)
                self._maybe_voice(chat_id, turn.reply, lang, voice_in, account)
        except Exception:  # noqa: BLE001 — un tour raté ne tue pas le pool
            logger.exception("tour Telegram en échec (chat %s)", chat_id)
            try:
                self.bot.send(chat_id, t["error"])
            except Exception:  # noqa: BLE001
                logger.warning("réponse d'erreur Telegram impossible", exc_info=True)

    def _send_reply(self, chat_id: int, turn: Turn) -> None:
        """La réponse du tour ; une question à choix (tableau posé, tour qui en porte une avec des options) part avec
        ses boutons, un par rangée, « Autre (à préciser) » en dernier. Clavier refusé par Telegram : repli en texte."""
        q = getattr(turn, "question", None) if self.questions is not None else None
        keyboard = getattr(self.bot, "send_keyboard", None)
        if isinstance(q, dict) and q.get("question_id") and q.get("options") and keyboard is not None:
            qid = q["question_id"]
            rows = [[{"text": o["label"], "callback_data": f"{CALLBACK_PREFIX}{qid}:{o['id']}"}] for o in q["options"]]
            other = q.get("other") or {}
            rows.append([{"text": other.get("label", ""), "callback_data": f"{CALLBACK_PREFIX}{qid}:{other.get('id')}"}])
            try:
                keyboard(chat_id, q["question"], rows)
                return
            except Exception:  # noqa: BLE001 — la question reste lisible : le texte numéroté part à la place
                logger.warning("clavier de question refusé (chat %s) : repli en texte", chat_id, exc_info=True)
        self.bot.send(chat_id, turn.reply)

    def _process_choice(self, cq: dict[str, Any]) -> None:
        """Appui sur un bouton d'une question : même identité, même fil et même verrou qu'un message du chat. Une
        option → le tour de suite avec son libellé ; « Autre » → la personne écrit sa réponse (la question reste en
        attente, son prochain message la ferme)."""
        from nokker.tools.clarify import OTHER_ID, QuestionError

        chat_id = cq["message"]["chat"]["id"]
        user = cq.get("from") or {}
        t = TEXTS["fr" if str(user.get("language_code", "fr")).startswith("fr") else "en"]
        answer = getattr(self.bot, "answer_callback", None)
        if answer is not None:
            answer(str(cq.get("id") or ""))
        try:
            qid, _, option = str(cq.get("data") or "")[len(CALLBACK_PREFIX):].partition(":")
            account = self.tenancy.account_for_identity("telegram", str(user.get("id")))
            if account is None:
                self.bot.send(chat_id, t["unpaired"])
                return
            thread_id = scoped_thread_id(account.tenant_id, f"tg-{chat_id}")
            q = self.questions.get(qid)
            if q is None or q.thread_id != thread_id or q.status != "pending":  # autre chat, autre compte, déjà close
                self.bot.send(chat_id, t["question_closed"])
                return
            if option == OTHER_ID:
                self.bot.send(chat_id, t["question_other"])
                return
            with self._lock(str(chat_id)), tenant_scope(account.tenant_id):
                try:
                    _, said = self.questions.answer(qid, choice=option, by=account.account_id, via="telegram")
                except QuestionError:
                    self.bot.send(chat_id, t["question_closed"])
                    return
                self.bot.typing(chat_id)
                turn = self.run_turn(TurnInput(thread_id=thread_id, user_text=said, channel="telegram",
                                               sender=account.account_id))
                self._send_reply(chat_id, turn)
        except Exception:  # noqa: BLE001 — un tour raté ne tue pas le pool
            logger.exception("réponse à une question Telegram en échec (chat %s)", chat_id)
            try:
                self.bot.send(chat_id, t["error"])
            except Exception:  # noqa: BLE001
                logger.warning("réponse d'erreur Telegram impossible", exc_info=True)

    def _voice_pref(self, chat_id: int, arg: str, t: dict[str, str]) -> None:
        """`/voix on|off` (`/voice`) : réglage « réponses vocales » de ce chat (docs/VOICE.md). Aucun tour modèle."""
        if self.voice is None or not getattr(self.voice, "enabled", False):
            self.bot.send(chat_id, t["voice_unavailable"])
            return
        on = arg not in ("off", "non", "no", "0", "stop")
        self.voice.set_chat_pref(str(chat_id), on)
        self.bot.send(chat_id, t["voice_on"] if on else t["voice_off"])

    def _maybe_voice(self, chat_id: int, reply: str, lang: str, voice_in: bool, account: Any = None) -> None:
        """Note vocale EN PLUS du texte déjà envoyé. Toute erreur : rien de plus (le texte est parti)."""
        if self.voice is None or not self.voice.wanted(str(chat_id), voice_in):
            return
        send_voice = getattr(self.bot, "send_voice", None)
        if send_voice is None:
            return
        if self.terms_outdated is not None:
            acc_id = getattr(account, "account_id", None)
            try:
                blocked = acc_id is not None and self.terms_outdated(acc_id)  # pas de compte : interne, non bloqué
            except Exception:  # noqa: BLE001 — état des conditions illisible : pas d'envoi au fournisseur
                logger.warning("conditions du compte illisibles (chat %s) : texte seul", chat_id, exc_info=True)
                blocked = True
            if blocked:
                return
        scope = None
        if self.speak_scope is not None and account is not None:
            try:
                scope = self.speak_scope(account)
            except Exception:  # noqa: BLE001 — pas de foyer lisible : voix sans lexique
                logger.warning("foyer du lexique illisible (chat %s)", chat_id, exc_info=True)
        audio = self.voice.render(reply, lang) if scope is None else self.voice.render(reply, lang, scope=scope)
        if not audio:
            return
        try:
            send_voice(chat_id, audio)
        except Exception:  # noqa: BLE001 — la note vocale est un confort, le texte est déjà livré
            logger.warning("sendVoice en échec (chat %s), texte seul", chat_id, exc_info=True)

    def _pair(self, chat_id: int, tg_id: str, code: str, t: dict[str, str]) -> None:
        if not code:
            existing = self.tenancy.account_for_identity("telegram", tg_id)
            self.bot.send(chat_id, t["paired"] if existing else t["unpaired"])
            return
        account_id = self.pairing.redeem(code)
        if account_id is None:
            self.bot.send(chat_id, t["bad_code"])
            return
        try:
            self.tenancy.bind_identity(account_id, "telegram", tg_id)
        except IdentityTaken:
            self.bot.send(chat_id, t["taken"])
            return
        self.bot.send(chat_id, t["paired"])


__all__ = ["BotApi", "TEXTS", "TelegramAdapter", "split_message"]
