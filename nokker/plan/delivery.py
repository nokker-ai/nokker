"""Livraison du brief du matin hors de l'app (docs/LOKKAL.md §7.18.5) : Telegram et/ou e-mail, au choix de chaque
personne, désactivable, éteinte par défaut.

- **Telegram** : le bot existant, par le chat relié au compte (`/start <code>`), avec `sendMessage` SEULEMENT (jamais
  `getUpdates`, jamais `setWebhook` : le webhook du bot reste la seule lecture).
- **E-mail** : par le connecteur mail de la personne (sa boîte, en son nom). La première fois, une carte FORTE arme la
  livraison : elle signe (personne, adresse de destination, boîte d'envoi). Ensuite CE brief part chaque matin vers
  CETTE adresse seulement, sans nouvelle carte ; changer d'adresse ou de boîte désarme et redemande la carte. Le
  modèle n'a aucun moyen d'en profiter : ce n'est pas l'outil `mail_send`, c'est le noyau qui envoie le texte du brief.
- **Contenu** : le texte du brief déjà passé par le caviardage final des tours (`scrub_output`), puis sans aucun lien
  qui contourne l'authentification (capacité `/_s/…`, `/_auth`, paramètres `token=`, `code=`, `sig=`…) ; le seul lien
  ajouté est l'adresse de l'app, qui demande une connexion.
- **Échec** d'un transport : noté dans `last` (jamais le contenu), le brief reste rangé dans l'app.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
from nokker.plan.types import PlanError, iso
from nokker.tenancy.context import INTERNAL_TENANT, current_tenant_or_none, scoped_thread_id

logger = logging.getLogger("nokker.plan.delivery")

DELIVERY_TOOL = "brief_delivery_arm"  # carte forte d'armement de l'e-mail : un geste du noyau, pas un outil du registre
_ADDR = re.compile(r"^[^@\s,;<>\"']{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}$")
_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_CAPABILITY = re.compile(
    r"(/_s/|/_auth\b|[?&#](?:token|access_token|refresh_token|code|key|api_key|sig|signature|secret|cap|session|"
    r"auth|jwt)=)", re.IGNORECASE)
REMOVED = "[lien retiré]"


def strip_capability_links(text: str) -> str:
    """Retire tout lien qui ouvrirait quelque chose sans connexion (capacité, jeton dans l'URL)."""
    return _URL.sub(lambda m: REMOVED if _CAPABILITY.search(m.group(0)) else m.group(0), text or "")


class BriefTransports(Protocol):
    def telegram_chats(self, owner: str) -> list[str]: ...
    def send_telegram(self, chat_id: str, text: str) -> None: ...
    def mail_sender(self, owner: str) -> str | None: ...  # adresse de la boîte reliée, ou None
    def send_mail(self, owner: str, to: str, subject: str, body: str) -> None: ...


@dataclass
class RealBriefTransports:
    """Le bot Telegram de la bêta (`BotApi.send` : `sendMessage`) et le connecteur mail du compte."""

    tenancy: Any  # identities_of(account, "telegram")
    bot: Any = None  # nokker.telegram.adapter.BotApi ; None = pas de Telegram sur cette instance
    connectors: Any = None  # ConnectorStore ; None = pas d'e-mail
    resolve: Callable[[str], str] | None = None  # `secret:NOM` → valeur (Lokkal) ; None = secret littéral

    def telegram_chats(self, owner: str) -> list[str]:
        if self.bot is None or self.tenancy is None:
            return []
        return [str(c) for c in self.tenancy.identities_of(owner, "telegram")]

    def send_telegram(self, chat_id: str, text: str) -> None:
        self.bot.send(chat_id, text)  # sendMessage, découpé à 4000 caractères

    def mail_sender(self, owner: str) -> str | None:
        cred = self.connectors.get(owner, "mail") if self.connectors is not None else None
        return str(cred.settings.get("address") or "") or None if cred is not None else None

    def send_mail(self, owner: str, to: str, subject: str, body: str) -> None:
        from nokker.connectors.mail import MailConnector

        cred = self.connectors.get(owner, "mail") if self.connectors is not None else None
        if cred is None:
            raise RuntimeError("connecteur mail absent")
        secret = self.resolve(cred.secret) if self.resolve is not None else cred.secret
        MailConnector(cred.settings, secret)._t_mail_send({"to": to, "subject": subject, "body": body})


def fingerprint(tenant: str, owner: str, to: str, sender: str) -> str:
    return hashlib.sha256(f"{tenant}\x00{owner}\x00{to.lower()}\x00{sender.lower()}".encode()).hexdigest()


def _blank() -> dict[str, Any]:
    return {"telegram": False, "email": None, "email_armed": None, "arm_approval_id": None, "armed_by": None,
            "armed_at": None, "last": None}


@dataclass
class BriefDelivery:
    store: Any  # BasePlan : get_delivery(owner), put_delivery(owner, d)
    transports: Any  # BriefTransports
    approvals: Any = None  # carte forte d'armement de l'e-mail ; None = e-mail indisponible
    scrub: Callable[[str], str] | None = None  # caviardage final des tours (kernel.invariants.scrub_output)
    app_url: str = ""
    clock: Callable[[], float] = field(default=time.time)

    # ── état ──
    def _load(self, owner: str) -> dict[str, Any]:
        return {**_blank(), **(self.store.get_delivery(owner) or {})}

    def _tenant(self) -> str:
        return current_tenant_or_none() or INTERNAL_TENANT

    def _card_pending(self, approval_id: str | None) -> bool:
        card = self.approvals.get(approval_id) if approval_id and self.approvals is not None else None
        return card is not None and card.status == "pending"

    def _email_state(self, owner: str, d: dict[str, Any]) -> str:
        if not d.get("email"):
            return "off"
        sender = self.transports.mail_sender(owner)
        if sender and d.get("email_armed") == fingerprint(self._tenant(), owner, d["email"], sender):
            return "armed"
        return "pending" if self._card_pending(d.get("arm_approval_id")) else "not_armed"

    def _view(self, owner: str, d: dict[str, Any]) -> dict[str, Any]:
        state = self._email_state(owner, d)
        return {"telegram": {"enabled": bool(d.get("telegram")), "chats": len(self.transports.telegram_chats(owner))},
                "email": {"address": d.get("email"), "from": self.transports.mail_sender(owner), "state": state,
                          "approval_id": d.get("arm_approval_id") if state == "pending" else None},
                "last": d.get("last")}

    def get(self, owner: str) -> dict[str, Any]:
        return self._view(owner, self._load(owner))

    # ── choix de la personne ──
    def put(self, owner: str, body: dict[str, Any]) -> dict[str, Any]:
        """`{telegram?: bool, email?: adresse | null}`. Une adresse nouvelle (ou pas encore armée, sans carte en
        attente) pose une carte FORTE ; tant qu'elle n'est pas signée, aucun e-mail ne part."""
        unknown = set(body) - {"telegram", "email"}
        if unknown:
            raise PlanError(f"livraison : clé inconnue {min(unknown)} (telegram, email)", code="invalid")
        d = self._load(owner)
        if "telegram" in body:
            if not isinstance(body["telegram"], bool):
                raise PlanError("telegram : true | false", code="invalid")
            if body["telegram"] and not self.transports.telegram_chats(owner):
                raise PlanError("aucun Telegram relié à ce compte : relie-le d'abord (/start <code>)", status=409,
                                code="no_telegram")
            d["telegram"] = body["telegram"]
        if "email" in body:
            self._set_email(owner, d, body["email"])
        self.store.put_delivery(owner, {**d, "updated_at": self.clock()})
        return self._view(owner, d)

    def _set_email(self, owner: str, d: dict[str, Any], raw: Any) -> None:
        if raw is None or raw == "":
            self._drop_card(d, "livraison du brief par e-mail coupée")
            d.update(email=None, email_armed=None, armed_by=None, armed_at=None)
            return
        to = str(raw).strip()
        if not _ADDR.match(to):
            raise PlanError("email : une adresse e-mail", code="invalid")
        sender = self.transports.mail_sender(owner)
        if not sender:
            raise PlanError("aucune boîte mail reliée à ce compte : relie le connecteur mail d'abord", status=409,
                            code="no_mail_connector")
        if self.approvals is None:
            raise PlanError("approbations fortes absentes sur cette instance : pas de livraison par e-mail",
                            status=503, code="approvals_unavailable")
        same = (d.get("email") or "").lower() == to.lower()
        fp = fingerprint(self._tenant(), owner, to, sender)
        if same and (d.get("email_armed") == fp or self._card_pending(d.get("arm_approval_id"))):
            return  # déjà armée pour cette adresse et cette boîte, ou carte déjà en attente
        self._drop_card(d, "adresse de livraison du brief changée")
        d.update(email=to, email_armed=None, armed_by=None, armed_at=None)
        d["arm_approval_id"] = self._request_card(owner, to, sender, fp)

    def _drop_card(self, d: dict[str, Any], why: str) -> None:
        if self._card_pending(d.get("arm_approval_id")):
            try:
                self.approvals.mark_failed(d["arm_approval_id"], why)
            except Exception:  # noqa: BLE001 — une carte déjà décidée entre-temps : rien à retirer
                logger.warning("carte de livraison %s non retirée", d.get("arm_approval_id"), exc_info=True)
        d["arm_approval_id"] = None

    def _request_card(self, owner: str, to: str, sender: str, fp: str) -> str:
        turn = Turn(thread_id=scoped_thread_id(self._tenant(), f"brief-delivery-{owner}"), index=0,
                    input=InputCheck(text=""), channel="kernel", sender=owner)
        call = ToolCall(id=f"brief-mail-{fp[:12]}", name=DELIVERY_TOOL,
                        arguments={"owner": owner, "to": to, "from": sender, "fingerprint": fp})
        verdict = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="brief:mail_delivery",
                          reason=(f"envoyer chaque matin le brief du matin à {to}, depuis ta boîte {sender} "
                                  "(ce brief seulement, vers cette adresse seulement)"),
                          subject=call.id, strength="strong")
        return self.approvals.request(turn, call, verdict, None).id

    def on_arm_decision(self, approval: Any) -> str:
        """Décision de la carte forte (exécuteur d'approbation, locataire du fil de la carte)."""
        args = approval.arguments or {}
        owner, to = str(args.get("owner") or ""), str(args.get("to") or "")
        d = self._load(owner)
        if d.get("arm_approval_id") == approval.id:
            d["arm_approval_id"] = None
        if approval.status != "approved":
            self.store.put_delivery(owner, {**d, "updated_at": self.clock()})
            return "livraison du brief par e-mail refusée"
        sender = self.transports.mail_sender(owner) or ""
        fp = fingerprint(self._tenant(), owner, to, sender)
        if (d.get("email") or "").lower() != to.lower() or fp != args.get("fingerprint"):
            self.store.put_delivery(owner, {**d, "updated_at": self.clock()})
            raise PlanError("adresse ou boîte d'envoi changée depuis la demande : rien n'est armé", status=409,
                            code="stale")
        d.update(email_armed=fp, armed_by=approval.decided_by, armed_at=self.clock())
        self.store.put_delivery(owner, {**d, "updated_at": self.clock()})
        return f"brief du matin livré chaque matin à {to}"

    # ── livraison (fin d'un passage réussi du brief) ──
    def deliver(self, owner: str, title: str, text: str) -> dict[str, str] | None:
        d = self._load(owner)
        if not d.get("telegram") and not d.get("email"):
            return None
        body = strip_capability_links(self.scrub(text) if self.scrub is not None else text).strip()
        link = f"\n\nNokker : {self.app_url.rstrip('/')}" if self.app_url else ""
        out = {"telegram": "off", "email": "off"}
        if d.get("telegram"):
            chats = self.transports.telegram_chats(owner)
            out["telegram"] = "no_chat" if not chats else "sent"
            for chat in chats:
                try:
                    self.transports.send_telegram(chat, f"{title}\n\n{body}{link}")
                except Exception:  # noqa: BLE001 — un transport en panne ne casse jamais le brief
                    logger.warning("brief : envoi Telegram en échec", exc_info=True)
                    out["telegram"] = "failed"
        if d.get("email"):
            if self._email_state(owner, d) != "armed":
                out["email"] = "not_armed"
            else:
                try:
                    self.transports.send_mail(owner, d["email"], title, f"{body}{link}\n")
                    out["email"] = "sent"
                except Exception:  # noqa: BLE001
                    logger.warning("brief : envoi par e-mail en échec", exc_info=True)
                    out["email"] = "failed"
        d["last"] = {"at": iso(self.clock()), **out}
        self.store.put_delivery(owner, {**d, "updated_at": self.clock()})
        return out


__all__ = ["DELIVERY_TOOL", "BriefDelivery", "BriefTransports", "RealBriefTransports", "fingerprint",
           "strip_capability_links"]
