"""Consolidation (docs/NEVER-CLEAR.md §5) — hors chemin critique, planifiable (timer nocturne) et appelable à la
demande : ferme les épisodes inactifs, raffine les résumés avec un résumeur LLM, construit les résumés de jour
(hiérarchie tour → épisode → jour) et propose les faits durables à la quarantaine de CortHeXis (jamais d'écriture
directe en mémoire de prod, D6)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from nokker.context.manager import ContextManager, day_of
from nokker.context.text import clip, first_sentences
from nokker.context.types import DaySummary, Summarizer, estimate_tokens, new_day_id

logger = logging.getLogger("nokker.context.consolidate")


@dataclass
class ConsolidationReport:
    closed: list[str] = field(default_factory=list)  # épisodes clos pour inactivité
    refined: list[str] = field(default_factory=list)  # épisodes dont le résumé LLM a remplacé l'extractif
    days: list[str] = field(default_factory=list)  # résumés de jour (re)construits
    facts_proposed: int = 0


@dataclass
class Consolidator:
    manager: ContextManager
    refiner: Summarizer | None = None  # LLMSummarizer ; None = pas de raffinement
    memory: Any = None  # MemoryPort : `propose(thread_id, facts)` (quarantaine)
    day_summary_chars: int = 700

    def run_once(self, now: float | None = None, threads: list[str] | None = None) -> ConsolidationReport:
        mgr = self.manager
        now = mgr.clock() if now is None else now
        report = ConsolidationReport()
        targets = list(dict.fromkeys([*(threads or []), *mgr.episodes.threads_with_open_episode()]))
        for thread_id in targets:
            try:
                self._thread(thread_id, now, report)
            except Exception:
                logger.exception("consolidation du fil %s en échec", thread_id)
        return report

    def _thread(self, thread_id: str, now: float, report: ConsolidationReport) -> None:
        mgr = self.manager
        st = mgr.state(thread_id)
        if st.episode_id and st.last_turn_at and now - st.last_turn_at >= mgr.budgets.silence_gap_s:
            ep_id = mgr.new_episode(thread_id, "silence")
            if ep_id:
                report.closed.append(ep_id)
        if self.refiner is not None:
            for ep in mgr.episodes.list(thread_id, closed_only=True):
                if not ep.refined:
                    self._refine(thread_id, ep, report)
        self._days(thread_id, now, report)

    def _refine(self, thread_id: str, ep: Any, report: ConsolidationReport) -> None:
        mgr = self.manager
        last = ep.last_turn if ep.last_turn is not None else ep.first_turn
        turns = [t for t in mgr._history_from(thread_id, ep.first_turn) if t.index <= last]
        res = self.refiner.summarize(turns, previous=ep.summary)
        if res.model is None:  # repli extractif : on garde l'existant, on réessaiera à la prochaine consolidation
            return
        ep.title = mgr._clean(res.title)[:120] or ep.title
        ep.summary = mgr._clean(res.summary) or ep.summary
        ep.summary_tokens = estimate_tokens(ep.summary)
        ep.keywords = [mgr._clean(k) for k in res.keywords][:16] or ep.keywords
        ep.refined = True
        mgr.episodes.save(ep)
        mgr.reindex_episode(ep.id)  # le résumé raffiné repart dans l'index sémantique
        report.refined.append(ep.id)
        if res.facts and self.memory is not None:
            self.memory.propose(thread_id, [mgr._clean(f) for f in res.facts])
            report.facts_proposed += len(res.facts)

    def _days(self, thread_id: str, now: float, report: ConsolidationReport) -> None:
        mgr = self.manager
        today = day_of(now)
        by_day: dict[str, list[Any]] = {}
        for ep in mgr.episodes.list(thread_id, closed_only=True):
            if ep.day and ep.day < today:
                by_day.setdefault(ep.day, []).append(ep)
        for day, eps in sorted(by_day.items()):
            ids = [e.id for e in eps]
            known = mgr.episodes.get_day(thread_id, day)
            if known is not None and known.episode_ids == ids:
                continue
            parts = [f"{e.title} : {first_sentences(e.summary, 1, 160)}" for e in eps]
            summary = clip(" | ".join(parts), self.day_summary_chars)
            mgr.episodes.save_day(DaySummary(id=known.id if known else new_day_id(), thread_id=thread_id, day=day,
                                             summary=summary, episode_ids=ids, tokens=estimate_tokens(summary),
                                             created_at=mgr.clock()))
            report.days.append(day)


__all__ = ["ConsolidationReport", "Consolidator"]
