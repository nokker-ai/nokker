"""Une machine liée prévient nokker.ai, SANS contenu : `POST /v1/gateway/notify` (docs/LOKKAL-PHONE.md §1.2 point 5).

Corps : `{kind: "turn_done"|"awaiting_approval"|"agent_failed"|"agent_done", thread_id, title|null}`, avec le jeton de
la machine, 30 par minute et par machine. `title` ne passe que sous la règle du miroir (`titles_allowed`, comme le
deck) ; sinon il est ignoré, même envoyé.

nokker.ai prévient le propriétaire de la machine :
- par une bulle `notify` sans fil (`GET /v1/notifications`, flux `/v1/events`) dont l'état porte
  `{kind, machine_id, machine_name, thread_id, title, at, link}` (contrat C4), lien `/t/<mc_…>/<fil>` ;
- sur son Telegram appairé (`sendMessage` seulement, avec un bouton vers le lien) ;
- plus tard, par le push de l'app (§4.2.3, lot P4), au même endroit.

Le texte est « atelier : ta session « site » a fini ». Ni extrait, ni message, ni compte rendu : nokker.ai ne détient
pas le contenu d'une session Lokkal.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from nokker.approvals.rule_labels import norm_lang

logger = logging.getLogger("nokker.cloud.notify")

KINDS = ("turn_done", "awaiting_approval", "agent_failed", "agent_done")
RATE_MAX = 30  # par minute et par machine
_THREAD = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
TITLE_MAX = 80

_TEXTS = {
    "fr": {"turn_done": ("{m} : ta session « {t} » a fini", "{m} : ta session a fini"),
           "awaiting_approval": ("{m} : ta session « {t} » attend ton accord", "{m} : ta session attend ton accord"),
           "agent_failed": ("{m} : ton agent « {t} » a échoué", "{m} : un agent a échoué"),
           "agent_done": ("{m} : ton agent « {t} » a fini", "{m} : un agent a fini"),
           "open": "Ouvrir"},
    "en": {"turn_done": ("{m}: your session “{t}” is done", "{m}: your session is done"),
           "awaiting_approval": ("{m}: your session “{t}” is waiting for your OK",
                                 "{m}: your session is waiting for your OK"),
           "agent_failed": ("{m}: your agent “{t}” failed", "{m}: an agent failed"),
           "agent_done": ("{m}: your agent “{t}” is done", "{m}: an agent is done"),
           "open": "Open"},
    "de": {"turn_done": ("{m}: Deine Sitzung «{t}» ist fertig", "{m}: Deine Sitzung ist fertig"),
           "awaiting_approval": ("{m}: Deine Sitzung «{t}» wartet auf dein OK", "{m}: Deine Sitzung wartet auf dein OK"),
           "agent_failed": ("{m}: Dein Agent «{t}» ist fehlgeschlagen", "{m}: Ein Agent ist fehlgeschlagen"),
           "agent_done": ("{m}: Dein Agent «{t}» ist fertig", "{m}: Ein Agent ist fertig"),
           "open": "Öffnen"},
    "it": {"turn_done": ("{m}: la tua sessione «{t}» ha finito", "{m}: la tua sessione ha finito"),
           "awaiting_approval": ("{m}: la tua sessione «{t}» attende il tuo ok", "{m}: la tua sessione attende il tuo ok"),
           "agent_failed": ("{m}: il tuo agente «{t}» ha avuto un errore", "{m}: un agente ha avuto un errore"),
           "agent_done": ("{m}: il tuo agente «{t}» ha finito", "{m}: un agente ha finito"),
           "open": "Apri"},
}


def _texts(lang: str | None) -> dict[str, Any]:
    """Textes dans la langue du compte (fr/en/de/it) ; une langue inconnue ou absente : `fr`, comme avant."""
    return _TEXTS[norm_lang(lang) or "fr"]


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


class NotifyError(ValueError):
    """Corps refusé (400) ; `code` : bad_kind | bad_thread | bad_request (contrat C4)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _clean_text(v: Any, n: int) -> str:
    """Une ligne, sans caractère de contrôle, `n` caractères au plus."""
    s = "".join(ch if ch.isprintable() else " " for ch in str(v or ""))
    return " ".join(s.split())[:n]


def clean_notify(raw: Any, *, titles: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise NotifyError("bad_request", "corps JSON attendu")
    kind, tid = raw.get("kind"), str(raw.get("thread_id") or "")
    if kind not in KINDS:
        raise NotifyError("bad_kind", f"kind : {' | '.join(KINDS)}")
    if not _THREAD.match(tid):
        raise NotifyError("bad_thread", "thread_id invalide")
    title = _clean_text(raw.get("title"), TITLE_MAX) if titles and isinstance(raw.get("title"), str) else ""
    return {"kind": kind, "thread_id": tid, "title": title or None}


def text_for(kind: str, machine_name: str, title: str | None, lang: str = "fr") -> str:
    t = _texts(lang)[kind]
    m = _clean_text(machine_name, 60) or "Lokkal"
    return t[0].format(m=m, t=title) if title else t[1].format(m=m)


@dataclass
class MachineNotifier:
    """Prévient le propriétaire de `machine` : bulle `notify` (hub de nokker.ai), puis Telegram appairé (bot posé par
    `configure_telegram`, absent sinon). Rend les voies utilisées."""

    hub: Any
    tenancy: Any
    app_url: str = "https://app.nokker.ai"
    bot: Any = None  # BotApi : send_with_button(chat_id, text, label, url), sinon send(chat_id, text)

    def link(self, machine_id: str, thread_id: str) -> str:
        return f"{self.app_url.rstrip('/')}/t/{machine_id}/{thread_id}"

    def __call__(self, machine: Any, event: dict[str, Any]) -> list[str]:
        from nokker.cloud.mirror import mirror_thread
        from nokker.tenancy.context import tenant_scope

        account = self.tenancy.account(machine.account) if self.tenancy is not None else None
        lang = getattr(account, "lang", None) or "fr"
        text = text_for(event["kind"], machine.name, event.get("title"), lang)
        url = self.link(machine.id, event["thread_id"])
        used: list[str] = []
        if self.hub is not None:
            try:
                with tenant_scope(machine.tenant):
                    # `from_thread` : l'id de la copie du miroir de ce fil (`lokkal-<mc>-<fil>`, locataire du compte) ;
                    # il rattache la bulle sans fil au bon locataire (`bubble_tenant`), même sans copie gardée.
                    self.hub.notify_user(machine.account, title=text, text=text,
                                         from_thread=mirror_thread(machine.tenant, machine.id, event["thread_id"]),
                                         extra={"kind": event["kind"], "machine_id": machine.id,
                                                "machine_name": machine.name, "thread_id": event["thread_id"],
                                                "title": event.get("title"), "at": _now_iso(), "link": url})
                used.append("app")
            except Exception:  # noqa: BLE001 — une voie en panne n'empêche pas les autres
                logger.warning("notification de %s : bulle non créée", machine.id, exc_info=True)
        chats = self.tenancy.identities_of(machine.account, "telegram") if self.bot is not None else []
        for chat in chats:
            try:
                send = getattr(self.bot, "send_with_button", None)
                if callable(send):
                    send(chat, text, _texts(lang)["open"], url)
                else:
                    self.bot.send(chat, f"{text}\n{url}")
                if "telegram" not in used:
                    used.append("telegram")
            except Exception:  # noqa: BLE001
                logger.warning("notification de %s : Telegram en échec", machine.id, exc_info=True)
        return used


__all__ = ["KINDS", "RATE_MAX", "MachineNotifier", "NotifyError", "clean_notify", "text_for"]
