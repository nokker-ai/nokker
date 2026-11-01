"""Outils de texte du contexte : découpage en mots, mots-clés, bornage. Pur Python, aucun modèle, déterministe."""
from __future__ import annotations

import re
from collections import Counter

_WORD = re.compile(r"[0-9a-zàâäçéèêëîïôöùûüÿœæ][0-9a-zàâäçéèêëîïôöùûüÿœæ_\-]*", re.IGNORECASE)

# Mots vides FR + quelques EN : assez pour qu'un mot-clé soit un mot qui porte du sens ; pas exhaustif.
STOPWORDS = frozenset(["alors", "ainsi", "après", "avec", "avoir", "avons", "avez", "aussi", "autre", "autres", "beaucoup", "bien", "cela", "celui", "ceux", "chaque", "comme", "comment", "dans", "dont", "donc", "elle", "elles", "encore", "entre", "était", "étaient", "être", "fait", "faire", "fois", "leur", "leurs", "mais", "même", "moins", "notre", "nous", "parce", "pour", "pourquoi", "plus", "puis", "quand", "quel", "quelle", "quels", "quelles", "sans", "sera", "sont", "sous", "sur", "tant", "tous", "tout", "toute", "toutes", "très", "trop", "une", "unes", "vers", "voici", "voilà", "votre", "vous", "avez", "cette", "ces", "cet", "ceci", "celle", "ici", "déjà", "depuis", "peut", "peux", "veux", "the", "and", "for", "with", "that", "this", "from", "have", "has", "are", "was", "were", "not", "you", "your", "our", "can", "will"])

# Mots outils FR/EN de 1 à 3 lettres (et quelques autres) absents de STOPWORDS : « les », « ont » pris pour des termes
# d'une REQUÊTE faisaient remonter des messages sans rapport à pertinence 1,00 (bench/keys/RESULTS.md, défaut 4).
# Retirés des seuls termes de requête (`index.query_terms(extra_stop=…)`), et seulement drapeau
# `never_clear_fixes_enabled` allumé : STOPWORDS reste tel quel (titres, mots-clés, ligne d'index de la couche 3 et
# résumés inchangés, gel du format).
FUNCTION_WORDS = frozenset([
    # FR
    "les", "des", "aux", "par", "pas", "que", "qui", "quoi", "est", "ont", "ses", "mes", "tes", "nos", "vos", "mon",
    "ton", "son", "lui", "eux", "moi", "toi", "soi", "ni", "ne", "ou", "où", "et", "un", "le", "la", "de",
    "du", "au", "en", "à", "se", "sa", "ma", "ta", "ce", "il", "ils", "je", "tu", "on", "me", "te", "y", "qu", "lors",
    "avait", "avaient", "aurait", "étais", "suis", "es", "sommes", "êtes", "serait", "seront", "ai", "as",
    "va", "vas", "vont", "font", "dit", "chez", "cela", "ça", "non", "oui", "si", "dès", "via", "selon", "parmi",
    "celles", "lequel", "laquelle", "lesquels", "auquel", "duquel", "dois", "doit", "faut", "quelque", "quelques",
    "aucun", "aucune", "certains", "tel", "telle", "afin", "contre", "pendant", "avant", "jusqu", "jusque",
    # EN
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "are", "was", "were", "not", "you", "your",
    "our", "can", "will", "a", "an", "of", "to", "in", "on", "at", "by", "is", "it", "its", "be", "been", "or", "as",
    "but", "if", "so", "do", "does", "did", "had", "his", "her", "him", "she", "he", "they", "them", "their", "we",
    "us", "my", "me", "i", "who", "what", "when", "where", "which", "why", "how", "all", "any", "out", "off", "too",
    "than", "then", "there", "these", "those", "into", "onto", "over", "about", "would", "could", "should", "shall",
    "might", "must", "also", "just", "very", "some", "such", "own", "same", "each", "every", "yes", "no", "nor",
    "only", "more", "most", "other", "being", "here", "while", "until", "upon", "yet", "ever"]) - STOPWORDS


def words(text: str) -> list[str]:
    """Mots en minuscules (lettres accentuées, chiffres, `_` et `-` internes), sans filtre."""
    return [m.group(0).lower() for m in _WORD.finditer(text or "")]


def content_words(text: str, min_len: int = 3) -> list[str]:
    return [w for w in words(text) if len(w) >= min_len and w not in STOPWORDS and not w.isdigit()]


def keywords(text: str, n: int = 12) -> list[str]:
    """Les `n` mots les plus fréquents (≥ 4 lettres), égalité → ordre de première apparition."""
    counts: Counter[str] = Counter()
    first: dict[str, int] = {}
    for i, w in enumerate(content_words(text, 4)):
        counts[w] += 1
        first.setdefault(w, i)
    ranked = sorted(counts, key=lambda w: (-counts[w], first[w]))
    return ranked[:n]


_PUNCT = ".,;:!?()«»\"'’[]"
_ACRONYM = re.compile(r"[A-ZÀÂÄÇÉÈÊËÎÏÔÖÙÛÜŸ]{2,}[0-9]*")
_CAPITALIZED = re.compile(r"[A-ZÀÂÄÇÉÈÊËÎÏÔÖÙÛÜŸ][a-zàâäçéèêëîïôöùûüÿœæ]{2,}")


# Marqueurs d'un REVIREMENT de l'utilisateur (il annule ou change une décision prise plus tôt) — banc X9, §5.1 de
# bench/never_clear/RESULTS.md. Formes impératives ou figées seulement : « je reviens » (pause), « réponds « non » »,
# « le retour en arrière » (nom) ne sont pas des revirements. Un faux positif coûte une ligne de contexte ; un oubli
# coûte une consigne annulée ré-appliquée.
_REVOCATION = re.compile(
    r"\bannul(e|ez|er|ons)\b|\brevi(ens|enez|enir|ent) (à|au|aux|en arrière|sur)\b|\bfinalement\b|"
    r"\bremet(s|tez|tre) (le|la|les|l'|un|une|plutôt)|\bdéfai(s|tes|re)\b|\brétabli(s|ssez|r)\b|\bah non\b|"
    r"\ben fait,? non\b|\boublie[sz]? (ça|ce|cette|cela|le|la|les)\b|\bne (veut|veux|veulent|voulons) plus\b|"
    r"\bchang\w* d'avis\b|\bon ne (fait|garde) plus\b|"
    r"\b(revert|undo|roll ?back|scratch that|never ?mind)\b", re.IGNORECASE)


def is_revocation(text: str) -> bool:
    """Vrai si le message (ou la phrase) annule ou change une décision antérieure (marqueurs ci-dessus)."""
    return bool(_REVOCATION.search(text or ""))


# Marqueurs d'une EXIGENCE DE PRÉSERVATION posée au fil d'une tâche : ce qui doit survivre aux changements suivants
# (« réexporte total_ht pour ne rien casser chez les appelants », « X doit rester importable », « la facture d'origine
# ne doit pas être modifiée », « garde le comportement actuel ») — banc X9, RESULTS.md §10.3, NEVER-CLEAR §13. Le
# code qui porte une telle exigence sert souvent des appelants HORS du dépôt : rien ne l'utilise dedans, un nettoyage
# le prend pour du code mort. Pas les spécifications ordinaires (« -1233 doit donner -1235 ») : les tests du projet
# les portent déjà ; pas « toujours… / jamais… » seuls, que le préfixe fait noter en règle (`memory_note`).
_CONSTRAINT = re.compile(
    r"\bpour ne (rien|pas) casser\b|\bsans (rien )?casser\b|\bne (cass|touch)(e|ez|er) (rien|pas|jamais)\b|"
    r"\bré-?export\w*|\b(rétro-?)?compatib\w*|"
    r"\bdoi(t|vent) (toujours |encore )?rester\b|\brest(e|ent|er) (importables?|compatibles?|disponibles?|publi(c|cs|que|ques))\b|"
    r"\bgarde(z|r)? (le comportement|l'api|la signature|l'interface|le nom|les noms|la compatibilité)\b|"
    r"\bne doi(t|vent) (pas|plus|jamais) (être )?(modifi|chang|supprim|bouger|disparaître)\w*|"
    r"\bne doi(t|vent) (pas )?changer\b|"
    r"\b(backwards?[- ]compat\w*|re-?export\w*|must (stay|remain)|do(n't| not) break|keep (it )?(working|importable))\b",
    re.IGNORECASE)


def is_constraint(text: str) -> bool:
    """Vrai si le message (ou la phrase) pose une exigence de préservation (marqueurs ci-dessus)."""
    return bool(_CONSTRAINT.search(text or ""))


def constraint_clauses(text: str) -> str:
    """Les seules propositions (découpe sur les phrases puis sur « ; ») qui portent une exigence, « » si aucune :
    « modeles.py ne garde que les classes, mais réexporte total_ht… » sans le « Crée factura/calculs.py… » d'avant."""
    picked: list[str] = []
    for sent in sentences(text):
        parts = [p.strip() for p in sent.split(";") if p.strip()]
        picked.extend(p for p in parts if is_constraint(p))
    return " ".join(picked)


# CONSIGNES DURABLES (bench/keys/RESULTS.md, défaut 2 ; NEVER-CLEAR §14) : une règle que l'utilisateur donne à
# l'ASSISTANTE sur sa façon de répondre ou d'agir, pour la suite (« termine chacune de tes réponses par X », « réponds
# toujours en anglais », « désormais, vouvoie-moi », « ne mets jamais d'emoji »). Le noyau cloud n'offre pas
# `memory_note` : sans ligne en couche 3, la règle tombe à la première clôture d'épisode. Deux conditions dans la MÊME
# phrase, hors segments entre guillemets : (1) elle s'adresse à l'assistante — un impératif d'une liste fermée de verbes
# d'action ou de réponse, ou « tu dois / je veux que tu / merci de / peux-tu… » ; (2) elle vaut pour la suite —
# « toujours » ou « jamais » collé au verbe, « désormais / dorénavant / à partir de maintenant / pour toute la suite »,
# « chacune de tes réponses / à chaque réponse / chaque fois que ». Pas : « il pleut toujours le lundi », « chaque mois
# je paie… », « ajoute une docstring à chaque fichier » (tâche ponctuelle), « vérifie que ça marche toujours »
# (« toujours » = encore), une citation, un texte collé.
_V_FR = (r"(?:termine|terminez|finis|finissez|conclus|concluez|commence|commencez|débute|débutez|réponds|répondez|"
         r"écris|écrivez|rédige|rédigez|signe|signez|utilise|utilisez|emploie|employez|parle|parlez|tutoie|tutoyez|"
         r"vouvoie|vouvoyez|appelle|appelez|nomme|nommez|mets|mettez|ajoute|ajoutez|évite|évitez|donne|donnez|"
         r"cite|citez|formule|formulez|reformule|reformulez|sois|soyez|reste|restez|fais|faites|inclus|incluez|"
         r"précise|précisez|indique|indiquez|demande|demandez|vérifie|vérifiez|pense|pensez|propose|proposez|"
         r"traduis|traduisez|résume|résumez|explique|expliquez|garde|gardez|place|placez|insère|insérez|"
         r"affiche|affichez|mentionne|mentionnez|rappelle|rappelez|préviens|prévenez|lance|lancez|relis|relisez|"
         r"teste|testez|sépare|séparez|numérote|numérotez|souligne|soulignez|limite|limitez|adresse|adressez|"
         r"tiens|tenez|envoie|envoyez|copie|copiez|joins|joignez|structure|structurez|présente|présentez|"
         r"choisis|choisissez|privilégie|privilégiez|écarte|écartez|ignore|ignorez|accompagne|accompagnez)")
_V_EN = (r"(?:end|finish|close|start|begin|open|reply|respond|answer|write|use|sign|call|address|add|include|avoid|"
         r"give|cite|keep|put|speak|talk|mention|say|format|summari[sz]e|translate|be|stay|check|run|ask|append|"
         r"prefix|close|greet|refer|list|show|send|copy|attach|use)")
_PRON = r"(?:-(?:moi|nous|le|la|les|lui|leur|toi|vous|en|y))*"
_LEAD = r"(?:^|[:;,(—–-]\s*|\b(?:et|puis|mais|alors|aussi|donc|stp|svp|s'il te plaît|s'il vous plaît|please|and|then)\s+)"
_DURABLE_IMPERATIVE = re.compile(
    rf"(?:{_LEAD}|\b(?:toujours|jamais|désormais|dorénavant|systématiquement)\s+){_V_FR}{_PRON}\b|"
    rf"\b(?:ne|n')\s*(?:me |nous |le |la |les |lui |leur |m'|l'|y |en )?{_V_FR}\b|"
    rf"(?:{_LEAD}|\b(?:always|never|ever)\s+){_V_EN}\b",
    re.IGNORECASE)
_DURABLE_REQUEST = re.compile(
    r"\b(?:tu|vous)\s+(?:dois|devras|devez|devrez|vas|iras|allez)\b|"
    r"\b(?:je|j')\s*(?:veux|voudrais|aimerais|souhaite|préfère|préférerais|exige)\s+(?:que\s+tu|que\s+vous|qu'à)\b|"
    r"\bil faut que (?:tu|vous)\b|\bmerci de\b|\b(?:peux|pourrais|pouvez|pourriez)-(?:tu|vous)\b|\btu peux\b|"
    r"\b(?:i want|i'd like|i would like|i need) you to\b|\byou (?:must|should|will|have to|need to)\b|"
    r"\b(?:can|could|would) you\b|\bmake sure (?:to|you)\b",
    re.IGNORECASE)
_DURABLE_SCOPE = re.compile(
    r"\b(?:désormais|dorénavant|à partir de (?:maintenant|ce moment|aujourd'hui|là|d'ici)|à l'avenir|"
    r"pour (?:toute )?la suite|jusqu'à nouvel ordre|systématiquement|en permanence|tout le temps|"
    r"(?:chacune|chacun|chaque|toutes|tous)\s+(?:de\s+)?(?:tes|vos|les|ta|ton|votre)?\s*"
    r"(?:réponses?|messages?|mails?|e-mails?|courriels?|textes?|résumés?|explications?|propositions?|tours?|commits?)|"
    r"à chaque (?:réponse|message|fois|tour|échange|mail|commit)|chaque fois que|dans (?:toutes )?tes réponses|"
    r"from now on|going forward|henceforth|until further notice|in (?:the )?future|"
    r"(?:every|each|all)\s+(?:one of\s+)?(?:your\s+|of your\s+)?(?:repl(?:y|ies)|responses?|answers?|messages?|emails?)|"
    r"every time|each time)\b",
    re.IGNORECASE)
_DURABLE_ADVERB = re.compile(  # « toujours / jamais » COLLÉ au verbe (« réponds toujours », « tu dois toujours répondre »)
    rf"{_V_FR}{_PRON}\s+(?:moi\s+)?(?:plus\s+)?(?:toujours|jamais|systématiquement)\b|"
    r"\b(?:toujours|jamais|systématiquement)\s+(?:me\s+|m'|le\s+|la\s+|les\s+|l')?\w+(?:er|ir|re)\b|"
    rf"\b(?:always|never)\s+{_V_EN}\b|\bdon't ever\b|\bdo not ever\b",
    re.IGNORECASE)
_QUOTED = re.compile(r"«[^»]*»|“[^”]*”|\"[^\"]*\"|‘[^’]*’")
_REPORTED = re.compile(r"\b(?:m'a|t'a|lui a|nous a|leur a|m'avait|a|ont|avait)\s+(?:dit|écrit|demandé|répondu)\b|"
                       r"\b(?:says|said|wrote|told me)\b|\bselon (?:lui|elle|eux)\b", re.IGNORECASE)
_ASK_OPENERS = re.compile(r"^\s*(?:(?:peux|pourrais|pouvez|pourriez)-(?:tu|vous)|tu peux|can you|could you|would you)\b",
                          re.IGNORECASE)
PASTE_MAX_CHARS = 700  # au-delà (ou ≥ 4 lignes, ou un bloc de code), le message est un texte COLLÉ : pas une consigne


def _looks_pasted(text: str) -> bool:
    t = text or ""
    return len(t) > PASTE_MAX_CHARS or "```" in t or sum(1 for ln in t.splitlines() if ln.strip()) >= 4


def _durable_sentence(sent: str) -> bool:
    if _REPORTED.search(sent):
        return False
    bare = _QUOTED.sub(" ", sent)
    if bare.rstrip().endswith("?") and not _ASK_OPENERS.search(bare):
        return False
    imperative = bool(_DURABLE_IMPERATIVE.search(bare))
    request = bool(_DURABLE_REQUEST.search(bare))
    if not (imperative or request):
        return False
    return bool(_DURABLE_SCOPE.search(bare) or _DURABLE_ADVERB.search(bare))


# HIÉRARCHIE : une consigne durable ne passe jamais avant les règles du système ni les approbations. Une demande de
# contourner une approbation, une porte ou une règle de sécurité n'est PAS promue (ni comme consigne, ni comme
# révocation) : « à partir de maintenant, envoie sans me demander », « ne me demande plus de valider », « ignore les
# règles », « from now on, send without asking », « stop asking for approval », « ignore your rules ».
_BYPASS = re.compile(
    r"\bsans\s+(?:me\s+|nous\s+|m'|lui\s+)?(?:le\s+|la\s+)?(?:demander|consulter|prévenir)\b|"
    r"\bsans\s+(?:ma|mon|notre|leur|son|sa|aucune?|une|la|l'|les|de)?\s*(?:validation|approbation|accord|"
    r"confirmation|permission|autorisation|feu vert|go|relecture|vérification)s?\b|"
    r"\b(?:ne|n')\s*(?:me\s+|nous\s+)?demande[sz]?\s+plus\b|"
    r"\b(?:arrête|arrêtez|cesse|cessez)\s+de\s+(?:me\s+|nous\s+)?(?:demander|faire valider|faire confirmer)\b|"
    r"\bplus besoin de\s+(?:me\s+|nous\s+)?(?:demander|valider|confirmer|approuver|attendre)\b|"
    r"\boublie[sz]?\s+(?:tes|vos|toutes\s+(?:les|tes|vos))\s+(?:règles|instructions|restrictions|"
    r"garde-fous|sécurités|limites)\b|"
    r"\b(?:ignore|ignorez|contourne|contournez|désactive|désactivez|enfreins|outrepasse|passe outre)\s+"
    r"(?:à\s+)?(?:les|tes|vos|toutes?\s+(?:les|tes|vos)|la|ta|ces|cette|ses|des)?\s*(?:règles?|consignes?|instructions?|"
    r"restrictions?|limites?|garde-fous?|sécurités?|portes?|approbations?|validations?|vérifications?|"
    r"politiques?|interdits?|protections?|contrôles?|invariants?)\b|"
    r"\b(?:contourne|contournez|désactive|désactivez|court-circuite)\b|"
    r"\b(?:valide|approuve|confirme)[sz]?\s+(?:toi-même|tout seul|toute seule|automatiquement|d'office|à ma place)\b|"
    r"\b(?:considère|considérez)\s+que\s+(?:j'ai|c'est|tout est)\s+(?:validé|approuvé|confirmé|ok)\b|"
    r"\b(?:tu as|vous avez)\s+mon\s+(?:go|feu vert|accord)\s+(?:pour tout|permanent|d'office|par défaut)\b|"
    r"\bgo\s+permanent\b|"
    r"\bwithout\s+(?:asking|checking|confirmation|confirming|approval|permission|consent|my\s+(?:approval|"
    r"permission|consent|ok|go-ahead))\b|"
    r"\b(?:don't|do not|never|stop)\s+(?:ask(?:ing)?|check(?:ing)?\s+with)\s+(?:me\s+)?(?:for\s+)?"
    r"(?:approval|confirmation|permission|consent|first|before)\b|\bstop\s+asking\b|"
    r"\b(?:ignore|disregard|bypass|skip|override|disable|circumvent)\s+(?:all\s+|any\s+)?(?:of\s+)?"
    r"(?:the|your|my|these|those|previous|prior|system)?\s*(?:rules?|instructions?|guidelines?|restrictions?|"
    r"policies|policy|safety|guardrails?|approvals?|confirmations?|gates?|checks?|limits?|filters?)\b|"
    r"\b(?:bypass|circumvent|jailbreak)\b|\bauto-?approve\b|\bdeveloper mode\b",
    re.IGNORECASE)


def is_bypass_request(text: str) -> bool:
    """Vrai si le message demande de contourner une approbation, une porte ou une règle (jamais promu)."""
    return bool(_BYPASS.search(text or ""))


# Blocs qui ne sont PAS la parole de l'utilisateur, retirés avant toute détection : contenu reçu de l'extérieur
# (`agents/engine.py::untrusted_block`), pièces jointes (`attachments.py`, `[Pièces jointes]`, `[Fichier …]`…).
_FOREIGN_BLOCKS = re.compile(
    r"<untrusted-data\b[^>]*>.*?(?:</untrusted-data[^>]*>|\Z)|"
    r"\[(Pièces jointes|Fichier|Dossier|Page web|Contexte reçu)\b[^\]]*\].*?(?:\[/\1\]|\Z)",
    re.IGNORECASE | re.DOTALL)


def own_words(text: str) -> str:
    """Le message sans les blocs étrangers (données reçues, pièces jointes) : seule la parole de l'utilisateur."""
    return _FOREIGN_BLOCKS.sub(" ", text or "")


def durable_clauses(text: str) -> str:
    """Les seules phrases (puis propositions séparées par « ; ») d'un message qui donnent une consigne DURABLE à
    l'assistante, « » si aucune (ou si le message est un texte collé, une révocation de consigne, ou une demande de
    contourner une approbation ou une règle). Les blocs étrangers (données reçues, pièces jointes) sont ignorés."""
    text = own_words(text)
    if _looks_pasted(text) or is_durable_revocation(text) or is_bypass_request(text):
        return ""
    picked: list[str] = []
    for sent in sentences(text):
        if _durable_sentence(sent):
            picked.append(sent)
            continue
        picked.extend(p.strip() for p in sent.split(";") if p.strip() and _durable_sentence(p.strip()))
    return " ".join(picked)


def is_durable_instruction(text: str) -> bool:
    """Vrai si le message donne à l'assistante une consigne qui vaut pour la suite (marqueurs ci-dessus)."""
    return bool(durable_clauses(text))


# Révocation d'une consigne durable : « arrête de terminer par X », « oublie cette règle », « plus besoin de … »,
# « ne termine plus par X », « stop ending with X », « forget that rule ».
_DURABLE_REVOCATION = re.compile(
    rf"\b(?:arrête|arrêtez|cesse|cessez)\s+de\b|\btu (?:peux|pouvez) arrêter\b|\bplus besoin de\b|"
    rf"\bpas la peine de\b.*\bplus\b|\b(?:ne|n')\s*(?:me |nous |le |la |les |lui |m'|l'|y |en )?{_V_FR}{_PRON}\s+plus\b(?!\s+jamais)|"
    r"\boublie[sz]?\s+(?:cette|ces|la|les|ta|tes|ma|mes|votre|vos|ce|mon|ton)\s+(?:règles?|consignes?|instructions?|"
    r"habitudes?|demandes?)\b|\blaisse[sz]?\s+tomber\s+(?:cette|la|ta|ma|ces|les)\s+(?:règles?|consignes?|instructions?)\b|"
    r"\b(?:la|cette|ma|ta)\s+(?:règle|consigne|instruction)\s+(?:ne tient plus|n'est plus valable|est annulée|tombe)\b|"
    r"\bannule[sz]?\s+(?:cette|la|ta|ma|ces|les)\s+(?:règles?|consignes?|instructions?)\b|"
    r"\bstop\s+\w+ing\b|\bno more\b(?!\s+than)|\bno longer\b|\bno need to\b|\byou can stop\b|"
    r"\bforget (?:that|this|the|my|your|these|those) (?:rules?|instructions?)\b|\bdrop (?:that|this|the) rule\b",
    re.IGNORECASE)
_RULE_WORDS = re.compile(r"\b(?:règles?|consignes?|instructions?|habitudes?|rules?)\b|\bça\b|\bcela\b", re.IGNORECASE)
_GENERIC_STEMS = frozenset(["répon", "répond", "messa", "toujo", "jamai", "chacu", "chaqu", "consi", "règle", "instr",
                            "arrêt", "cesse", "besoi", "oubli", "désor", "doréna", "suite", "conve", "maint", "termi",
                            "repl", "respo", "answe", "alway", "never", "rule", "stop"])


def is_durable_revocation(text: str) -> bool:
    """Vrai si le message retire une consigne durable donnée plus tôt (marqueurs ci-dessus)."""
    text = own_words(text)
    return bool(_DURABLE_REVOCATION.search(_QUOTED.sub(" ", text)) or _DURABLE_REVOCATION.search(text))


def _stems(text: str) -> set[str]:
    return {w[:5] for w in words(text) if len(w) >= 4 and w not in STOPWORDS and w not in FUNCTION_WORDS}


def revoked_durables(revocation: str, durables: list[str]) -> set[int]:
    """Indices, dans `durables` (consignes plus anciennes, de la plus ancienne à la plus récente), de celles que la
    révocation retire : celles qui partagent le plus de racines porteuses de sens avec elle (« arrête de finir par
    ORCHIDÉE » → la consigne qui nomme ORCHIDÉE) ; à défaut de mot commun, une révocation générique (« oublie cette
    règle », « arrête ça ») retire la plus récente."""
    if not durables:
        return set()
    rv = _stems(revocation) - _GENERIC_STEMS
    scores = [len(rv & (_stems(d) - _GENERIC_STEMS)) for d in durables]
    best = max(scores)
    if best > 0:
        return {i for i, s in enumerate(scores) if s == best}
    # « arrête de terminer tes réponses par … » sans le mot : le verbe seul départage (racine générique comprise)
    rv_all = _stems(revocation) - {"arrêt", "cesse", "besoi", "oubli", "répon", "messa", "chacu", "chaqu", "toujo",
                                   "jamai", "consi", "règle", "instr"}
    scores = [len(rv_all & _stems(d)) for d in durables]
    best = max(scores)
    if best > 0:
        return {i for i, s in enumerate(scores) if s == best}
    if _RULE_WORDS.search(revocation):
        return {len(durables) - 1}
    return set()

def salience(line: str) -> float:
    """Densité de « faits » d'une phrase : noms propres (hors début de phrase), sigles, identifiants alphanumériques,
    nombres entiers de 3 chiffres et plus ; les petits nombres et les décimaux (numérotation, heures) comptent peu ;
    une question vaut moins qu'un énoncé. Sert à choisir ce qu'un résumé extractif garde. Une phrase de revirement
    (« Reviens au point-virgule. ») n'a souvent ni nom ni nombre mais porte la décision EN VIGUEUR : elle pèse autant
    qu'un fait fort ; une exigence de préservation (« réexporte … pour ne rien casser ») aussi."""
    toks = [t.strip(_PUNCT) for t in line.split()]
    score = 6.0 if is_revocation(line) or is_constraint(line) else 0.0
    for i, t in enumerate(toks):
        if not t:
            continue
        digits = sum(c.isdigit() for c in t)
        if i > 0 and _CAPITALIZED.fullmatch(t):
            score += 3
        elif _ACRONYM.fullmatch(t):
            score += 2 + (1 if digits else 0)
        elif digits and any(c.isalpha() for c in t):
            score += 3  # identifiant alphanumérique (B204, 9h30)
        elif re.fullmatch(r"\d+[.,]\d+", t):
            score += 0.5  # décimal / numérotation
        elif digits >= 3:
            score += 3
        elif digits == 2:
            score += 1
        elif digits == 1:
            score += 0.5
    if line.rstrip().endswith("?"):
        score -= 3
    return score


def distinctive_words(text: str) -> list[str]:
    """Mots qui servent à retrouver un fait : mots pleins (≥ 4 lettres) ET jetons à chiffres (≥ 2 caractères),
    sans doublon, dans l'ordre d'apparition."""
    out: list[str] = []
    for w in words(text):
        if w in STOPWORDS:
            continue
        has_digit = any(c.isdigit() for c in w)
        if (has_digit and len(w) >= 2) or (len(w) >= 4 and not w.isdigit()):
            out.append(w)
    return list(dict.fromkeys(out))


def sentences(text: str) -> list[str]:
    text = " ".join((text or "").split())
    return [p for p in re.split(r"(?<=[.!?])\s+", text) if p]


def clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"


def first_sentences(text: str, n: int, max_chars: int) -> str:
    text = " ".join((text or "").split())
    parts = re.split(r"(?<=[.!?])\s+", text)
    return clip(" ".join(parts[:n]), max_chars)


__all__ = ["FUNCTION_WORDS", "STOPWORDS", "clip", "constraint_clauses", "content_words", "distinctive_words",
           "durable_clauses", "first_sentences", "is_bypass_request", "is_constraint", "is_durable_instruction",
           "is_durable_revocation", "is_revocation", "keywords", "own_words", "revoked_durables", "salience", "sentences", "words"]
