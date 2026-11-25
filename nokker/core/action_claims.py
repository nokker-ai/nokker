"""Garde de fin de tour pour les ACTIONS : une réponse qui affirme avoir fait quelque chose (« j'ai allumé la lampe »,
« c'est envoyé », « le mail a été envoyé », « done », « ich habe es eingeschaltet », « ho acceso la luce ») alors que l'outil
qui l'aurait fait n'a pas abouti dans le tour est remplacée (réponse courte) ou complétée (réponse plus longue) par un texte
honnête qui dit pourquoi rien n'est fait. Même schéma que `note_claims` (qui traite « noté » et la mémoire).

Ce qui compte comme « abouti » (`tool_outcomes.call_outcomes`) : un outil NON lecture (effet déclaré de la spec : écriture,
envoi, destruction, dépense…), sans refus de porte (`Refusé : …`), sans carte en attente (`EN ATTENTE D'APPROBATION
HUMAINE…` : une carte posée n'est PAS faite), sans échec d'outil. Les lectures ne comptent pas.

Jugement ACTION PAR ACTION (G3) : chaque affirmation a une espèce (envoi, suppression, dépense, écriture, ou « générique » pour
« c'est fait » / « done »), chaque appel d'outil aussi (effet + nom). Aucun outil à effet abouti : toute affirmation est
fausse (réponse courte remplacée, longue complétée). Un ou plusieurs effets aboutis : un effet réussi ne couvre PAS une autre
action encore en attente, refusée ou en échec (et non rejouée ensuite avec succès) : l'affirmation de CETTE espèce (ou une
affirmation générique, qui les couvre toutes) est complétée par le texte honnête, jamais remplacée (une partie est vraie).

La réponse ENTIÈRE est lue (toutes les phrases, éléments de liste compris, texte long compris), à la 1re personne (passé
composé, « je viens de », « c'est fait/envoyé… », « done », « fait ! » seul en tête ou en fin de réponse), au passif et à la
3e personne (« le mail a été envoyé », « the message was sent », « die Nachricht wurde gesendet », « il messaggio è stato
inviato »). Prudence (mieux vaut ne pas corriger que corriger à tort) : négations (« pas encore envoyé », « je n'ai pas
envoyé », « n'a pas été envoyé »), questions, conditionnel/hypothèse (« si j'ai envoyé »), repères du passé (« hier j'ai
envoyé »), citations, code (blocs et en ligne) et propositions (« je peux l'envoyer ») ne déclenchent rien.
Garde allumée pour TOUS les paliers de la bêta (`tiers.<nom>.action_claims_guard`) et par défaut dans Lokkal
(`local.action_claims_guard`)."""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from nokker.core import person_texts as ptx
from nokker.core.note_claims import _NEG as _NEG_BASE
from nokker.core.note_claims import SHORT_REPLY
from nokker.core.tool_outcomes import CallOutcome, call_outcomes, read_outcomes
from nokker.core.turn import Effect, Message

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

_IT_PP_ANY = (r"(?:acces|spent|inviat|mandat|lanciat|avviat|eliminat|cancellat|creat|aggiunt|spostat|programmat|prenotat|"
              r"pubblicat|attivat|disattivat|chius|bloccat|sbloccat|ordinat|pagat|annullat|modificat)[oaie]")

_CLAIM = re.compile(
    # fr (1re personne, puis passif : « le mail a été envoyé », « vient d'être envoyé »)
    rf"\b(?:j'ai|je\s+(?:l'|les\s+|le\s+|la\s+)ai)\s+(?:bien\s+|déjà\s+|maintenant\s+|donc\s+)?{_FR_PP}\b|"
    rf"\bje\s+viens\s+(?:de|d')\s*(?:{_FR_INF})\b|\bc'est\s+(?:fait|{_FR_PP})\b|"
    rf"\b(?:a|ont)\s+(?:bien\s+|déjà\s+|maintenant\s+|été\s+)*été\s+{_FR_PP}\b|\bvient\s+d'être\s+{_FR_PP}\b|"
    # en
    rf"\bi(?:'ve|\s+have)?\s+(?:just\s+|already\s+|now\s+|successfully\s+)?"
    rf"(?:(?:turned|switched)\s+(?:\w+\s+){{0,3}}?(?:on|off)|(?:{_EN_PP}))\b|"
    rf"\b(?:it's|it\s+is|that's|that\s+is)\s+(?:all\s+)?done\b|"
    rf"\b(?:has|have|had)\s+(?:now\s+|just\s+|already\s+|successfully\s+)*been\s+(?:{_EN_PP})\b|"
    rf"\b(?:was|were)\s+(?:now\s+|just\s+|already\s+|successfully\s+)?(?:{_EN_PP})\b|"
    # de
    rf"\bich\s+(?:habe|hab)\s+(?:\w+\s+){{0,5}}?(?:{_DE_PP})\b|\b(?:ist|wurde)\s+(?:bereits\s+|jetzt\s+)?erledigt\b|"
    rf"\bwurden?\s+(?:bereits\s+|jetzt\s+)?(?:\w+\s+){{0,4}}?(?:{_DE_PP})\b|"
    rf"\b(?:ist|sind)\s+(?:bereits\s+|jetzt\s+)?(?:\w+\s+){{0,4}}?(?:{_DE_PP})\s+worden\b|"
    # it
    rf"\b(?:l'ho|li\s+ho|le\s+ho|ho)\s+(?:già\s+|appena\s+)?{_IT_PP}\b|\b(?:è|e')\s+fatto\b|\bho\s+fatto\b|"
    rf"\b(?:è|e'|sono)\s+stat[oaie]\s+(?:già\s+|appena\s+)?(?:\w+\s+){{0,3}}?{_IT_PP_ANY}\b",
    re.IGNORECASE)
# Réponse réduite à l'accusé lui-même (« Fait ! », « Done. », « Envoyé. », « Erledigt. », « Fatto ! »).
_BARE = re.compile(rf"^(?:voilà[,\s]*|ok[,\s]*|alright[,\s]*|so[,\s]*|ecco[,\s]*)?(?:fait|terminé|{_FR_PP}|done|all\s+done|"
                   rf"erledigt|fertig|fatto)[\s!.]*$", re.IGNORECASE)
# Avant l'affirmation, dans la même phrase : hypothèse ou passé d'un autre moment.
_NOT_NOW = re.compile(r"\b(?:si|if|wenn|falls|quand|when|se|hier|yesterday|gestern|ieri|avant|before|earlier|previously|"
                      r"précédemment|dernier|dernière|last|letzte|letzten|scorso|scorsa)\b", re.IGNORECASE)
# Après le temps de l'affirmation : « … le mail a été envoyé hier » est un récit, pas un accusé.
_PAST_AFTER = re.compile(r"\b(?:hier|yesterday|gestern|ieri|avant|before|earlier|previously|précédemment)\b", re.IGNORECASE)
_FENCE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_QUOTED = re.compile(r"`[^`]*`|«[^»]*»|\"[^\"]*\"|“[^”]*”")
_LIST = re.compile(r"(?m)^\s*(?:[-*•|]|\d+[.)])\s")


# Verbe coordonné à une affirmation : « j'ai envoyé le mail et supprimé l'ancien » (2e action affirmée).
_COORD = re.compile(rf"(?:\b(?:et|puis|and|then|und|dann|e|poi)\b|,)\s+(?:\w+\s+){{0,3}}?"
                    rf"(?:{_FR_PP}|{_EN_PP}|{_DE_PP}|{_IT_PP_ANY})\b", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*•|]|\d+[.)])\s+")
_TICK = re.compile(r"^\s*[✅✔✓☑]\s*")
_ANY_PP = re.compile(rf"\b(?:{_FR_PP}|{_EN_PP}|{_DE_PP}|{_IT_PP_ANY})\b", re.IGNORECASE)
# Espèce d'une affirmation, lue dans le texte reconnu : mêmes racines que les verbes ci-dessus.
_K_SEND = re.compile(r"\b(?:envoy|publi|sent\b|publish|gesendet|geschickt|verschickt|abgeschickt|veröffentlicht|inviat|mandat|"
                     r"pubblicat)", re.IGNORECASE)
_K_DELETE = re.compile(r"\b(?:supprim|effac|annul|delet|remov|cancel|gelösch|storniert|abgesagt|eliminat|cancellat|annullat)",
                       re.IGNORECASE)
_K_SPEND = re.compile(r"\b(?:pay|command|order|bought|bezahlt|bestellt|pagat|ordinat)", re.IGNORECASE)
_K_GENERIC = re.compile(r"\b(?:fait|fatto|done|erledigt|fertig|terminé)\b", re.IGNORECASE)
# Espèce d'un OUTIL : par l'effet, puis par le nom (un outil classé WRITE qui s'appelle `send_x` envoie).
_T_SEND = re.compile(r"(?:^|[_\W])(?:send|publish|reply|forward|notify|sms|tweet|post)(?:$|[_\W])", re.IGNORECASE)
_T_DELETE = re.compile(r"(?:^|[_\W])(?:delete|remove|trash|erase|purge|cancel|drop|destroy)(?:$|[_\W])", re.IGNORECASE)
_T_SPEND = re.compile(r"(?:^|[_\W])(?:pay|order|buy|charge|purchase)(?:$|[_\W])", re.IGNORECASE)


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for p in re.split(r"(?<=[.!?])\s+|\n+", text):
        p = _BULLET.sub("", p.strip()).strip()
        if p:
            out.append(p)
    return out


def _kind_of_claim(text: str) -> str:
    """Espèce d'une affirmation reconnue : send | delete | spend | write | generic (« c'est fait », « done »)."""
    if _K_SEND.search(text):
        return "send"
    if _K_DELETE.search(text):
        return "delete"
    if _K_SPEND.search(text):
        return "spend"
    return "generic" if _K_GENERIC.search(text) else "write"


def claim_kinds(text: str) -> list[str]:
    """Espèces des actions affirmées comme faites dans TOUTE la réponse (voir l'en-tête du module), sans doublon."""
    text = _QUOTED.sub(" ", _FENCE.sub(" ", (text or "").replace("’", "'")))
    sentences = _sentences(text)
    kinds: list[str] = []

    def add(kind: str) -> None:
        if kind not in kinds:
            kinds.append(kind)

    for i, sentence in enumerate(sentences[:400]):
        if sentence.endswith("?"):
            continue
        if _TICK.match(sentence) and _ANY_PP.search(sentence) and not _NEG.search(sentence):
            add(_kind_of_claim(sentence))
            continue
        if (i == 0 or i == len(sentences) - 1) and _BARE.match(sentence):
            add(_kind_of_claim(sentence))
            continue
        for m in _CLAIM.finditer(sentence):
            if _NEG.search(sentence[:m.end()]) or _NOT_NOW.search(sentence[:m.end()]) \
                    or _PAST_AFTER.search(sentence[m.end():]):
                continue
            add(_kind_of_claim(m.group(0)))
            for c in _COORD.finditer(sentence, m.end()):
                if not _NEG.search(sentence[m.end():c.end()]):
                    add(_kind_of_claim(c.group(0)))
    return kinds


def claims_done(text: str) -> bool:
    """Affirmation d'une action accomplie par le modèle, n'importe où dans la réponse (voir l'en-tête du module)."""
    return bool(claim_kinds(text))


def is_drafted_text(reply: str) -> bool:
    """Texte rédigé (long, code, liste, tableau). Ne désactive PLUS la garde (G3) ; sert aux appelants qui veulent le savoir."""
    return len(reply.strip()) > MAX_REPLY or "```" in reply or bool(_LIST.search(reply))


def _tool_kinds(o: CallOutcome) -> set[str]:
    """Espèces d'un appel : un envoi sans nom d'envoi (invitation d'agenda) vaut aussi une écriture."""
    kinds: set[str] = set()
    if o.effect in (Effect.SEND_EXTERNAL, Effect.SEND_BULK) or _T_SEND.search(o.name):
        kinds.add("send")
        if o.effect in (Effect.SEND_EXTERNAL, Effect.SEND_BULK) and not _T_SEND.search(o.name):
            kinds.add("write")
    if o.effect is Effect.DESTRUCTIVE or _T_DELETE.search(o.name):
        kinds.add("delete")
    if o.effect is Effect.SPEND or _T_SPEND.search(o.name):
        kinds.add("spend")
    return kinds or {"write"}


def _unresolved(outs: list[CallOutcome]) -> list[CallOutcome]:
    """Appels à effet restés en attente, refusés ou en échec, sans un appel du même outil abouti APRÈS (un nouvel essai réussi
    solde le premier)."""
    out = []
    for i, o in enumerate(outs):
        if o.state in ("pending", "refused", "failed") and not any(
                later.state == "done" and later.name == o.name for later in outs[i + 1:]):
            out.append(o)
    return out


def _fixed(reply: str, lang: str | None, reason: str, replace: bool) -> str:
    fixed = ptx.pick(ptx.ACTION_NOT_DONE, lang).format(why=ptx.pick(ptx.ACTION_WHY[reason], lang))
    return fixed if replace and len(reply.strip()) <= SHORT_REPLY else f"{reply.rstrip()}\n\n{fixed}"


def guard(messages: list[Message], reply: str, lang: str | None, spec_of: Callable[[str], Any] | None = None,
          failed_ids: Iterable[str] = (), continuation: bool = False) -> str:
    """`reply` corrigée si elle affirme une action faite que les outils du tour n'ont pas faite (jugement action par action,
    voir l'en-tête) ; sinon inchangée. `continuation` : tour de suite d'une carte décidée (l'action a tourné hors tour) : jamais touché."""
    if continuation or not reply:
        return reply
    kinds = claim_kinds(reply)
    if not kinds:
        return reply
    outs = call_outcomes(messages, spec_of, failed_ids)
    if not any(o.state == "done" for o in outs):
        return _fixed(reply, lang, read_outcomes(messages, spec_of, failed_ids).reason(), replace=True)
    pending = _unresolved(outs)
    hit = [o for o in pending if "generic" in kinds or _tool_kinds(o) & set(kinds)]
    if not hit:
        return reply
    reason = "pending" if any(o.state == "pending" for o in hit) else "refused" if any(
        o.state == "refused" for o in hit) else "failed"
    return _fixed(reply, lang, reason, replace=False)
