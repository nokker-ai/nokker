"""Garde de fin de tour : une réponse qui affirme « c'est noté » alors que toutes les écritures de `memory_note` du
tour ont échoué est corrigée par un texte fixe (la personne ne doit jamais croire qu'une note existe quand rien n'a été
écrit). Le résultat d'un refus se termine par `NOT_WRITTEN` (nokker/context/project_memory.py) : c'est la marque lue ici.

Une note refusée n'est corrigée que si la PERSONNE en a demandé une (`asked_note` : verbe d'enregistrement dans son message
du tour ; une préférence énoncée sans verbe ne suffit plus) : un `memory_note` que le modèle a appelé de lui-même et que la garde de portée a refusé
n'est pas un échec à signaler, le refus de l'outil suffit (sinon le texte fixe « je n'ai pas pu noter ça » répond à une
demande que personne n'a faite). Sans message de la personne à lire (`user_text` None), on suppose la demande : l'ancien
comportement. Le cas 2 (accusé nu sans appel) ne dépend pas de la demande : « noté » dit sans qu'aucune note existe est un faux accusé.

Se déclenche dans deux cas, quand la réponse AFFIRME « noté » (reconnaissance ci-dessous) :
  1. au moins un `memory_note` en échec ET aucun qui ait abouti (écrit, déjà noté, en attente) ;
  2. AUCUN `memory_note` appelé dans le tour (outil absent — `deny_tools`, mémoire éteinte — ou modèle qui n'a rien appelé),
     sauf si un autre outil À EFFET a abouti ou posé une carte dans le tour : un « noté » peut alors désigner cet effet
     (rappel posé, agenda…), la garde ne remplace pas une réponse peut-être vraie. Les lectures et les refus ne comptent pas.
     Ce cas est le plus prudent : il ne vise que l'ACCUSÉ NU (la réponse n'est que l'accusé et des politesses : « C'est noté. »,
     « Parfait, bien noté ! ») OU une PROMESSE de mémoire dans les deux premières phrases d'une réponse courte
     (`promises_memory` : « C'est noté. Je noterai ta préférence pour le thé. », « I'll remember that. »). « Noté jeudi. », « Noté : vendredi, payer la facture. » (accusé d'une réponse à une question,
     reformulation) ne sont pas touchés, SAUF au cas 3 ; ni les tours de suite d'une carte décidée (`continuation`) : l'outil a tourné hors tour.
  3. (cas 2 + demande) AUCUN `memory_note` appelé ET la PERSONNE a demandé de mémoriser par un VERBE (`asked_note(user_text)` : forte, jamais un mot faible seul) ET la réponse
     COMMENCE par un accusé « noté » (`leading_ack`) : corrigée quelle que soit la suite (« C'est noté. Je prendrai le thé le
     matin. » : une reformulation n'est pas une écriture), sans la limite de `SHORT_REPLY` ni des deux premières phrases ; une
     réponse longue garde sa suite, précédée du texte fixe. Sans demande (`user_text` None ou sans demande), « Noté jeudi. » reste intact.
Le texte de remplacement dépend de ce qui a été tenté : un appel ÉCHOUÉ → `NOTE_FAILED` (« redis-le-moi pour que je réessaie ») ;
AUCUN appel → `NOTE_NOT_TRIED` (rien n'a été tenté : ni promesse de réessayer, ni prétention d'échec ; invite à redemander).
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from nokker.core import model_texts as mt
from nokker.core import person_texts as ptx
from nokker.core.tool_outcomes import read_outcomes
from nokker.core.turn import Message

TOOL = "memory_note"
# Début de la phrase qui clôt tout refus de `memory_note` (vue du modèle) ; project_memory.NOT_WRITTEN la complète.
FAILED_MARK = "rien n'a été noté"
_FAILED_MARKS = tuple(mt.NOTE_NOT_WRITTEN_MARK.values())  # la même phrase dans les quatre langues du tour
# Une réponse plus longue que ceci garde son texte : le texte fixe s'y ajoute au lieu de la remplacer.
SHORT_REPLY = 160

# Reconnaissance d'un ACCUSÉ ou d'une promesse de mémoire, mots entiers, sans casse (fr, en, de, it). Une observation (« je note
# que… », « I note that… », « ich notiere, dass… », « noto che… ») n'en est pas une, ni « note » seul (le nom commun) ni les
# infinitifs du texte de remplacement (« noter », « notieren », « annotarlo »).
_CLAIM = re.compile(
    r"\b(?:noté|notée|notés|notées|je\s+(?:l'|le\s+|la\s+|te\s+)?ai\s+noté|je\s+prends\s+note|je\s+note(?!\s*(?:que\b|qu'))|"
    r"je\s+m'en\s+(?:souviendrai|rappellerai)|je\s+retiens|(?:c'est|est|sera)\s+enregistré(?:e)?|"
    r"noted|i(?:'ll|\s+will|'ve|\s+have)\s+(?:made\s+a\s+)?(?:note(?!\s+that\b)|noted)|i(?:'ll|\s+will)\s+remember|"
    r"got\s+it,?\s+saved|(?:it's|it\s+is)\s+saved|"
    r"notiert|vermerkt|ich\s+(?:habe|hab)\s+(?:mir\s+)?(?:das\s+)?(?:notiert|vermerkt)|ich\s+merke\s+es\s+mir|"
    r"ich\s+werde\s+mir\s+das\s+merken|"
    r"annotato|ho\s+annotato|segnato|appuntato|preso\s+nota|me\s+lo\s+ricordo|lo\s+ricorder[òo]|me\s+ne\s+ricordo)\b",
    re.IGNORECASE)
# Négation dans la même phrase (« ce n'est pas noté », « not noted », « nicht notiert ») : pas une affirmation.
_NEG = re.compile(r"\b(?:pas|rien|jamais|not|never|nicht|kein|keine|nie|niente|mai|non)\b|\bn'", re.IGNORECASE)


# La personne demande de mémoriser : VERBE d'enregistrement à l'impératif ou en demande (« note que », « retiens », « peux-tu
# noter », « can you remember », « kannst du dir merken », « puoi ricordare »). Mots entiers, sans casse ; fr, en, de, it.
# Les mots FAIBLES seuls (« toujours », « jamais », « je préfère », always, never, I prefer, immer, sempre, d'ora in poi…) ne
# comptent PAS : une simple question sur une préférence verrait sa bonne réponse remplacée, pire qu'un faux accusé.
# « note » seul (le nom) n'en est pas une, seulement suivi de que/ça/down/this…
_ASK = re.compile(
    r"\b(?:retiens|retenez|retenir|mémorise|mémoriser|enregistre|enregistrez|souviens-toi|souvenez-vous|rappelle-toi|"
    r"n'oublie\s+pas|n'oubliez\s+pas|noter|notez|note-(?:le|la|moi|ça)|"
    r"note(?:s)?\s+(?:que|qu'|ça|cela|ceci|bien|ce\s+qui)|garde\s+(?:ça\s+)?en\s+(?:mémoire|tête)|"
    r"remember|don't\s+forget|do\s+not\s+forget|keep\s+in\s+mind|make\s+a\s+note|take\s+note|"
    r"note\s+(?:that|this|it|down)|write\s+(?:this|that|it)\s+down|save\s+(?:this|that|it)|"
    r"merk\s+dir|merke\s+dir|merk's|merks|merken|vergiss\s+nicht|notiere|notier|speichere|speicher|behalte|"
    r"ricorda|ricordati|ricordare|ricorda-ti|annota|annotare|appunta|segna|segnare|prendi\s+nota|non\s+dimenticare|"
    r"tieni\s+a\s+mente|memorizza|salva)\b",
    re.IGNORECASE)


# Verbes de mémoire de `_ASK` qu'une négation voisine retourne (« ne retiens pas ça », « don't save this », « merk dir das
# nicht », « non annotare »). Les adverbes (toujours, jamais, always…) et les formes déjà négatives (« n'oublie pas ») n'y sont pas.
_ASK_VERB = re.compile(
    r"(?:retiens|retenez|retenir|mémorise|mémoriser|enregistre|enregistrez|souviens-toi|souvenez-vous|rappelle-toi|noter|"
    r"notez|note|remember|make\s+a\s+note|take\s+note|write|save|merk\s+dir|merke\s+dir|merk's|merks|notiere|notier|"
    r"merken|speichere|speicher|behalte|ricorda|ricordati|ricordare|annota|annotare|appunta|segna|segnare|prendi\s+nota|"
    r"memorizza|salva)\b", re.IGNORECASE)
_ASK_BEFORE = re.compile(r"(?:\bne|\bn'|\bnon|\bnot|\bnever|\bdon't|\bdo\s+not|\bplease\s+don't|\bkein|\bnicht)\s*(?:\w+\s+)?$",
                         re.IGNORECASE)
_ASK_AFTER = re.compile(r"^\s?(?:\w+\s+){0,2}?(?:pas|plus|rien|not|nicht|niente)\b", re.IGNORECASE)


def asked_note(user_text: str | None) -> bool:
    """Le message de la personne demande de mémoriser quelque chose, ou énonce une préférence / une règle. Une demande
    NÉGATIVE (« ne retiens pas ça », « don't save this ») n'en est pas une."""
    text = (user_text or "").replace("’", "'")
    for m in _ASK.finditer(text):
        if _ASK_VERB.match(m.group(0)):
            before = text[max(0, m.start() - 24):m.start()]
            if _ASK_BEFORE.search(before) or _ASK_AFTER.match(text[m.end():m.end() + 24]):
                continue
        return True
    return False


# Promesse de mémoire (futur ou présent), mots entiers, sans casse (fr, en, de, it). Une observation (« je noterai que… »,
# « I'll note that… ») n'en est pas une ; une négation juste avant ou après non plus.
_PROMISE = re.compile(
    r"\b(?:je\s+(?:le\s+|la\s+|te\s+)?(?:noterai|retiendrai)(?!\s*(?:que\b|qu'))|je\s+m'en\s+(?:souviendrai|rappellerai)|"
    r"je\s+(?:garde|garderai)\s+(?:ça|cela|ceci|le|la)\s+en\s+(?:mémoire|tête)|"
    r"i(?:'ll|\s+will)\s+(?:remember|note(?!\s+that\b)|keep\s+(?:that|it|this)\s+in\s+mind)|"
    r"ich\s+merke\s+(?:es\s+)?mir|ich\s+werde\s+mir\s+(?:das|es)\s+merken|ich\s+werde\s+(?:es|das)\s+notieren|"
    r"lo\s+annoter[òo]|me\s+lo\s+ricorder[òo]|lo\s+ricorder[òo]|lo\s+terr[òo]\s+a\s+mente)\b",
    re.IGNORECASE)
PROMISE_SENTENCES = 2


def promises_memory(text: str) -> bool:
    """Une des deux premières phrases promet de retenir ou de noter quelque chose (affirmation, pas observation)."""
    for sentence in _sentences((text or "").replace("’", "'"))[:PROMISE_SENTENCES]:
        for m in _PROMISE.finditer(sentence):
            if not _NEG.search(sentence[:m.start()]) and not _NEG.search(sentence[m.end():m.end() + 14]):
                return True
    return False


def _sentences(text: str) -> list[str]:
    return [p for p in re.split(r"(?<=[.!?])\s+|\n+", text or "") if p.strip()]


def claims_noted(text: str) -> bool:
    for m in _CLAIM.finditer(text or ""):
        start = max(text.rfind(c, 0, m.start()) for c in ".!?\n:;") + 1
        if not _NEG.search(text[start:m.start()]):
            return True
    return False


# Mots de politesse qui peuvent accompagner un accusé nu (fr, en, de, it) ; tout autre mot fait une réponse « avec contenu ».
_FILLER = frozenset((
    "c'", "c'est", 'c', 'est', 'ok', 'okay', 'oui', 'bien', 'très', 'parfait', 'voilà', 'merci', "d'accord",
    'compris', 'entendu', 'super', 'tout', 'alors', "it's", 'it', 'is', 'got', 'thanks', 'thank', 'you',
    'great', 'perfect', 'sure', 'alright', 'all', 'right', 'will', 'done', 'ich', 'habe', 'hab', 'es', 'das',
    'mir', 'ist', 'alles', 'klar', 'gut', 'ja', 'danke', 'perfekt', 'gerne', 'verstanden', 'va', 'bene',
    'certo', 'perfetto', 'grazie', 'capito', "d'accordo", 'ricevuto', 'è',
))


def is_bare_ack(text: str) -> bool:
    """La réponse n'est qu'un accusé « noté » (reconnu) et des politesses, sans contenu."""
    if not claims_noted(text):
        return False
    rest = _CLAIM.sub(" ", (text or "").replace("’", "'"))
    words = re.findall(r"[\w']+", rest.lower())
    return all(w in _FILLER for w in words)


# Accusé « noté » EN TÊTE de réponse (fr, en, de, it), mots entiers, casse / ponctuation / apostrophes typographiques tolérées :
# une politesse d'ouverture possible, puis l'accusé lui-même. Seule la TÊTE compte : ce qui suit n'est pas lu.
_LEAD_OPEN = (r"(?:(?:ok|okay|oui|parfait|d'accord|compris|entendu|super|alors|bien\s+sûr|alright|sure|great|perfect|"
              r"ja|klar|gut|perfekt|va\s+bene|certo|perfetto|capito)\s*[,.!:;\-–—]*\s+)?")
_LEAD_ACK = re.compile(
    r"^[\W_]*" + _LEAD_OPEN + r"(?:"
    r"(?:c'est\s+)?(?:(?:très\s+)?bien\s+)?(?:noté|notée|notés|notées)|"
    r"(?:je\s+(?:l'|le\s+|la\s+|te\s+)?ai|j'ai)\s+(?:bien\s+)?noté|je\s+prends\s+note|c'est\s+enregistré(?:e)?|"
    r"got\s+it(?:,?\s+noted)?|noted|verstanden(?:,?\s+notiert)?|(?:i've|i\s+have)\s+(?:made\s+a\s+)?(?:noted|note)(?:\s+(?:of\s+)?(?:it|that|this))?|"
    r"notiert|vermerkt|ich\s+(?:habe|hab)\s+(?:mir\s+)?(?:das\s+|es\s+)?(?:notiert|vermerkt)|"
    r"annotato|ho\s+annotato|segnato|appuntato|preso\s+nota|ricevuto"
    r")(?![\w'])", re.IGNORECASE)


def leading_ack(text: str) -> re.Match | None:
    """La réponse COMMENCE par un accusé « noté » (« C'est noté », « Noté », « Noted », « Got it », « Verstanden », « Annotato », « Ricevuto »…)."""
    return _LEAD_ACK.match((text or "").replace("’", "'"))


def note_outcomes(messages: list[Message]) -> tuple[int, int]:
    """(échecs, aboutis) des appels `memory_note` du tour, lus dans les résultats d'outil."""
    failed = ok = 0
    for m in messages:
        if m.role == "tool" and m.name == TOOL:
            if any(mark in (m.content or "") for mark in _FAILED_MARKS):
                failed += 1
            else:
                ok += 1
    return failed, ok


def guard(messages: list[Message], reply: str, lang: str | None, spec_of: Callable[[str], Any] | None = None,
          failed_ids: Iterable[str] = (), continuation: bool = False, user_text: str | None = None,
          tool_removed: bool = False) -> str:
    """`reply` corrigée si elle affirme « noté » sans qu'aucune note n'ait été écrite (tous les `memory_note` refusés, ou
    aucun appelé et aucun outil à effet abouti ou en attente) ; sinon inchangée. `spec_of`/`failed_ids` : voir
    `tool_outcomes.read_outcomes` ; `continuation` : tour de suite d'une carte décidée (sans `spec_of`, tout outil abouti compte comme à effet : le plus prudent) ;
    `user_text` : message de la personne (None = inconnu, la demande est supposée) : un refus de `memory_note` qu'elle n'a pas demandé ne se corrige pas.
    `tool_removed` : `memory_note` est RETIRÉ par l'hôte pour ce fil (`deny_tools`, `memory: off|host`) : la garde se tait, la
    note de l'hôte fait foi (l'outil n'existe pas pour le modèle) ; réponse intacte, sur échec, accusé nu ou promesse."""
    if tool_removed:
        return reply
    failed, ok = note_outcomes(messages)
    if ok:
        return reply
    if failed:
        if user_text is not None and not asked_note(user_text):
            return reply  # note venue du modèle seul : le refus de l'outil suffit, rien à signaler à qui n'a rien demandé
        if not claims_noted(reply):
            return reply
        return _fixed(reply, ptx.pick(ptx.NOTE_FAILED, lang))
    # Aucun `memory_note` appelé dans le tour : « noté » ne repose sur rien (sauf un autre outil à effet, ou suite de carte).
    if continuation or read_outcomes(messages, spec_of, failed_ids).effect_or_pending:
        return reply
    lead = leading_ack(reply) if user_text is not None and asked_note(user_text) else None
    if lead is None and not (is_bare_ack(reply) or (len(reply.strip()) <= SHORT_REPLY and promises_memory(reply))):
        return reply
    fixed = ptx.pick(ptx.NOTE_NOT_TRIED, lang)
    if lead is not None and len(reply.strip()) > SHORT_REPLY:  # demande de mémoire + réponse qui ouvre par « noté », quelle que soit la suite
        rest = reply.replace("’", "'")[lead.end():].lstrip(" \t,.;:!?-–—")
        return f"{fixed}\n\n{rest}" if rest.strip() else fixed
    return _fixed(reply, fixed)


def _fixed(reply: str, fixed: str) -> str:
    return fixed if len(reply.strip()) <= SHORT_REPLY else f"{reply.rstrip()}\n\n{fixed}"
