"""Résumé EXTRACTIF et déterministe d'un épisode (docs/NEVER-CLEAR.md §5). Sans aucun appel modèle : il tourne sur
le chemin critique (clôture d'épisode) et sert de repli au résumeur LLM (`summarize_llm.py`). Il ne fait que
recopier et abréger ce que les tours contiennent : il ne peut rien inventer."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from nokker.context.keyitems import key_line
from nokker.context.text import (
    clip,
    distinctive_words,
    first_sentences,
    keywords,
    salience,
    sentences,
)
from nokker.context.tokens import estimate_tokens
from nokker.context.types import SummaryResult
from nokker.core.turn import Message, Turn


def message_chars(m: Message) -> int:
    n = len(m.content or "")
    for tc in m.tool_calls:
        n += len(tc.name) + len(tc.raw_arguments or "") + len(str(tc.arguments or ""))
    return n


def message_tokens(m: Message) -> int:
    """Jetons estimés d'un message (texte, puis arguments d'appels d'outils comptés une fois, en JSON)."""
    n = estimate_tokens(m.content or "")
    for tc in m.tool_calls:
        n += estimate_tokens(tc.name or "") + estimate_tokens(tc.raw_arguments or str(tc.arguments or ""), "json")
    return n


def turn_tokens(turn: Turn) -> int:
    """Jetons estimés (`context/tokens.py`) des tokens bruts d'un tour : entrée, contexte reçu, messages d'outils, réponse."""
    n = estimate_tokens(turn.input.text or "") + sum(estimate_tokens(b) for b in turn.injected)
    return n + sum(message_tokens(m) for m in turn.messages)


def _salient_lines(texts: list[str], budget_chars: int, line_chars: int = 170, latest: bool = False) -> list[str]:
    """Les phrases les plus riches en faits (noms propres, nombres, identifiants), plus la première et la dernière
    pour garder le fil, dans l'ordre chronologique, sous un budget de caractères."""
    cands: list[tuple[int, float, str]] = []
    order = 0
    for t in texts:
        for sent in sentences(t):
            cands.append((order, salience(sent), clip(sent, line_chars)))
            order += 1
    if not cands:
        return []
    chosen = {0, len(cands) - 1}
    used = sum(len(cands[i][2]) + 3 for i in chosen)
    # `latest` : à saillance égale, la phrase la plus RÉCENTE d'abord (une valeur révisée plus tard l'emporte sur l'ancienne)
    for i, sc, text in sorted(cands, key=lambda c: (-c[1], -c[0] if latest else c[0])):
        if sc <= 0 or i in chosen:
            continue
        if used + len(text) + 3 > budget_chars:
            continue
        chosen.add(i)
        used += len(text) + 3
    return [cands[i][2] for i in sorted(chosen)]


def _key_texts(turns: list[Turn]) -> list[str]:
    """Textes d'où tirer les éléments clés, dans l'ordre : message de l'utilisateur, texte et arguments (chaînes) des appels
    d'outils du modèle, réponse. Pas les résultats d'outils (volumineux, pleins d'identifiants sans intérêt)."""
    out: list[str] = []
    for t in turns:
        out.append(t.input.text or "")
        for m in t.messages:
            if m.role != "assistant":
                continue
            out.append(m.content or "")
            for tc in m.tool_calls:
                args = tc.arguments if isinstance(tc.arguments, dict) else {}
                out.extend(v for v in args.values() if isinstance(v, str))
        if t.reply and t.reply not in out[-3:]:
            out.append(t.reply)
    return out


@dataclass
class ExtractiveSummarizer:
    """Garde les phrases riches en faits de l'épisode (pas seulement le début, le milieu et la fin) : un fait planté au
    milieu d'un épisode de remplissage doit survivre au résumé. `keywords` = les mots fréquents d'abord, puis tous
    les mots distinctifs (dont les nombres et identifiants) — c'est ce que la recherche épisodique interroge."""

    max_chars: int = 1300
    title_chars: int = 60
    max_keywords: int = 150
    # Ligne « Éléments clés » (noms de fichiers, identifiants, valeurs avec unité, révisions) et préférence pour la dernière
    # occurrence à saillance égale (`context/keyitems.py`). Faux : le résumé d'avant, mot pour mot.
    key_items: bool = True
    key_items_chars: int = 300

    def summarize(self, turns: list[Turn], previous: str | None = None) -> SummaryResult:
        users = [t.input.text.strip() for t in turns if t.input.text and t.input.text.strip()]
        replies = [t.reply.strip() for t in turns if t.reply and t.reply.strip()]
        tools = Counter(tc.name for t in turns for m in t.messages for tc in m.tool_calls)
        title = clip(first_sentences(users[0], 1, self.title_chars), self.title_chars) if users else "Épisode"
        parts: list[str] = []
        key = key_line(_key_texts(turns), self.key_items_chars) if self.key_items else ""
        budget = self.max_chars - (len(key) + 1 if key else 0)  # la ligne d'éléments clés n'est jamais coupée par le résumé
        if users:
            parts.append("Demandes : " + " ; ".join(_salient_lines(users, int(budget * 0.62), latest=self.key_items)) + ".")
        if tools:
            parts.append("Outils : " + ", ".join(f"{name}×{n}" if n > 1 else name
                                                  for name, n in tools.most_common(6)) + ".")
        if replies:
            parts.append("Réponses : " + " | ".join(_salient_lines(replies[-6:], int(budget * 0.25), 140,
                                                                   latest=self.key_items)))
        summary = clip(" ".join(parts), budget)
        if key:
            summary = f"{summary} {key}"
        frequent = keywords(" ".join(users + replies), 12)
        bag = distinctive_words(" ".join(users))
        kw = list(dict.fromkeys([*frequent, *bag]))[: self.max_keywords]
        return SummaryResult(title=title, summary=summary, keywords=kw, facts=[], model=None)


__all__ = ["ExtractiveSummarizer", "message_chars", "message_tokens", "turn_tokens"]
