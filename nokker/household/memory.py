"""Mémoire commune du foyer dans les tours (lot X23, docs/LOKKAL.md §7.29).

Les notes du foyer sont les notes de projet de l'équipe (`team_notes` du projet `household` : `rule`, `feedback`,
`fact`, au format des notes CortHeXis de X1 — nom, titre, texte, liens), écrites depuis l'app par les membres
(`PUT /v1/household/notes/{nom}`) avec les règles des notes d'équipe : caviardage à l'arrivée, la dernière reçue gagne,
la version remplacée en quarantaine relue par le payeur. Elles sont rappelées dans le `[Rappel mémoire]` du tour de
CHAQUE membre actif (port `MemoryPort`, via `TenantMemory.tenant_memory`), par pertinence lexicale (mots de
l'index des épisodes, nokker/context/index.py) : chez nokker.ai, un locataire n'a pas d'index CortHeXis à lui (docs/TENANCY.md
§5), et la mémoire de profil de chacun (portée `user`) ne passe jamais par le foyer.

Cache par foyer, tenu à jour par le curseur des notes (`seq`) : un tour relit seulement ce qui a changé depuis le
précédent (pierres tombales comprises).
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any

from nokker.context.index import query_terms
from nokker.context.text import content_words, distinctive_words
from nokker.core.ports import Recall, ToolContext, ToolSpec
from nokker.core.turn import ToolResult

MIN_SCORE = 0.3
# idf cumulé minimal retrouvé. Plus bas que celui des épisodes d'un fil (0,9) : un foyer a peu de notes, et un seul mot
# décisif (« dentiste ») doit suffire quand une seule note le porte.
MIN_MASS = 0.6
PREFIX = "foyer:"


def _tokens(*texts: str) -> set[str]:
    out: set[str] = set()
    for t in texts:
        out |= set(content_words(t)) | {w for w in distinctive_words(t)}
    return out


def rank(notes: list[Any], query: str, k: int, min_score: float = MIN_SCORE) -> list[tuple[Any, float]]:
    """Notes classées par pertinence lexicale : part de l'idf des termes de la requête retrouvés dans le titre ou le
    texte (les termes absents de toutes les notes ne départagent rien et sont ignorés), titre compté double."""
    q = query_terms(query)
    if not q or not notes:
        return []
    docs = [(n, _tokens(n.text), _tokens(n.title)) for n in notes]
    df = {t: sum(1 for _, body, title in docs if t in body or t in title) for t in q}
    q = [t for t in q if df[t] > 0]
    if not q:
        return []
    idf = {t: math.log(1 + (len(docs) + 1) / (1 + df[t])) for t in q}
    total = sum(idf.values())
    out = []
    for n, body, title in docs:
        mass = sum(idf[t] for t in q if t in body or t in title)
        if mass < MIN_MASS:
            continue
        bonus = sum(idf[t] for t in q if t in title) / (2 * total)
        score = round(min(1.0, mass / total + bonus), 4)
        if score >= min_score:
            out.append((n, score))
    out.sort(key=lambda p: (-p[1], -p[0].seq))
    return out[:k]


@dataclass
class HouseholdRecall:
    household: Any  # nokker.household.service.Household
    min_score: float = MIN_SCORE
    _cache: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _notes(self, acc: Any, p: Any) -> list[Any]:
        with self._lock:
            cursor, notes = self._cache.get(p.id, (0, {}))
            notes = dict(notes)
        while True:
            rows = self.household.store.notes_after(acc, p, cursor, 500)
            for n in rows:
                if n.deleted:
                    notes.pop(n.name, None)
                else:
                    notes[n.name] = n
                cursor = max(cursor, n.seq)
            if len(rows) < 500:
                break
        with self._lock:
            self._cache[p.id] = (cursor, notes)
        return list(notes.values())

    def recall_for_account(self, account_id: str, query: str, k: int = 5) -> list[Recall]:
        if not query.strip():
            return []
        found = self.household.membership(account_id)
        if found is None:
            return []
        acc, p, row = found
        if row.minor:
            st = self.household.minor_state(account_id)
            if st is None or not st.active:
                return []
        out = []
        for n, score in rank(self._notes(acc, p), query, k, self.min_score):
            out.append(Recall(note=PREFIX + n.name, text=f"[Mémoire du foyer] {n.title} — {n.text}", score=score))
        return out

    def for_tenant(self, tenant_id: str) -> _MemberMemory | None:
        """Port mémoire d'un locataire testeur : celui de son compte s'il est membre d'un foyer, sinon None."""
        tenancy = self.household.team.tenancy
        acc = tenancy.account_for_tenant(tenant_id) if tenancy is not None else None
        if acc is None or self.household.membership(acc.account_id) is None:
            return None
        return _MemberMemory(self, acc.account_id)


@dataclass
class _MemberMemory:
    """`MemoryPort` du tour d'un membre : le rappel des notes du foyer ; rien d'écrit, aucun outil."""

    source: HouseholdRecall
    account_id: str

    def recall(self, query: str, k: int = 5) -> list[Recall]:
        return self.source.recall_for_account(self.account_id, query, k)

    def recall_for(self, thread_id: str, query: str, k: int = 5) -> list[Recall]:
        return self.recall(query, k)

    def propose(self, thread_id: str, facts: list[str]) -> None:
        return None

    def specs(self) -> list[ToolSpec]:
        return []

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)


__all__ = ["HouseholdRecall"]
