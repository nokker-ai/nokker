"""Garde de fin de tour pour les ACTIONS : une réponse qui affirme avoir fait quelque chose (« j'ai allumé la lampe »,
« c'est envoyé », « done », « ich habe es eingeschaltet », « ho acceso la luce ») alors qu'aucun outil à effet n'a abouti dans
le tour est remplacée (réponse courte) ou complétée (un peu plus longue) par un texte honnête qui dit pourquoi rien n'est
fait. Même schéma que `note_claims` (qui traite « noté » et la mémoire).

Ce qui compte comme « abouti » (`tool_outcomes.read_outcomes`) : un outil NON lecture (effet déclaré de la spec : écriture,
envoi, destruction, dépense…), sans refus de porte (`Refusé : …`), sans carte en attente (`EN ATTENTE D'APPROBATION
HUMAINE…` : une carte posée n'est PAS faite), sans échec d'outil. Les lectures ne comptent pas. Si au moins un outil à
effet a abouti, la garde se tait (réponse peut-être vraie, même pour une partie seulement).

Prudence (mieux vaut ne pas corriger que corriger à tort) : seules les affirmations à la 1re personne (passé composé,
« je viens de », « c'est fait/envoyé… », « done », « fait ! » seul) des DEUX premières phrases comptent ; négations
(« pas encore envoyé », « je n'ai pas allumé »), questions, conditionnel/hypothèse (« si j'ai envoyé »), repères du passé
(« hier j'ai envoyé »), citations, code, et propositions (« je peux l'allumer ») ne déclenchent rien. Une réponse de plus de
`MAX_REPLY` caractères, avec bloc de code ou liste, est un texte rédigé (« j'ai ajouté une section… ») : jamais touchée.
Drapeau d'hôte `local.action_claims_guard` (éteint par défaut : `Kernel.action_claims_guard`)."""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from nokker.core import person_texts as ptx
from nokker.core.note_claims import _NEG as _NEG_BASE
from nokker.core.note_claims import SHORT_REPLY
from nokker.core.tool_outcomes import read_outcomes
from nokker.core.turn import Message

MAX_REPLY = 400
# Négation de la reconnaissance « noté » + contractions anglaises (« haven't », « can't ») et « cannot », « unable ».
_NEG = re.compile(_NEG_BASE.pattern + r"|\w+n't\b|\b(?:cannot|unable|nothing|nichts|nulla|ohne|sans|without)\b", re.IGNORECASE)
FIRST_SENTENCES = 2


def _pp(stems: str) -> str:
    return rf"(?:{stems})(?:e|s|es)?"


_FR_PP = _pp("allumé|éteint|envoyé|lancé|supprimé|créé|ajouté|déplacé|programmé|réservé|effacé|activé|désactivé|fermé|"
             "publié|planifié|annulé|modifié|démarré|commandé|payé|verrouillé|déverrouillé")
_FR_INF = ("allumer|éteindre|envoyer|lancer|supprimer|créer|ajouter|déplacer|programmer|réserver|effacer|activer|"
           "désactiver|fermer|publier|planifier|annuler|modifier|démarrer")
_EN_PP = ("sent|launched|started|deleted|removed|created|added|moved|scheduled|booked|cancelled|canceled|published|"
          "activated|deactivated|locked|unlocked|ordered|paid")
_DE_PP = ("eingeschaltet|ausgeschaltet|angeschaltet|gesendet|geschickt|verschickt|abgeschickt|gestartet|gelöscht|erstellt|"
          "angelegt|hinzugefügt|verschoben|geplant|gebucht|reserviert|abgesagt|storniert|veröffentlicht|aktiviert|"
          "deaktiviert|gesperrt|entsperrt|bestellt|bezahlt|erledigt")
_IT_PP = _pp("acceso|spento|inviato|mandato|lanciato|avviato|eliminato|cancellato|creato|aggiunto|spostato|programmato|"
             "prenotato|pubblicato|attivato|disattivato|chiuso|bloccato|sbloccato|ordinato|pagato|annullato|modificato")

_CLAIM = re.compile(
    # fr
    rf"\b(?:j'ai|je\s+(?:l'|les\s+|le\s+|la\s+)ai)\s+(?:bien\s+|déjà\s+|maintenant\s+|donc\s+)?{_FR_PP}\b|"
    rf"\bje\s+viens\s+(?:de|d')\s*(?:{_FR_INF})\b|\bc'est\s+(?:fait|{_FR_PP})\b|"
    # en
    rf"\bi(?:'ve|\s+have)?\s+(?:just\s+|already\s+|now\s+|successfully\s+)?"
    rf"(?:(?:turned|switched)\s+(?:\w+\s+){{0,3}}?(?:on|off)|(?:{_EN_PP}))\b|"
    rf"\b(?:it's|it\s+is|that's|that\s+is)\s+(?:all\s+)?done\b|\b(?:it\s+)?(?:has|have)\s+been\s+(?:{_EN_PP})\b|"
    # de
    rf"\bich\s+(?:habe|hab)\s+(?:\w+\s+){{0,5}}?(?:{_DE_PP})\b|\b(?:ist|wurde)\s+(?:bereits\s+|jetzt\s+)?erledigt\b|"
    rf"\bwurde\s+(?:bereits\s+|jetzt\s+)?(?:{_DE_PP})\b|"
    # it
    rf"\b(?:l'ho|li\s+ho|le\s+ho|ho)\s+(?:già\s+|appena\s+)?{_IT_PP}\b|\b(?:è|e')\s+fatto\b|\bho\s+fatto\b",
    re.IGNORECASE)
# Réponse réduite à l'accusé lui-même (« Fait ! », « Done. », « Envoyé. », « Erledigt. », « Fatto ! »).
_BARE = re.compile(rf"^(?:voilà[,\s]*|ok[,\s]*|alright[,\s]*|so[,\s]*|ecco[,\s]*)?(?:fait|terminé|{_FR_PP}|done|all\s+done|"
                   rf"erledigt|fertig|fatto)[\s!.]*$", re.IGNORECASE)
# Avant l'affirmation, dans la même phrase : hypothèse ou passé d'un autre moment.
_NOT_NOW = re.compile(r"\b(?:si|if|wenn|falls|quand|when|se|hier|yesterday|gestern|ieri|avant|before|earlier|previously|"
                      r"précédemment|dernier|dernière|last|letzte|letzten|scorso|scorsa)\b", re.IGNORECASE)
_FENCE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_QUOTED = re.compile(r"`[^`]*`|«[^»]*»|\"[^\"]*\"|“[^”]*”")
_LIST = re.compile(r"(?m)^\s*(?:[-*•|]|\d+[.)])\s")


def _sentences(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]


def claims_done(text: str) -> bool:
    """Affirmation d'une action accomplie par le modèle dans les premières phrases (voir l'en-tête du module)."""
    text = _QUOTED.sub(" ", _FENCE.sub(" ", (text or "").replace("’", "'")))
    for sentence in _sentences(text)[:FIRST_SENTENCES]:
        if sentence.endswith("?"):
            continue
        if _BARE.match(sentence):
            return True
        for m in _CLAIM.finditer(sentence):
            if not (_NEG.search(sentence[:m.end()]) or _NOT_NOW.search(sentence[:m.start()])):
                return True
    return False


def is_drafted_text(reply: str) -> bool:
    """Texte rédigé (long, code, liste, tableau) : « j'ai ajouté une section » décrit le texte, pas une action d'outil."""
    return len(reply.strip()) > MAX_REPLY or "```" in reply or bool(_LIST.search(reply))


def guard(messages: list[Message], reply: str, lang: str | None, spec_of: Callable[[str], Any] | None = None,
          failed_ids: Iterable[str] = (), continuation: bool = False) -> str:
    """`reply` corrigée si elle affirme une action faite sans qu'aucun outil à effet n'ait abouti ; sinon inchangée. `continuation` : tour de suite d'une carte décidée (l'action a tourné hors tour) : jamais touché."""
    if continuation or not reply or is_drafted_text(reply) or not claims_done(reply):
        return reply
    outcomes = read_outcomes(messages, spec_of, failed_ids)
    if outcomes.done:
        return reply
    fixed = ptx.pick(ptx.ACTION_NOT_DONE, lang).format(why=ptx.pick(ptx.ACTION_WHY[outcomes.reason()], lang))
    return fixed if len(reply.strip()) <= SHORT_REPLY else f"{reply.rstrip()}\n\n{fixed}"
