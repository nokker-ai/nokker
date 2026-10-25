"""Notificateur Telegram des cartes d'approbation (docs/APPROVALS.md §2 : « le notificateur pousse la
carte »). Interface + deux implémentations :

  - `NullNotifier` : défaut, ne fait rien — utilisée dans TOUS les tests (jamais de vrai appel réseau) et
    tant que `config.approvals_telegram_enabled` est False (défaut, cf `nokker/config.py`) ;
  - `TelegramNotifier` : Bot API `sendMessage` avec un bouton `web_app` (lien `startapp=<approval_id>`
    vers la Mini App) — n'appelle le réseau que si `send()` est réellement invoqué par un appelant qui
    l'a construit explicitement (jamais depuis les tests unitaires de ce chantier).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol
from urllib import request as urllib_request

from nokker.core.ports import Approval

logger = logging.getLogger("nokker.approvals.notify")


class ApprovalNotifier(Protocol):
    def notify(self, approval: Approval) -> None: ...


@dataclass
class NullNotifier:
    """Défaut — n'envoie rien. `sent` : liste des approbations « notifiées » (pour les tests qui veulent
    vérifier QUE `request()` a bien tenté de notifier, sans jamais toucher au réseau)."""

    sent: list[Approval] | None = None

    def __post_init__(self) -> None:
        if self.sent is None:
            self.sent = []

    def notify(self, approval: Approval) -> None:
        self.sent.append(approval)  # type: ignore[union-attr]


@dataclass
class ChainNotifier:
    """Plusieurs notificateurs à la suite ; l'échec de l'un n'empêche pas les autres (Lokkal : celui de la config, puis
    le dépôt des cartes fortes chez nokker.ai, lot X10)."""

    notifiers: tuple[ApprovalNotifier, ...]

    def notify(self, approval: Approval) -> None:
        for n in self.notifiers:
            try:
                n.notify(approval)
            except Exception:  # noqa: BLE001
                logger.exception("notificateur %s en échec pour l'approbation %s", type(n).__name__, approval.id)


@dataclass
class TelegramNotifier:
    """Bot API `sendMessage`, bouton inline `web_app` (Mini App, `startapp=<approval_id>`). Désactivé
    par défaut (`NokkerConfig.approvals_telegram_enabled = False`) — `nokker/api/server.py` ne construit
    cette classe que si le flag est vrai ET qu'un jeton de bot est présent."""

    bot_token: str
    mini_app_url: str  # ex. "https://t.me/<bot>/app"
    chat_ids: tuple[str, ...]  # destinataires (opérateurs internes — identifiants Telegram numériques)
    timeout_s: float = 10.0

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/{method}"

    def notify(self, approval: Approval) -> None:
        text = (f"Approbation demandée : {approval.tool_name}\n"
                f"Effet : {approval.rule} ({approval.strength})\n"
                f"Raison : {approval.reason}")
        if approval.advice:
            from nokker.qa.review import RISK_LABEL_FR, parse_advice

            risk, advice = parse_advice(approval.advice)  # X8 : `[risk:<niveau>] <texte>`
            text += f"\nAvis de Nina{f' (risque {RISK_LABEL_FR[risk]})' if risk else ''} : {advice}"
        keyboard = {"inline_keyboard": [[{
            "text": "Ouvrir la carte",
            "web_app": {"url": f"{self.mini_app_url}?startapp={approval.id}"},
        }]]}
        for chat_id in self.chat_ids:
            body = json.dumps({"chat_id": chat_id, "text": text, "reply_markup": keyboard}).encode("utf-8")
            req = urllib_request.Request(self._api_url("sendMessage"), data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib_request.urlopen(req, timeout=self.timeout_s) as resp:
                    resp.read()
            except Exception:  # noqa: BLE001 — une notification ratée ne doit jamais faire échouer la demande
                logger.exception("notify telegram: envoi échoué (chat_id=%s, approval=%s)", chat_id, approval.id)


__all__ = ["ApprovalNotifier", "ChainNotifier", "NullNotifier", "TelegramNotifier"]
