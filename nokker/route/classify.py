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


# Mots de politesse : l'un d'eux est NÉCESSAIRE au trivial.
_COURTESY = {"merci", "mercii", "thanks", "thank", "thx", "bonjour", "bonsoir", "salut", "hello", "hi", "hey",
             "coucou", "bravo", "ciao", "bye", "bisous", "cordialement", "bonne", "nuit", "journée", "soirée", "à",
             "plus", "+", "a+", "beaucoup", "you", "much", "bien", "joué", "nickel", "super", "top", "génial", "cool",
             "parfait", "excellent", "ok", "okay", "très", "vraiment", "encore", "mille", "fois", "😊", "🙏", "👍",
             "🙂", "❤️"}
_COURTESY_CORE = {"merci", "mercii", "thanks", "thank", "thx", "bonjour", "bonsoir", "salut", "hello", "hi", "hey",
                  "coucou", "bravo", "ciao", "bye", "bisous", "nuit", "🙏"}
# Mots de GO ou de suite : jamais triviaux, même avec un merci (« merci, vas-y »).
_GO_WORDS = re.compile(r"\b(oui|ouais|yes|go|vas[- ]?y|fais[- ]le|fais|lance|continue|valide|approuve|confirme|"
                       r"d'accord|dac|ok pour|on y va|allons[- ]y|c'est parti|envoie|supprime|installe|pousse|"
                       r"push|merge)\b")

_CODE_FENCE = re.compile(r"```")
_TRACE = re.compile(r"traceback \(most recent call last\)|\b(error|exception|erreur)\b.*\bline \d+|"
                    r"\bat .+:\d+:\d+\)|\bsegmentation fault\b|\bpanicked at\b", re.IGNORECASE)
_SOURCE_FILE = re.compile(r"\b[\w./-]+\.(py|ts|tsx|js|jsx|mjs|go|rs|java|kt|c|cc|cpp|h|hpp|rb|php|cs|swift|sh|sql|"
                          r"vue|svelte|toml|ya?ml|json|css|scss|html)\b")
# Verbes et objets du code : une demande d'écrire ou de corriger.
_CODE_ASK = re.compile(
    r"\b(corrige|corriger|fixe?r?|fix|debug(ue)?r?|implémente[rz]?|implement|refactor(ise)?r?|réécri[st]|"
    r"ajoute[rz]? (un |une |des )?(test|fonction|méthode|classe|endpoint|route|option|champ|module)|"
    r"écri[st] (un |une |le |la |des )?(test|fonction|script|classe|code|module|programme)|"
    r"code[rz]?|coding|bug|bogue|régression|stack ?trace|compil|build|lint|typage|type ?check|"
    r"tests? (unitaires?|qui (échoue|casse|passe))|pytest|jest|vitest|npm|pip install|cargo|"
    r"pull request|\bpr\b|commit|diff|patch|merge conflict|conflit de fusion|migration sql|schéma sql|"
    r"fonction|function|méthode|classe|class|variable|endpoint|api rest|regex)\b", re.IGNORECASE)
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
    if (len(t) <= 60 and not _GO_WORDS.search(t) and words and all(w in _COURTESY for w in words)
            and any(w in _COURTESY_CORE for w in words)):
        return Classified(TaskClass.TRIVIAL, "politesse seule, sans GO")
    if _SOURCE_FILE.search(t) or _CODE_ASK.search(t):
        return Classified(TaskClass.CODE, "demande de code (verbe, fichier source ou outil de développement)")
    if previous is TaskClass.CODE and len(t) <= _CONTINUATION_MAX_CHARS:
        return Classified(TaskClass.CODE, "suite courte d'un tour de code")
    return Classified(TaskClass.CONVERSATION, "conversation (aucune règle ne tranche)", sure=False)


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
        words = set(re.split(r"[\s,;:!.?…]+", t))
        if _GO_WORDS.search(t) or len(t) > 80 or not (words & _COURTESY_CORE):
            # Le modèle ne fait trivial qu'une politesse : sans mot de politesse, ou avec un mot de GO, c'est une suite.
            return Classified(TaskClass.CONVERSATION, "modèle : trivial refusé (pas une politesse seule)")
    return Classified(got, f"modèle de classe ({model.model_name}) : {got.value}")


__all__ = ["CLASSIFIER_PROMPT", "Classified", "ModelClassifier", "TaskClass", "classify", "classify_rules"]
