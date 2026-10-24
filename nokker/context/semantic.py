"""Recherche SÉMANTIQUE des épisodes (docs/NEVER-CLEAR.md §11) : port + repli. La vraie implémentation est le moteur
CortHeXis (`corthexis_index.py`) ; le gestionnaire de contexte ne connaît que ce port, et fonctionne sans (lexical)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nokker.context.types import Episode


@dataclass
class SemanticHit:
    episode_id: str  # ep_…
    score: float  # 0..1, comparable entre requêtes du même fil
    text: str = ""  # extrait le plus proche de la requête
    rerank: float | None = None


class SemanticIndex(Protocol):
    def index_episode(self, ep: Episode, body: str) -> None:
        """Enregistre (ou met à jour) un épisode clos. NE BLOQUE PAS le tour : l'indexation est asynchrone."""

    def search(self, thread_id: str, query: str, k: int = 5) -> list[SemanticHit]:
        """Les épisodes du fil les plus proches de `query`. Liste vide si indisponible — jamais d'exception."""

    def flush(self, timeout: float = 30.0) -> bool:
        """Attend la fin des indexations en cours (tests, consolidation). True si tout est indexé."""


__all__ = ["SemanticHit", "SemanticIndex"]
