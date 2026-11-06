"""nokker/tools/router_rules.py — règles DÉTERMINISTES du routeur (`delegate_router`), avant et à la place du modèle.

Le routeur (enfant de profondeur 1, docs/LOKKAL.md §7.22) choisissait seul la lecture seule et le palier de ses enfants
d'après le but réécrit par la voix ; au banc d'orchestration (contrat B, 04.11.2026) : orchestration juste 8/24, la
moitié des erreurs sur `readonly`. Le noyau tranche désormais ce qui se lit dans le texte :

  - `actions(texte)` : la personne demande-t-elle de MODIFIER quelque chose (ranger, déplacer, effacer, répondre, créer,
    annuler…) ? En conversation, `readonly` des enfants du routeur = AUCUNE action trouvée (le modèle ne le choisit
    plus). Une action manquée ne fait rien de mal : l'enfant en lecture seule refuse l'écriture et le dit ; une action
    vue à tort non plus : chaque outil réel garde ses portes et ses cartes.
  - `deep_markers(texte)` : la demande exige-t-elle un jugement (recommander, choisir, diagnostiquer une cause, repérer
    ce qui manque, concevoir une organisation) ? Si oui, le palier deep est un PLANCHER : le routeur ne peut pas le
    descendre en ship (il peut toujours monter en deep).

Français, anglais, allemand. Une négation proche (« sans rien déplacer », « ne réponds pas », « don't send », « nicht
löschen ») annule le mot d'action. Les listes sont courtes et lisibles exprès : on les étend, on ne les apprend pas.
"""
from __future__ import annotations

import re
import unicodedata


def _fold(text: str) -> str:
    """Minuscules, apostrophes droites, sans accents (« déplace » = « deplace »)."""
    t = (text or "").lower().replace("’", "'").replace("‘", "'")
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


# ── actions (modifier, envoyer, créer…) ──
# Français : impératif (2e personne, « range », « range-les », « réponds-lui ») ou infinitif après une demande (« peux-tu
# ranger », « merci de déplacer », « il faut annuler »). Pas d'infinitif seul : « avant de signer » n'est pas une
# demande faite à l'agent.
_FR_IMP = (r"range|deplace|classe|archive|supprime|efface|jette|vide|envoie|renvoie|reponds|ecris|transfere|"
           r"fais suivre|cree|ajoute|inscris|mets|programme|reserve|annule|decale|reporte|deplace|modifie|change|"
           r"renomme|confirme|accepte|decline|refuse|paie|paye|regle|commande|achete|partage|publie|invite|marque|"
           r"desabonne|desinscris|bloque|trie|nettoie|corrige|remplis|signe")
_FR_INF = (r"ranger|deplacer|classer|archiver|supprimer|effacer|jeter|vider|envoyer|renvoyer|repondre|ecrire|"
           r"transferer|faire suivre|creer|ajouter|inscrire|mettre|programmer|reserver|annuler|decaler|reporter|"
           r"modifier|changer|renommer|confirmer|accepter|decliner|refuser|payer|regler|commander|acheter|partager|"
           r"publier|inviter|marquer|desabonner|desinscrire|bloquer|trier|nettoyer|corriger|remplir|signer")
_FR_ASK = (r"peux-tu|peux tu|pourrais-tu|pourrais tu|tu peux|tu pourrais|veux-tu|tu veux bien|merci de|il faut|"
           r"je veux que tu|j'aimerais que tu|occupe-toi de|charge-toi de|n'oublie pas de|essaie de")
_EN_VERB = (r"move|file|archive|delete|remove|trash|clear|send|resend|reply|answer|write to|email|e-mail|forward|create|"
            r"add|schedule|book|cancel|reschedule|postpone|rename|change|edit|update|confirm|accept|decline|reject|pay|"
            r"order|buy|purchase|share|publish|post|invite|mark|unsubscribe|block|sort|clean up|fix|fill in|sign")
_EN_ASK = (r"please|can you|could you|would you|will you|i want you to|i need you to|i'd like you to|go ahead and|"
           r"make sure to|don't forget to")
_DE_VERB = (r"verschiebe|verschieben|verschieb|losche|loschen|losch|entferne|entfernen|sende|senden|schicke|schicken|"
            r"schick|antworte|antworten|beantworte|beantworten|leite .{0,50}weiter|weiterleiten|erstelle|erstellen|"
            r"lege? .{0,50}an|anlegen|trage? .{0,50}ein|eintragen|buche|buchen|storniere|stornieren|sage? .{0,50}ab|"
            r"absagen|archiviere|archivieren|sortiere|sortieren|ordne .{0,50}ein|einordnen|markiere|markieren|"
            r"bezahle|bezahlen|bestelle|bestellen|kaufe|kaufen|lade .{0,50}ein|einladen|bestatige|bestatigen|"
            r"melde .{0,50}ab|abmelden|benenne .{0,50}um|umbenennen|korrigiere|korrigieren|fulle .{0,50}aus|"
            r"ausfullen|unterschreibe|unterschreiben")

# Impératif en tête de proposition seulement : « classe », « commande », « marque » sont aussi des noms.
_CLAUSE = r"(?:^|[.!?;:\n(«\"]\s*|,\s*|\b(?:et|puis|ensuite|alors|aussi|stp|svp|s'il te plait|merci de bien vouloir)\s+)"
_FR_IMP_RE = re.compile(rf"{_CLAUSE}({_FR_IMP})(?:-(?:moi|les|le|la|lui|leur|nous|y|en|toi))*(?=[\s.,;:!?»\")]|$)")
_FR_ASK_RE = re.compile(rf"(?:{_FR_ASK})\s+(?:(?:me|m'|te|t'|le|la|les|lui|leur|nous|vous|y|en|bien|tout|tous|"
                        rf"toutes|vite|aussi|donc|d'abord)\s*)*({_FR_INF})\b")
_EN_RE = re.compile(rf"(?:^|[.!?;:\n]\s*|\b(?:{_EN_ASK})\s+(?:also\s+|just\s+)?)({_EN_VERB})\b")
_DE_RE = re.compile(rf"\b({_DE_VERB})\b")
# Négation dans les quelques mots qui précèdent (fr, en, de) ou qui suivent (« ne range pas », « sende nicht »).
_NEG_BEFORE = re.compile(r"(?:\bne\b|\bn'|\bsans\b|\bdon't\b|\bdo not\b|\bnever\b|\bwithout\b|\bnot\b|\bnicht\b|\bnichts\b|"
                         r"\bkein\w*\b|\bjamais\b|\bpas\b|\bni\b|\bohne\b)[^.,;:!?\n]{0,25}$")
_NEG_AFTER = re.compile(r"^[^.,;:!?\n]{0,25}?\b(?:pas|rien|jamais|nicht|nichts|niemals)\b")


def _not_negated(text: str, start: int, end: int) -> bool:
    return not _NEG_BEFORE.search(text[max(0, start - 30):start]) and not _NEG_AFTER.search(text[end:end + 30])


def actions(*texts: str) -> list[str]:
    """Mots d'action (pliés) trouvés dans les textes, sans les tournures niées. Liste vide : rien à modifier."""
    found: list[str] = []
    for raw in texts:
        t = _fold(raw)
        for rx in (_FR_IMP_RE, _FR_ASK_RE, _EN_RE, _DE_RE):
            for m in rx.finditer(t):
                if _not_negated(t, m.start(1), m.end(1)) and m.group(1) not in found:
                    found.append(m.group(1))
    return found


# ── jugement (palier deep) ──
_DEEP = (
    # français
    r"\bpourquoi\b|\blaquelle\b|\blequel\b|\blesquelles\b(?=[^.?!]{0,40}(?:convien|meilleur|choisir|prendre|garder))|"
    r"\bme convien\w*|\bnous convien\w*|\bvaut mieux\b|\bvaut-il mieux\b|\bconseill\w*|\brecommand\w*|\bhesit\w*|"
    r"\bchoisir entre\b|\bdois-je\b|\bfaut-il\b|\bce qui (?:pourrait |peut )?manqu\w*|\bpose\w* probleme|"
    r"\bpoint\w* faibles?\b|\brisques?\b|\bavant de signer\b|\bdiagnosti\w*|\bcauses?\b|\borganisation\b|"
    r"\barchitecture\b|\bstrategie\b|\bconcevoir\b|\bconcois\b|\bconception\b|\bapprofondi\w*|\bevalue\w*|"
    r"\bcritique\w*|\bavantages et (?:les )?inconvenients\b|\bpour et (?:le )?contre\b|"
    # anglais
    r"\bwhy\b|\bwhich\b[^.?!]{0,40}\b(?:should|better|best|suits?|recommend\w*)\b|\bshould i\b|\bshould we\b|\brecommend\w*|\badvi[cs]e\b|\btrade-?offs?\b|"
    r"\brisks?\b|\bwhat(?:'s| is) missing\b|\bdiagnos\w*|\broot cause\b|\bdesign\b|\bstrategy\b|\bpros and cons\b|"
    r"\bbefore signing\b|\bin depth\b|\bin-depth\b|"
    # allemand
    r"\bwarum\b|\bwieso\b|\bweshalb\b|\bwelche\w*\b[^.?!]{0,40}\b(?:passt|besser|soll\w*|empfiehl\w*)\b|\bempfehl\w*|\bempfiehl\w*|"
    r"\brisik\w*|\bwas fehlt\b|\bursache\w*|\bdiagnos\w*|\bkonzept\w*|\barchitektur\b|\bstrategie\b|\babwag\w*|"
    r"\bvor- und nachteile\b|\bvor der unterschrift\b")
_DEEP_RE = re.compile(_DEEP)


def deep_markers(*texts: str) -> list[str]:
    """Marqueurs de jugement (pliés) trouvés dans les textes. Liste vide : rien n'impose deep."""
    found: list[str] = []
    for raw in texts:
        for m in _DEEP_RE.finditer(_fold(raw)):
            if m.group(0) not in found:
                found.append(m.group(0))
    return found


__all__ = ["actions", "deep_markers"]
