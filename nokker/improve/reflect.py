"""Réflexion sur les traces (GEPA, arXiv 2507.19457, §3 « reflective prompt mutation ») : un modèle lit le texte actuel
d'une unité et des échecs réels qui lui sont attribués, et propose UNE nouvelle version. Deux implémentations :

- `ModelReflector` : un vrai modèle (palier `deep` de la config par défaut), message système `REFLECT_SYSTEM` et message
  `REFLECT_USER` ci-dessous — textes vus par un modèle, à transmettre tels quels à la relecture du format ;
- `FakeReflector` : faux modèle déterministe (démo, tests) — pour un outil, il ajoute à la description les mots des
  demandes ratées qui n'y figurent pas encore ; pour le routeur, il bouge un seuil d'un cran.

Le reflecteur ne voit JAMAIS un banc : seulement des traces filtrées (`traces.py`) ; il ne reçoit des bancs que des
scores numériques, à travers le choix du parent (front de Pareto).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.improve.guards import words
from nokker.improve.overlay import ROUTER_KEYS
from nokker.improve.traces import Example

REFLECT_SYSTEM = (
    "Tu améliores un texte du harness d'un agent : description d'outil, mode d'emploi de skill, seuils de routage ou "
    "bloc de prompt. On te donne le texte actuel et des tours réels où l'agent a échoué. Propose UNE nouvelle version "
    "qui aurait évité ces échecs sans en créer d'autres : garde le sens, reste bref, n'invente aucune capacité, ne "
    "recopie ni nom, ni adresse, ni identifiant, ni secret tiré des exemples. Réponds uniquement par la nouvelle "
    "version, au format demandé, sans commentaire.")

REFLECT_USER = (
    "[Texte actuel — {kind} « {key} »]\n{current}\n[/Texte actuel]\n\n"
    "[Échecs observés]\n{examples}\n[/Échecs observés]\n\n"
    "Format de réponse : {format}")

KINDS = {"tools": "description d'outil", "skills": "skill appris", "router": "seuils du routeur",
         "blocks": "bloc de prompt"}
FORMATS = {
    "tools": 'un objet JSON {"description": "…", "parameters": {"champ": "…"}} avec exactement les mêmes champs.',
    "skills": 'un objet JSON {"description": "une ligne", "body": "mode d\'emploi complet en Markdown"}.',
    "router": "un objet JSON avec les mêmes clés ; bornes : {bornes}.",
    "blocks": "le texte complet du bloc, sans balises autour.",
}
MAX_EXAMPLES = 8


def render_value(target: str, value: Any) -> str:
    """Forme texte d'une valeur d'unité (ce que lit le reflecteur, ce que compare le diff et les garde-fous)."""
    if target == "blocks":
        return str(value)
    return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True)


def reflect_messages(target: str, key: str, value: Any, examples: list[Example]) -> list[dict[str, str]]:
    bornes = ", ".join(f"{k} {'booléen' if t is bool else f'{lo}..{hi}'}" for k, (t, lo, hi) in ROUTER_KEYS.items())
    user = REFLECT_USER.format(kind=KINDS[target], key=key, current=render_value(target, value),
                               examples="\n".join(e.render() for e in examples[:MAX_EXAMPLES]) or "- (aucun)",
                               format=FORMATS[target].replace("{bornes}", bornes))
    return [{"role": "system", "content": REFLECT_SYSTEM}, {"role": "user", "content": user}]


def parse_reply(target: str, text: str) -> Any:
    text = (text or "").strip()
    fence = re.match(r"^```(?:json|markdown|md)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if target == "blocks":
        return text
    return json.loads(text)


@dataclass
class Reflection:
    value: Any
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ModelReflector:
    """`complete(messages) -> (texte, jetons d'entrée, jetons de sortie)` — un palier de la config (`cli.py`)."""

    def __init__(self, complete: Callable[[list[dict[str, str]]], tuple[str, int, int]]) -> None:
        self.complete = complete

    def propose(self, target: str, key: str, value: Any, examples: list[Example], round_: int) -> Reflection:
        text, pin, pout = self.complete(reflect_messages(target, key, value, examples))
        return Reflection(parse_reply(target, text), pin, pout)


_STOP = frozenset({
    "avec", "dans", "pour", "mais", "plus", "tout", "tous", "toute", "toutes", "cette", "celle", "celui", "ceux",
    "leur", "leurs", "quel", "quelle", "quels", "quelles", "quoi", "comme", "sont", "être", "avoir", "fait",
    "faire", "peux", "peut", "veux", "veut", "voudrais", "aussi", "alors", "encore", "très", "bien", "merci",
    "bonjour", "moi", "mon", "mes", "ton", "tes", "son", "ses", "notre", "nos", "votre", "vos", "elle", "elles",
    "nous", "vous", "ils", "est", "sur", "par", "une", "des", "les", "aux", "que", "qui", "pas", "ça", "cela",
    "ceci", "demande", "demain", "hier", "aujourd", "jour", "jours", "semaine", "prochaine", "prochain", "dernier",
    "dernière", "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche", "matin", "soir", "après-midi",
    "midi", "est-ce", "j'ai", "reçu", "reçus", "regarde", "retrouve", "cherche", "trouve", "montre",
    "restent"})


_HYPHENATED = re.compile(r"[0-9a-zà-ÿœæ]+(?:-[0-9a-zà-ÿœæ]+)*", re.IGNORECASE)


def _content_words(text: str) -> list[str]:
    """Mots pleins d'une demande (traits d'union gardés : « rendez-vous »)."""
    return [w for w in (m.lower() for m in _HYPHENATED.findall(text or ""))
            if len(w) >= 4 and w not in _STOP and not w.isdigit()]


@dataclass
class FakeReflector:
    """Faux modèle déterministe : aucune requête réseau. Le coût annoncé est celui qu'aurait le même prompt (jetons
    estimés à 3,5 caractères), pour exercer le budget comme en vrai."""

    words_per_round: tuple[int, ...] = (2, 4, 3, 5)
    _seen: Counter = field(default_factory=Counter)

    def propose(self, target: str, key: str, value: Any, examples: list[Example], round_: int) -> Reflection:
        msgs = reflect_messages(target, key, value, examples)
        pin = int(sum(len(m["content"]) for m in msgs) / 3.5)
        self._seen[key] += 1
        k = self.words_per_round[(self._seen[key] - 1) % len(self.words_per_round)]
        if target == "tools":
            have = set(words(value["description"])) | set(words(key.replace("_", " ")))
            freq = Counter(w for e in examples for w in dict.fromkeys(_content_words(e.user_text)) if w not in have)
            picked = [w for w, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]
            new = dict(value)
            if picked:
                new["description"] = value["description"].rstrip(". ") + ". Aussi : " + ", ".join(picked) + "."
            return Reflection(new, pin, int(len(new["description"]) / 3.5))
        if target == "skills":
            have = set(words(value["description"]))
            freq = Counter(w for e in examples for w in dict.fromkeys(_content_words(e.user_text)) if w not in have)
            picked = [w for w, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]
            new = dict(value)
            if picked:
                new["description"] = value["description"].rstrip(". ") + " ; aussi : " + ", ".join(picked)
            return Reflection(new, pin, 40)
        if target == "router":
            new = dict(value)
            step = -1 if self._seen[key] % 2 else 1
            new["deep_tool_failures"] = max(1, min(8, int(value.get("deep_tool_failures", 3)) + step))
            return Reflection(new, pin, 20)
        return Reflection(value, pin, 0)  # blocs : le faux modèle ne réécrit pas un bloc (variante inchangée)


__all__ = [
    "REFLECT_SYSTEM",
    "REFLECT_USER",
    "FakeReflector",
    "ModelReflector",
    "Reflection",
    "parse_reply",
    "reflect_messages",
    "render_value",
]
