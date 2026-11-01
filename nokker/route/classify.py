"""Classe de tâche d'un tour (X8, docs/LOKKAL.md §7.17 ; PLAN-P1 §2.6, chantier C-2) : trivial, conversation ou code.

Deux étages, le premier toujours :

  1. **règles** (aucun appel, aucune latence) : une politesse seule est triviale ; un bloc de code, une trace d'erreur,
     un chemin de fichier source ou un verbe de code (« corrige le bug », « ajoute un test ») est du code ; une suite
     courte d'un tour de code (« et les tests ? », « vas-y ») reste du code. Une règle qui reconnaît sa forme est
     **sûre** ; sinon le tour est une conversation **non sûre** ;
  2. **modèle** (optionnel, `route.classifier: model`) : seulement pour un tour non sûr, une question à un jeton posée
     au petit palier (A trivial, B conversation, C code). Un amont qui tombe ou qui répond hors des options : la règle
     garde la main.

Doctrine de sûreté : un GO ne part JAMAIS au petit palier. « oui », « ok », « vas-y », « d'accord », « parfait » seuls
peuvent répondre à une question de l'agent (« je supprime ? ») : ce sont des suites du tour précédent, pas des
politesses. Le trivial exige un mot de politesse (merci, bonjour…) et aucun mot de GO.
"""
from __future__ import annotations

import enum
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger("nokker.route.classify")


class TaskClass(str, enum.Enum):
    TRIVIAL = "trivial"  # petit palier
    CONVERSATION = "conversation"  # Ship
    CODE = "code"  # Deep : écrire ou corriger du code


@dataclass(frozen=True)
class Classified:
    task: TaskClass
    reason: str
    sure: bool = True  # False : le modèle de classe peut trancher (étage 2)


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower().replace("’", "'")
    return re.sub(r"\s+", " ", t).strip()


# Mots de politesse : l'un d'eux est NÉCESSAIRE au trivial. Les mots de petite conversation (« ça va ? », « et
# toi ? ») et de liaison (« c'est », « a lot ») ne suffisent pas seuls : il faut un mot du noyau ou une formule de
# `_SMALLTALK`.
_COURTESY = {"merci", "mercii", "thanks", "thank", "thx", "bonjour", "bonsoir", "salut", "hello", "hi", "hey",
             "coucou", "bravo", "ciao", "bye", "bisous", "cordialement", "bonne", "nuit", "journée", "soirée", "à",
             "plus", "+", "a+", "beaucoup", "you", "much", "bien", "joué", "nickel", "super", "top", "génial", "cool",
             "parfait", "excellent", "ok", "okay", "très", "vraiment", "encore", "mille", "fois", "😊", "🙏", "👍",
             "🙂", "❤️", "c'est", "a", "lot", "so", "semaine", "week-end", "weekend", "ça", "va", "comment", "vas",
             "tu", "toi", "et", "aujourd'hui", "how", "are", "doing", "good", "morning", "evening", "night"}
_COURTESY_CORE = {"merci", "mercii", "thanks", "thank", "thx", "bonjour", "bonsoir", "salut", "hello", "hi", "hey",
                  "coucou", "bravo", "ciao", "bye", "bisous", "nuit", "soirée", "journée", "semaine", "week-end",
                  "weekend", "morning", "evening", "🙏"}
# Petite conversation sans demande : trivial au même titre qu'une politesse (banc bench/route, 01.11.2026).
_SMALLTALK = re.compile(r"\b(ça va|ca va|comment vas[- ]tu|how are you)\b")
# Mots de GO ou de suite : jamais triviaux, même avec un merci (« merci, vas-y »).
# Formes conjuguées comprises (« tu peux le supprimer », « envoyez ») : le 35B en classeur a rendu « merci, tu peux le
# supprimer » trivial le 01.11.2026 (banc bench/route), la garde ne voyait que « supprime ».
_GO_WORDS = re.compile(r"\b(oui|ouais|yes|go|vas[- ]?y|fais[- ]le|fais|lance\w*|continue\w*|valide\w*|approuve\w*|"
                       r"confirme\w*|d'accord|dac|ok pour|on y va|allons[- ]y|c'est parti|envo[iy]\w*|supprim\w*|"
                       r"efface\w*|install\w*|pousse\w*|push|merge|publie\w*|paye?\w*|vire\w*|transf[eè]re\w*)\b")

_CODE_FENCE = re.compile(r"```")
_TRACE = re.compile(r"traceback \(most recent call last\)|\b(error|exception|erreur)\b.*\bline \d+|"
                    r"\bat .+:\d+:\d+\)|\bsegmentation fault\b|\bpanicked at\b", re.IGNORECASE)
# Fichier source : à lui seul, un signe de code. Un document (html, json, yaml…) n'en est un qu'avec une demande de
# code ou dans la suite d'un tour de code (« résume rapport.html » est une conversation).
_SOURCE_FILE = re.compile(r"\b[\w./-]+\.(py|ts|tsx|js|jsx|mjs|go|rs|java|kt|c|cc|cpp|h|hpp|rb|php|cs|swift|sh|sql|"
                          r"vue|svelte|scss)\b")
_DOC_FILE = re.compile(r"\b[\w./-]+\.(toml|ya?ml|json|css|html)\b")
# Emplois de « code », « fonction », « classe »… hors programmation : retirés avant de chercher une demande de code
# (banc bench/route : 13 conversations sur 38 partaient en Deep, dont « code postal », « en fonction de »).
_NOT_CODE = re.compile(
    r"\b(qr ?code|code[- ]barres?|code (postal|promo\w*|pin|secret|d'accès|vestimentaire|civil|pénal|du travail|"
    r"de (vérification|confirmation|sécurité|réduction|parrainage|la route|conduite|réservation|suivi|couleur))|"
    r"(en|selon la|la|une) fonction (de|du|des|d')|fonction publique|"
    r"(de|en|sa|ma|ta|leur|même) classe\b|classe (de|d'|économique|affaires|moyenne)|"
    r"(partie|part|rémunération|salaire|taux) variable|variable (de|du) (salaire|rémunération))", re.IGNORECASE)
# Verbes et objets du code : une demande d'écrire ou de corriger.
_CODE_ASK = re.compile(
    r"\b(corrige|corriger|fixe?r?|fix|debug(ue)?r?|implémente[rz]?|implement|refactor(ise)?r?|réécri[st]|"
    r"ajoute[rz]? (un |une |des )?(test|fonction|méthode|classe|endpoint|route|option|champ|module)|"
    r"écri[st] (un |une |le |la |des )?(test|fonction|script|classe|code|module|programme)|"
    r"(renomme|supprime|crée|déplace|extrai[st]) (la |le |une |un |cette |ce )?(variable|fonction|méthode|classe)|"
    r"code[rz]?|coding|bug|bogue|régression|stack ?trace|compil|build|lint|typage|type ?check|"
    r"tests? (unitaires?|qui (échoue|casse|passe))|pytest|jest|vitest|npm|pip install|cargo|"
    r"pull request|\bpr\b|commit|diff|patch|merge conflict|conflit de fusion|migration sql|schéma sql|"
    r"function|endpoint|api rest|regex)\b", re.IGNORECASE)
# Objet de code nommé par un identifiant (`slugify`, `parse_date()`, `InvoiceRow`) : la fonction ou la classe d'un
# programme, pas celle d'un bouton ou d'un élève.
_CODE_IDENT = re.compile(r"\b(fonction|méthode|classe|class|variable)\s+`?([a-z]+_[\w]*|[a-z]+[A-Z]\w*|\w+\(\)|"
                         r"[A-Z][a-z]+[A-Z]\w*)")
# Une suite courte d'un tour de code qui parle d'autre chose (mail, agenda…) n'est plus du code.
_OTHER_TOPIC = re.compile(r"\b(mail|courriel|agenda|planning|rendez-vous|rdv|réunion|calendrier|kanban|facture|"
                          r"appel|rappel|message|sms|whatsapp|telegram|anniversaire|courses|vacances)\b")
_CONTINUATION_MAX_CHARS = 200


def classify_rules(text: str, previous: TaskClass | None = None) -> Classified:
    """Étage 1 : règles, sans appel. `previous` = classe du tour précédent du fil (None : premier tour connu)."""
    raw = text or ""
    t = _norm(raw)
    if not t:
        return Classified(TaskClass.CONVERSATION, "message vide", sure=True)
    if _CODE_FENCE.search(raw) or _TRACE.search(raw):
        return Classified(TaskClass.CODE, "bloc de code ou trace d'erreur dans le message")
    words = [w for w in re.split(r"[\s,;:!.?…]+", t) if w]
    courteous = words and all(w in _COURTESY for w in words)
    if (len(t) <= 60 and not _GO_WORDS.search(t) and courteous
            and (any(w in _COURTESY_CORE for w in words) or _SMALLTALK.search(t))):
        return Classified(TaskClass.TRIVIAL, "politesse seule, sans GO")
    coded = _NOT_CODE.sub(" ", t)
    if _SOURCE_FILE.search(coded) or _CODE_ASK.search(coded) or _CODE_IDENT.search(_NOT_CODE.sub(" ", raw)):
        return Classified(TaskClass.CODE, "demande de code (verbe, fichier source ou outil de développement)")
    if previous is TaskClass.CODE and len(t) <= _CONTINUATION_MAX_CHARS and not _OTHER_TOPIC.search(t):
        return Classified(TaskClass.CODE, "suite courte d'un tour de code")
    if previous is TaskClass.CODE and _DOC_FILE.search(t):
        return Classified(TaskClass.CODE, "fichier de configuration dans la suite d'un tour de code")
    return Classified(TaskClass.CONVERSATION, "conversation (aucune règle ne tranche)", sure=False)


# ── Type de tour (palier « nina », docs/LOKKAL.md §7.17 point 13) ──
# Plus fin que la classe : il dit À QUI confier un tour quand un palier spécialisé existe. Les types « nina » par
# défaut (conversation, voix, jugement, approbation, escalade, diagnostic d'infra en LECTURE) sont ceux pour lesquels
# le modèle maison est entraîné ; code, raisonnement long et rédaction restent à Ship/Deep.
TURN_KINDS = ("conversation", "voice", "judgment", "approval", "escalation", "infra_read", "code", "long_reasoning",
              "content")
NINA_KINDS_DEFAULT = ("conversation", "voice", "judgment", "approval", "escalation", "infra_read")
# Réponse à une carte ou à une question de l'agent (« je supprime ? ») : un GO en tête, ou une permission donnée.
_KIND_APPROVAL = re.compile(r"^(oui|ouais|yes|ok|okay|go|vas[- ]?y|fais[- ]le|valide|approuve|confirme|d'accord|dac|"
                            r"on y va|c'est parti|non|refuse)\b|\b(ok,? vas[- ]?y|vas[- ]?y,? \w+ maintenant|"
                            r"tu peux (le |la |les |l')?\w+)\b")
_KIND_CONTENT = re.compile(
    r"\b(rédige[rz]?|écri[st]|draft|traduis|traduire|translate|génère|reformule)\b.{0,40}"
    r"\b(post|article|billet|lettre|mail|courriel|e-mail|texte|discours|présentation|newsletter|annonce|communiqué|"
    r"paragraphe|page|offre|cv|description|fiche)\b|\b(traduis|traduire|translate)\b", re.IGNORECASE)
_KIND_REASONING = re.compile(
    r"\b(démontre[rz]?|prouve[rz]?|résous|résoudre|étape par étape|step by step|calcule[rz]?|optimise[rz]?|"
    r"compare[rz]? en détail|analyse détaillée|plan détaillé|dimensionne[rz]?)\b", re.IGNORECASE)
_KIND_JUDGMENT = re.compile(
    r"\b(qu'en penses[- ]tu|ton avis|à ton avis|bonne idée|est[- ]ce (une )?arnaque|arnaque|risqué|c'est sûr|"
    r"fiable|je fais bien|vaut[- ]il|faut[- ]il|devrais[- ]je|évalue[rz]?|juge[rz]?)\b", re.IGNORECASE)
_KIND_ESCALATION = re.compile(
    r"\b(escalade[rz]?|transmets|transmettre|un humain|une personne|l'équipe|tva|fiscal\w*|juridique|avocat|"
    r"contrat de travail|licenciement|impôts?)\b", re.IGNORECASE)
_KIND_INFRA = re.compile(
    r"\b(disque|serveur|machine|cpu|ram|mémoire vive|logs?|journal|journaux|service|conteneurs?|pods?|docker|"
    r"kubernetes|alerte|charge|réseau|dns|certificat|uptime|latence|site|base de données|sauvegarde|backup|gpu)\b",
    re.IGNORECASE)
_INFRA_WRITE = re.compile(
    r"\b(redémarre|relance|lance|supprime|efface|installe|déploie|mets? à jour|upgrade|lvextend|resize|agrandis|"
    r"modifie|change|coupe|arrête|stoppe|kill|purge|restaure)\b", re.IGNORECASE)


def turn_kind(text: str, task: TaskClass, channel: str = "") -> str:
    """Type d'un tour (`TURN_KINDS`), par règles, sans appel. `channel == "voice"` (transcription d'un message vocal)
    l'emporte sur la forme ; un GO est une approbation ; le diagnostic d'infra n'est « nina » qu'en lecture seule."""
    if task is TaskClass.CODE:
        return "code"
    t = _norm(text)
    if channel == "voice":
        return "voice"
    if _KIND_APPROVAL.search(t) and len(t) <= 200:
        return "approval"
    if _KIND_CONTENT.search(t):
        return "content"
    if _KIND_REASONING.search(t):
        return "long_reasoning"
    if _KIND_INFRA.search(t) and not _INFRA_WRITE.search(t):
        return "infra_read"
    if _KIND_ESCALATION.search(t):
        return "escalation"
    if _KIND_JUDGMENT.search(t):
        return "judgment"
    return "conversation"


CLASSIFIER_PROMPT = """Classe le DERNIER message de l'utilisateur d'un agent de travail. Réponds par UNE lettre :
A = politesse ou remerciement seul, sans demande (« merci ! », « bonjour »)
B = conversation : question, explication, organisation, rédaction, action sur des outils hors code
C = code : écrire, corriger, tester ou modifier du code ou des fichiers d'un projet

Message précédent de la session (classe {previous}).
Message : {text}
Lettre :"""

_LETTERS = {"A": TaskClass.TRIVIAL, "B": TaskClass.CONVERSATION, "C": TaskClass.CODE}


class ClassifierTransport(Protocol):
    def post(self, body: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class ModelClassifier:
    """Étage 2 : une question à un jeton au petit palier. `max_tokens` > 1 pour un modèle qui raisonne d'abord
    (gpt-oss : le premier jeton est son canal de raisonnement, mesuré le 25.10) — la lettre est alors lue dans le
    texte final."""

    transport: ClassifierTransport
    max_tokens: int = 1
    model_name: str = "small"

    def classify(self, text: str, previous: TaskClass | None) -> TaskClass | None:
        prompt = CLASSIFIER_PROMPT.format(previous=(previous.value if previous else "aucune"), text=(text or "")[:2000])
        body = {"messages": [{"role": "user", "content": prompt}], "max_tokens": self.max_tokens, "temperature": 0}
        try:
            data = self.transport.post(body)
        except Exception as e:  # noqa: BLE001 — un classeur qui tombe ne doit jamais bloquer le tour
            logger.warning("classe de tâche : amont indisponible (%s), règles seules", e)
            return None
        content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        m = re.search(r"\b([ABC])\b", content.upper()) or re.match(r"([ABC])", content.upper())
        return _LETTERS.get(m.group(1)) if m else None


def classify(text: str, previous: TaskClass | None = None, model: ModelClassifier | None = None) -> Classified:
    """Règles, puis le modèle pour un tour non sûr. Le modèle ne fait jamais d'un GO un trivial."""
    ruled = classify_rules(text, previous)
    if ruled.sure or model is None:
        return ruled
    got = model.classify(text, previous)
    if got is None:
        return ruled
    if got is TaskClass.TRIVIAL:
        t = _norm(text)
        words = {w for w in re.split(r"[\s,;:!.?…]+", t) if w}
        # Le modèle ne fait trivial que ce que les règles appellent une politesse (tous les mots en sont) : une demande
        # glissée après un merci (« merci, tu peux le supprimer ») n'est jamais triviale.
        if _GO_WORDS.search(t) or len(t) > 80 or not (words & _COURTESY_CORE) or not words <= _COURTESY:
            # Le modèle ne fait trivial qu'une politesse : sans mot de politesse, ou avec un mot de GO, c'est une suite.
            return Classified(TaskClass.CONVERSATION, "modèle : trivial refusé (pas une politesse seule)")
    return Classified(got, f"modèle de classe ({model.model_name}) : {got.value}")


__all__ = ["CLASSIFIER_PROMPT", "NINA_KINDS_DEFAULT", "TURN_KINDS", "Classified", "ModelClassifier", "TaskClass",
           "classify", "classify_rules", "turn_kind"]
