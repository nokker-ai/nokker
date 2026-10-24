"""Carte d'approbation d'un testeur → son Telegram relié (bot de la bêta). Le message nomme l'outil et le motif, et
renvoie vers l'app web pour décider : la preuve (passkey de l'appareil) se donne là, jamais dans Telegram."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from nokker.core.ports import Approval
from nokker.tenancy.context import tenant_of_thread
from nokker.tenancy.store import Tenancy

logger = logging.getLogger("nokker.telegram.notify")


@dataclass
class TesterApprovalNotifier:
    tenancy: Tenancy
    bot: Any  # BotApi : send(chat_id, text) ; send_with_button(chat_id, text, label, url) si présent
    app_url: str  # base de l'app web, ex. https://app.nokker.ai

    def __call__(self, approval: Approval) -> None:
        account = self.tenancy.account_for_tenant(tenant_of_thread(approval.thread_id))
        if account is None:
            return
        chats = self.tenancy.identities_of(account.account_id, "telegram")
        if not chats:
            return
        fr = account.lang != "en"
        text = (f"{'Action à approuver' if fr else 'Action awaiting your approval'} : {approval.tool_name}\n"
                f"{approval.reason}")
        url = f"{self.app_url.rstrip('/')}/approvals/{approval.id}"
        for chat in chats:
            send = getattr(self.bot, "send_with_button", None)
            if callable(send):
                send(chat, text, "Ouvrir" if fr else "Open", url)
            else:
                self.bot.send(chat, f"{text}\n{url}")


__all__ = ["TesterApprovalNotifier"]
