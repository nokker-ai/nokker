"""`Escalator` v0 (`core.ports.Escalator`) — PLAN-P1 §2.3/§5.1 (F2). Renvoie le texte qui remplace la
réponse et journalise l'escalade (canal d'origine + motif). La carte d'approbation réelle sur le canal
(Telegram/Slack/mail) est le chantier B-3 ; v0 ne fait que le texte + le journal, pas l'envoi."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from nokker.core import person_texts as ptx
from nokker.core.person_texts import CANNOT_CONTINUE, CANNOT_CONTINUE_BARE, pick
from nokker.core.turn import Turn

logger = logging.getLogger("nokker.qa.escalate")


def cannot_continue_text(why: str, lang: str | None = None) -> str:
    """Phrase d'une panne qui n'attend personne (≠ une escalade à un humain) ; commune à tous les escaladeurs.
    `lang` : langue du tour (sinon celle du contexte, sinon fr)."""
    if why:
        return pick(CANNOT_CONTINUE, lang).format(why=why)
    return pick(CANNOT_CONTINUE_BARE, lang)


@dataclass
class EscalatorV0:
    addressee: str = "l'équipe"

    def escalate(self, turn: Turn, reason: str) -> str:
        logger.info("escalade: fil=%s tour=%d motif=%s", turn.thread_id, turn.index, reason)
        lang = getattr(turn, "lang", None) or None  # langue du tour (sinon celle du contexte, sinon fr)
        if self.addressee == ptx.DEFAULT_ADDRESSEE:  # destinataire par défaut : traduit ; un nom propre reste tel quel
            return (pick(ptx.TRANSMIT_TEAM, lang).format(reason=reason) if reason
                    else pick(ptx.TRANSMIT_TEAM_BARE, lang))
        if reason:
            return pick(ptx.TRANSMIT, lang).format(addressee=self.addressee, reason=reason)
        return pick(ptx.TRANSMIT_BARE, lang).format(addressee=self.addressee)

    def cannot_continue(self, turn: Turn, why: str) -> str:
        """Panne qui se règle seule (palier indisponible, boucle non terminée) : rien n'est transmis à personne."""
        logger.info("arrêt: fil=%s tour=%d motif=%s", turn.thread_id, turn.index, why)
        return cannot_continue_text(why, getattr(turn, "lang", None) or None)


@dataclass
class LocalEscalator:
    """Nokker Lokkal : l'interlocuteur est le propriétaire de la machine, il n'y a personne d'autre à qui « transmettre »
    (vu sur la CLI le 23.10 : « Je transmets à … » dans une session locale)."""

    def escalate(self, turn: Turn, reason: str) -> str:
        logger.info("escalade (local) : fil=%s tour=%d motif=%s", turn.thread_id, turn.index, reason)
        lang = getattr(turn, "lang", None) or None
        return pick(ptx.NEED_YOU, lang).format(reason=reason) if reason else pick(ptx.NEED_YOU_BARE, lang)

    def cannot_continue(self, turn: Turn, why: str) -> str:
        logger.info("arrêt (local) : fil=%s tour=%d motif=%s", turn.thread_id, turn.index, why)
        return cannot_continue_text(why, getattr(turn, "lang", None) or None)


__all__ = ["EscalatorV0", "LocalEscalator", "cannot_continue_text"]
