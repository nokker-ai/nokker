"""Pont avec la recherche sémantique (port `SemanticIndex`) : indexation à la clôture, fusion avec le lexical,
tolérance aux pannes, cache par requête, ré-indexation."""
from __future__ import annotations

from nokker.context.consolidate import Consolidator
from nokker.context.semantic import SemanticHit
from nokker.context.types import Episode, SummaryResult

from .harness import make_session
from .test_never_clear import FILLER
from .test_recall_quality import FACTS, TOPICS


class FakeSemantic:
    """Sens simulé : « backups » ≈ « sauvegarde » (ce que le lexical ne peut pas faire)."""

    def __init__(self, fail: bool = False):
        self.indexed: list[tuple[Episode, str]] = []
        self.searches: list[tuple[str, str]] = []
        self.fail = fail

    def index_episode(self, ep, body):
        if self.fail:
            raise RuntimeError("index indisponible")
        self.indexed.append((ep, body))

    def search(self, thread_id, query, k=5):
        self.searches.append((thread_id, query))
        if self.fail:
            raise RuntimeError("recherche indisponible")
        if "backups" in query:
            return [SemanticHit(episode_id=e.id, score=0.8, text="Le serveur de sauvegarde s'appelle Zébulon.")
                    for e, body in self.indexed if "Zébulon" in body][:k]
        return []

    def flush(self, timeout=30.0):
        return True


def _timeline(s):
    for day in range(len(FACTS)):
        s.clock.advance(26 * 3600)
        for k in range(14):
            if k == 5:
                s.say(FACTS[day][0])
            s.say(f"Parlons de {TOPICS[(day + k) % len(TOPICS)]} ; rien de précis à retenir ({day}.{k}).")
    s.clock.advance(3 * 3600)


def test_episodes_are_indexed_at_close_with_all_user_messages(tmp_path):
    s = make_session(tmp_path=tmp_path)
    s.manager.semantic = FakeSemantic()
    _timeline(s)
    eps = s.manager.episodes.list(s.thread, closed_only=True)
    assert len(s.manager.semantic.indexed) == len(eps) >= 3
    bodies = [b for _, b in s.manager.semantic.indexed]
    assert all("## Messages de l'utilisateur" in b for b in bodies)
    assert any("Zébulon" in b for b in bodies)  # le fait du milieu est dans le corps indexé, un message par paragraphe


def test_semantic_hit_fills_the_lexical_gap(tmp_path):
    s = make_session(tmp_path=tmp_path)
    s.manager.semantic = FakeSemantic()
    _timeline(s)
    q = "Quelle est la machine qui fait nos backups ?"
    s.manager.semantic = None
    assert "Zébulon" not in s.manager.relevant_block(s.thread, q, exclude=s.manager.layer3_ids(s.thread))
    s.manager.semantic = FakeSemantic()
    holder = next(e for e in s.manager.episodes.list(s.thread, closed_only=True)
                  if "Zébulon" in s.manager.transcript(e, 20000))
    s.manager.semantic.indexed = [(holder, "Zébulon")]
    block = s.manager.relevant_block(s.thread, q, exclude=s.manager.layer3_ids(s.thread))
    assert "Zébulon" in block or "sauvegarde" in block


def test_semantic_failure_never_breaks_a_turn(tmp_path):
    s = make_session(tmp_path=tmp_path)
    s.manager.semantic = FakeSemantic(fail=True)
    _timeline(s)  # l'indexation échoue à chaque clôture : les tours continuent
    turn = s.say("Quelle est la machine qui fait nos backups ?")
    assert turn.reply and not turn.error
    assert s.manager.relevant_block(s.thread, "Quelle est la machine qui fait nos backups ?") is not None


def test_search_is_cached_per_query_within_a_turn(tmp_path):
    s = make_session(tmp_path=tmp_path)
    s.manager.semantic = FakeSemantic()
    _timeline(s)
    n = len(s.manager.semantic.searches)
    for _ in range(4):  # assemble() repasse à chaque appel modèle du tour
        s.manager.relevant_block(s.thread, "Quelle est la machine qui fait nos backups ?")
    assert len(s.manager.semantic.searches) == n + 1


def test_reindex_and_consolidation_refresh_the_index(tmp_path):
    s = make_session(tmp_path=tmp_path)
    s.manager.semantic = FakeSemantic()
    _timeline(s)
    closed = len(s.manager.episodes.list(s.thread, closed_only=True))
    before = len(s.manager.semantic.indexed)
    assert s.manager.reindex_thread(s.thread) == closed
    assert len(s.manager.semantic.indexed) == before + closed

    class Refiner:
        def summarize(self, turns, previous=None):
            return SummaryResult(title="T", summary="Résumé raffiné", keywords=[], facts=[], model="m")

    Consolidator(s.manager, refiner=Refiner()).run_once()
    assert len(s.manager.semantic.indexed) > before + closed  # chaque épisode raffiné est ré-indexé


def test_filler_only_thread_returns_nothing_semantic(tmp_path):
    s = make_session(tmp_path=tmp_path)
    s.manager.semantic = FakeSemantic()
    for i in range(40):
        s.say(f"sujet {i} : {FILLER}")
    assert s.manager.relevant_block(s.thread, "recette de tiramisu") == ""
