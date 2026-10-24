"""`Escalator` v0 (`core.ports.Escalator`) — PLAN-P1 §2.3/§5.1 (F2). Renvoie le texte qui remplace la
réponse et journalise l'escalade (canal d'origine + motif). La carte d'approbation réelle sur le canal
(Telegram/Slack/mail) est le chantier B-3 ; v0 ne fait que le texte + le journal, pas l'envoi."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from nokker.core.turn import Turn

logger = logging.getLogger("nokker.qa.escalate")


@dataclass
class EscalatorV0:
    addressee: str = "l'équipe"

    def escalate(self, turn: Turn, reason: str) -> str:
        logger.info("escalade: fil=%s tour=%d motif=%s", turn.thread_id, turn.index, reason)
        if reason:
            return f"Je transmets à {self.addressee} : {reason}."
        return f"Je transmets à {self.addressee}."


@dataclass
class LocalEscalator:
    """Nokker Lokkal : l'interlocuteur est le propriétaire de la machine, il n'y a personne d'autre à qui « transmettre »
    (vu sur la CLI le 23.10 : « Je transmets à … » dans une session locale)."""

    def escalate(self, turn: Turn, reason: str) -> str:
        logger.info("escalade (local) : fil=%s tour=%d motif=%s", turn.thread_id, turn.index, reason)
        return f"J'ai besoin de toi pour continuer : {reason}." if reason else "J'ai besoin de toi pour continuer."


__all__ = ["EscalatorV0", "LocalEscalator"]
