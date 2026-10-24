"""Consolidation (fermeture des fils inactifs, raffinement, résumés de jour, faits en quarantaine) et pont bulles."""
from __future__ import annotations

from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.bubbles.types import BubbleError
from nokker.context.consolidate import Consolidator
from nokker.context.types import SummaryResult
from nokker.threads.memory import InMemoryThreads

from .harness import make_session
from .test_never_clear import FILLER


class FakeRefiner:
    def __init__(self, ok: bool = True):
        self.ok, self.calls = ok, 0

    def summarize(self, turns, previous=None):
        self.calls += 1
        if not self.ok:
            return SummaryResult(title="repli", summary="repli extractif", model=None)
        return SummaryResult(title="Migration du disque", summary="Résumé raffiné (LLM).", keywords=["disque"],
                             facts=["le serveur s'appelle Zébulon"], model="fake-llm")


class FakeMemory:
    def __init__(self):
        self.proposed: list[tuple[str, list[str]]] = []

    def propose(self, thread_id, facts):
        self.proposed.append((thread_id, facts))


def _fill(s, n=40):
    for i in range(n):
        s.say(f"sujet {i} : {FILLER}")
    # Un tour court en dernier : l'épisode courant reste ouvert quel que soit le point où le seuil de la fenêtre a
    # clos le précédent (depuis l'en-tête rejoué, 20.10.2026, les tours pèsent plus et le seuil tombe ailleurs).
    s.say("ok")


def test_consolidation_closes_idle_thread_and_builds_day_summaries(tmp_path):
    s = make_session(tmp_path=tmp_path)
    _fill(s)
    s.clock.advance(2 * 86400)
    rep = Consolidator(s.manager).run_once()
    assert rep.closed  # l'épisode ouvert, inactif depuis 2 jours, est clos
    assert rep.days
    days = s.manager.episodes.list_days(s.thread)
    assert days and all(d.summary and d.episode_ids for d in days)
    # idempotent : un second passage ne reconstruit rien
    assert Consolidator(s.manager).run_once().days == []
    block = s.manager.memory_block(s.thread)
    assert "dy_" in block or "épisode(s) plus ancien" in block
    assert days[0].id in s.manager.recall(s.thread, ref=days[0].id)


def test_refiner_replaces_extractive_summary_and_proposes_facts(tmp_path):
    s = make_session(tmp_path=tmp_path)
    _fill(s)
    mem = FakeMemory()
    rep = Consolidator(s.manager, refiner=FakeRefiner(), memory=mem).run_once()
    assert rep.refined and rep.facts_proposed == len(rep.refined)
    ep = s.manager.episodes.get(rep.refined[0])
    assert ep.refined and ep.title == "Migration du disque" and "raffiné" in ep.summary
    assert mem.proposed and mem.proposed[0][1] == ["le serveur s'appelle Zébulon"]
    assert Consolidator(s.manager, refiner=FakeRefiner(), memory=mem).run_once().refined == []  # déjà raffinés


def test_failed_refiner_keeps_extractive_and_retries_later(tmp_path):
    s = make_session(tmp_path=tmp_path)
    _fill(s)
    refiner = FakeRefiner(ok=False)
    rep = Consolidator(s.manager, refiner=refiner).run_once()
    assert not rep.refined and refiner.calls > 0
    assert all(not e.refined and e.summary != "repli extractif" for e in s.manager.episodes.list(s.thread, closed_only=True))


def _hub(manager):
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), known_users=frozenset({"alice"}))
    hub.context_manager = manager
    manager.on_episode_closed = hub.create_episode_bubble
    return hub


def test_episode_bubble_is_created_and_reload_archive_actions_work(tmp_path):
    s = make_session(tmp_path=tmp_path, thread="th_a")
    hub = _hub(s.manager)
    hub.threads.create("Infra", "alice", thread_id="th_a")
    _fill(s, 80)  # assez d'épisodes pour que le premier ne soit plus parmi les « récents »
    eps = s.manager.episodes.list("th_a", closed_only=True)
    assert len(eps) >= 4
    bubbles = [b for b, _shared in hub.list_bubbles("th_a", "alice", include_closed=True)]
    ep_bubbles = [b for b in bubbles if b.kind == "episode"]
    assert len(ep_bubbles) == len(eps) >= 2
    b = ep_bubbles[0]
    assert b.state["episode_id"] == eps[0].id and b.status == "done"
    assert {a["id"] for a in b.actions} == {"reload", "archive"}
    assert hub.action(b.id, "alice", "reload") == {"pinned": True, "episode_id": eps[0].id}
    assert eps[0].id in s.manager.memory_block("th_a")
    assert hub.action(b.id, "alice", "archive")["closed"] is True
    assert eps[0].id not in s.manager.memory_block("th_a")


def test_client_cannot_create_episode_bubble(tmp_path):
    s = make_session(tmp_path=tmp_path, thread="th_a")
    hub = _hub(s.manager)
    hub.threads.create("Infra", "alice", thread_id="th_a")
    try:
        hub.create_bubble("th_a", "alice", kind="episode", title="x")
    except BubbleError:
        return
    raise AssertionError("un client ne crée pas de bulle episode")


def test_transfer_episode_bubble_carries_the_memory_to_the_other_thread(tmp_path):
    s = make_session(tmp_path=tmp_path, thread="th_a")
    hub = _hub(s.manager)
    hub.threads.create("Infra", "alice", thread_id="th_a")
    hub.threads.create("Compta", "alice", thread_id="th_b")
    s.say("Le code du coffre est 4711.")
    for i in range(40):
        s.say(f"sujet {i} : {FILLER}")
    ep = s.manager.episodes.list("th_a", closed_only=True)[0]
    bubble = next(b for b, _shared in hub.list_bubbles("th_a", "alice", include_closed=True)
                  if b.kind == "episode" and b.state["episode_id"] == ep.id)
    out = hub.transfer(bubble.id, "alice", "th_b", "copy")
    block = hub.threads.drain_context("th_b")[0]
    assert "épisode" in block and "recall_context(\"rs_" in block
    rs_id = block.split('recall_context("')[1].split('"')[0]
    assert "4711" in s.manager.recall("th_b", ref=rs_id, max_chars=8000)  # la mémoire a voyagé
    assert "inconnu" in s.manager.recall("th_a", ref=rs_id)  # et reste cloisonnée par fil
    assert out["bubble"].thread_id == "th_b"
