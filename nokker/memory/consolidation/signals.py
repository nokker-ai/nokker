"""Ce que la consolidation lit hors de la mémoire longue, pour UN compte, dans le locataire courant (docs/CONSOLIDATION.md
§3) : les épisodes anciens de ses fils (never clear) et les situations d'échec ou de correction (leçons).

Les fils lus sont ceux où le compte est l'expéditeur des tours : jamais le fil d'un autre compte, même d'un même
locataire. Les épisodes épinglés et les épisodes porteurs d'un revirement ou d'une exigence de préservation (la
couche 3 du never clear les montre au modèle comme décisions en vigueur) ne sont jamais résumés."""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from nokker.context.text import is_constraint, is_revocation

log = logging.getLogger("nokker.memory.consolidation.signals")


def _text(turn: Any) -> str:
    inp = getattr(turn, "input", None)
    return getattr(inp, "text", "") or ""


@dataclass
class KernelSignals:
    threads_of: Callable[[str], list[str]]  # compte → ses fils (locataire courant)
    episodes: Any  # EpisodeStore : list(fil, closed_only=True), get_state(fil)
    history_from: Callable[[str, int], list[Any]]  # (fil, premier tour) → tours
    recent_turns: Callable[[str, float], list[tuple[str, int]]]  # (compte, depuis) → [(fil, tour)]
    refusals: Callable[[list[str], float], list[dict[str, Any]]] | None = None  # (fils, depuis) → cartes

    def old_episodes(self, account_id: str, cutoff: float) -> Iterator[tuple[str, list[Any]]]:
        for thread_id in self.threads_of(account_id):
            try:
                st = self.episodes.get_state(thread_id)
                pinned = set(getattr(st, "pinned_episodes", None) or [])
                eps = [e for e in self.episodes.list(thread_id, closed_only=True)
                       if e.closed_at is not None and e.closed_at < cutoff and e.id not in pinned]
                if not eps:
                    continue
                eps.sort(key=lambda e: e.seq)
                turns = {t.index: t for t in self.history_from(thread_id, eps[0].first_turn)}
                keep = []
                for e in eps:
                    last = e.last_turn if e.last_turn is not None else e.first_turn
                    texts = [_text(turns[i]) for i in range(e.first_turn, last + 1) if i in turns]
                    if any(is_revocation(t) or is_constraint(t) for t in texts):
                        continue  # couche 3 : décision ou exigence en vigueur, jamais condensée
                    keep.append(e)
                if keep:
                    yield thread_id, keep
            except Exception:
                log.warning("consolidation : épisodes du fil illisibles", exc_info=True)

    def situations(self, account_id: str, since: float) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        by_thread: dict[str, list[int]] = {}
        for thread_id, idx in self.recent_turns(account_id, since):
            by_thread.setdefault(thread_id, []).append(idx)
        for thread_id, idxs in by_thread.items():
            try:
                turns = {t.index: t for t in self.history_from(thread_id, max(0, min(idxs) - 1))}
            except Exception:
                log.warning("consolidation : tours du fil illisibles", exc_info=True)
                continue
            for i in sorted(idxs):
                t = turns.get(i)
                if t is None or getattr(t, "sender", None) != account_id or not is_revocation(_text(t)):
                    continue
                prev = turns.get(i - 1)
                out.append({"source": "correction", "key": f"c:{thread_id}:{i}", "thread_id": thread_id, "turn": i,
                            "before": getattr(prev, "reply", "") if prev is not None else "", "user": _text(t),
                            "after": getattr(t, "reply", "") or ""})
        if self.refusals is not None and by_thread:
            try:
                for r in self.refusals(sorted(by_thread), since):
                    out.append({"source": "refused" if r["status"] == "rejected" else "failed",
                                "key": f"a:{r['id']}", "thread_id": r["thread_id"], "turn": r.get("turn_index"),
                                "tool": r.get("tool_name", ""),
                                "reason": (r.get("error") if r["status"] == "failed" else r.get("reason")) or ""})
            except Exception:
                log.warning("consolidation : cartes illisibles", exc_info=True)
        return out


def pg_signal_fns(dsn: str) -> dict[str, Callable[..., Any]]:
    """Requêtes sous la RLS du locataire courant (`store/conn.py::connect`) ; filtrées en plus par l'expéditeur."""
    from nokker.store.conn import connect

    def threads_of(account_id: str) -> list[str]:
        with connect(dsn) as c:
            return [r[0] for r in c.execute("SELECT DISTINCT thread_id FROM turns WHERE turn->>'sender' = %s "
                                            "ORDER BY thread_id LIMIT 500", (account_id,)).fetchall()]

    def recent_turns(account_id: str, since: float) -> list[tuple[str, int]]:
        with connect(dsn) as c:
            return [(r[0], int(r[1])) for r in c.execute(
                "SELECT thread_id, turn_index FROM turns WHERE turn->>'sender' = %s AND created_at >= to_timestamp(%s) "
                "ORDER BY created_at LIMIT 2000", (account_id, since)).fetchall()]

    def refusals(threads: list[str], since: float) -> list[dict[str, Any]]:
        with connect(dsn) as c:
            if c.execute("SELECT to_regclass('approvals')").fetchone()[0] is None:
                return []
            rows = c.execute("SELECT id, thread_id, turn_index, tool_name, reason, status, error FROM approvals "
                             "WHERE thread_id = ANY(%s) AND status IN ('rejected', 'failed') "
                             "AND COALESCE(decided_at, created_at) >= %s ORDER BY created_at LIMIT 200",
                             (threads, since)).fetchall()
        return [dict(zip(("id", "thread_id", "turn_index", "tool_name", "reason", "status", "error"), r, strict=True))
                for r in rows]

    return {"threads_of": threads_of, "recent_turns": recent_turns, "refusals": refusals}


__all__ = ["KernelSignals", "pg_signal_fns"]
