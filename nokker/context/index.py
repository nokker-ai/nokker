"""Recherche épisodique LEXICALE (docs/NEVER-CLEAR.md §2) : classe les épisodes clos d'un fil par pertinence au
message courant. Pas d'embedding en P1 (port prévu, §9) : mots-clés + titre + résumé, idf calculé sur le fil.

Deux signaux : le niveau ÉPISODE (sac de mots : titre, mots-clés, résumé) et le niveau LIGNE (les phrases du résumé,
une par une — la co-occurrence « serveur » + « sauvegarde » dans la MÊME phrase départage deux épisodes qui parlent
tous deux de serveurs). Le score est normalisé dans [0, 1]."""
from __future__ import annotations

import math
import re
from collections import Counter

from nokker.context.text import content_words, distinctive_words
from nokker.context.types import Episode

_LINE_SPLIT = re.compile(r"\s*;\s*|(?<=[.!?])\s+|\s*\|\s*")


def query_terms(query: str) -> list[str]:
    q = [w for w in content_words(query)]
    q += [w for w in distinctive_words(query) if any(c.isdigit() for c in w)]
    return list(dict.fromkeys(q))


def summary_lines(ep: Episode) -> list[str]:
    return [ln.strip() for ln in _LINE_SPLIT.split(ep.summary or "") if len(ln.strip()) > 8]


def _doc_counts(ep: Episode) -> Counter[str]:
    c: Counter[str] = Counter()
    for w in content_words(ep.title):
        c[w] += 2
    for w in ep.keywords:
        c[w.lower()] += 2
    for w in content_words(ep.summary):
        c[w] += 1
    return c


def best_lines(ep: Episode, query: str, n: int = 2) -> list[tuple[str, float]]:
    """Les `n` phrases du résumé qui recoupent le mieux la requête (part des termes retrouvés)."""
    q = query_terms(query)
    if not q:
        return []
    out: list[tuple[str, float]] = []
    for ln in summary_lines(ep):
        toks = set(content_words(ln)) | {w for w in distinctive_words(ln)}
        hit = sum(1 for t in q if t in toks)
        if hit:
            out.append((ln, hit / len(q)))
    out.sort(key=lambda p: -p[1])
    return out[:n]


MIN_MASS = 0.9  # idf cumulé minimal retrouvé : un seul terme banal (présent partout) ne suffit pas à citer un épisode


def search(episodes: list[Episode], query: str, k: int = 3, min_score: float = 0.15,
           exclude: set[str] | None = None) -> list[tuple[Episode, float]]:
    q = query_terms(query)
    pool = [e for e in episodes if e.closed_at is not None]
    if not q or not pool:
        return []
    docs = {e.id: _doc_counts(e) for e in pool}
    n = len(pool)
    df = {t: sum(1 for d in docs.values() if t in d) for t in q}
    # Les termes absents de TOUT le fil (« qui », « dois-je »…) ne peuvent rien départager : on les ignore au
    # dénominateur plutôt que de laisser diluer le score d'un seul terme décisif (« novembre »).
    q = [t for t in q if df[t] > 0]
    if not q:
        return []
    idf = {t: math.log(1 + (n + 1) / (1 + df[t])) for t in q}
    total = sum(idf.values()) or 1.0
    scored: list[tuple[Episode, float]] = []
    for e in pool:
        if exclude and e.id in exclude:
            continue
        d = docs[e.id]
        episode_hit = sum(idf[t] * min(1.0, d[t] / 2.0) for t in q if t in d)
        line_hit = 0.0
        for ln in summary_lines(e):
            toks = set(content_words(ln)) | set(distinctive_words(ln))
            line_hit = max(line_hit, sum(idf[t] for t in q if t in toks))
        if max(episode_hit, line_hit) < MIN_MASS:
            continue
        score = 0.4 * (episode_hit / total) + 0.6 * (line_hit / total)
        if score >= min_score:
            scored.append((e, round(score, 4)))
    scored.sort(key=lambda p: (-p[1], -p[0].seq))
    return scored[:k]


__all__ = ["MIN_MASS", "best_lines", "query_terms", "search", "summary_lines"]
