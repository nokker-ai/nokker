"""Lecture « Qui fait quoi » (docs/WHO-DOES-WHAT.md §2) : pour UN compte, dans SON locataire (l'appelant est déjà sous
`tenant_scope`), la liste des fils actifs, sessions de code, sous-agents, objectifs, agents Crew et agents de la salle
Maison, avec un statut dérivé de l'état existant. Lecture seule, aucune table, aucun texte vu par le modèle."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from nokker.presence.model import AWAITING, BLOCKED, IDLE, PAUSED, STATUSES, WORKING, flat, rank

logger = logging.getLogger("nokker.presence")
GOAL_LOOKUPS = 60  # fils les plus récents dont on cherche l'objectif


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


def act(code: str, **data: Any) -> dict[str, Any]:
    """Activité d'une ligne : un CODE stable et des valeurs brutes (outil, extrait, objectif). Le client écrit la phrase
    dans sa langue ; le noyau n'écrit aucun libellé."""
    out: dict[str, Any] = {"code": code}
    for k, v in data.items():
        if v:
            out[k] = flat(v) if k == "text" else v
    return out


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 — une source en panne ne vide pas la vue
        logger.warning("présence : source illisible", exc_info=True)
        return default


@dataclass
class PresenceService:
    hub: Any = None  # BubbleHub : fils du compte (`list_threads`)
    approvals: Any = None  # BaseApprovals : cartes en attente (`pending`)
    goals: Any = None  # GoalService : `current(thread_id)`
    code: Any = None  # CodeSessions : `store.list_user`
    agents: Any = None  # AgentsHub : Crew
    room: Any = None  # HouseRoom (salle Maison) ou None
    tracker: Any = None  # ActivityTracker
    entries_cap: int = 100

    def view(self, account_id: str) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        pending: dict[str, Any] = {}
        if self.approvals is not None:
            for a in _safe(lambda: self.approvals.pending(account_id), []):
                pending.setdefault(a.thread_id, a)
        member = _safe(lambda: self.room.member(account_id), None) if self.room is not None else None
        viewer = {"household": member is not None, "payer": bool(member and member.payer),
                  "read_only": bool(member is not None and not member.payer)}
        threads = _safe(lambda: self.hub.list_threads(account_id), []) if self.hub is not None else []
        code_by_thread = {}
        if self.code is not None:
            for s in _safe(lambda: self.code.store.list_user(account_id, live_only=True), []):
                code_by_thread[s.thread_id] = s
        handled: set[str] = set()
        for i, t in enumerate(threads):
            if (t.origin or {}).get("kind") == "agent" or ":house-" in t.id:
                continue  # portés par l'entrée de l'agent ou de la salle
            goal = _safe(lambda: self.goals.current(t.id), None) if self.goals is not None and i < GOAL_LOOKUPS else None
            e = self._thread_entry(t, pending.get(t.id), goal, code_by_thread.get(t.id))
            handled.add(t.id)
            if e["status"] != IDLE or e["kind"] == "code" or goal is not None:
                entries.append(e)
        for tid, s in code_by_thread.items():
            if tid not in handled:
                entries.append({"id": f"code:{s.id}", "kind": "code", "name": "", **self._status(
                    IDLE, None), "target": {"type": "thread", "id": tid}, "since": _iso(s.last_used_at)})
        entries += self._crew_entries(pending)
        entries += self._house_entries(member, pending)
        entries.sort(key=lambda e: (rank(e["status"]), -(datetime.fromisoformat(e["since"]).timestamp()
                                                          if e.get("since") else 0)))
        counts = {k: 0 for k in STATUSES}
        for e in entries:
            counts[e["status"]] += 1
        nxt = next((e["id"] for e in entries if e["status"] == AWAITING), None)
        return {"viewer": viewer, "entries": entries[: self.entries_cap], "counts": counts, "next_waiting": nxt}

    @staticmethod
    def _status(status: str, activity: dict[str, Any] | None) -> dict[str, Any]:
        return {"status": status, "activity": activity}

    def _thread_entry(self, t: Any, card: Any, goal: Any, code: Any) -> dict[str, Any]:
        sub = "/sa-" in t.id
        kind = "code" if code is not None else "subagent" if sub else "thread"
        track = self.tracker.thread(t.id) if self.tracker is not None else {}
        tool = track.get("tool")
        activity = act("snippet", text=t.last_snippet) if t.last_snippet else None
        status = IDLE
        if t.working:
            status = WORKING
            activity = act("tool", tool=tool) if tool else (activity or act("thinking"))
        if track.get("state") == PAUSED and t.working:
            status, activity = PAUSED, act("turn_paused")
        if goal is not None:
            if goal.status == "blocked":
                status, activity = BLOCKED, act("goal_blocked", text=goal.text)
            elif goal.status == "draft" and status == IDLE:
                status, activity = AWAITING, act("goal_draft", text=goal.text)
            elif goal.status == "active" and status == IDLE:
                status, activity = WORKING, act("goal_active", text=goal.text)
        if card is not None:
            status, activity = AWAITING, act("approval", tool=card.tool_name)
        e = {"id": t.id, "kind": kind, "name": t.title or "", **self._status(status, activity),
             "target": {"type": "thread", "id": t.id}, "since": _iso(t.updated_at), "goal": None}
        if goal is not None:
            e["goal"] = {"text": flat(goal.text), "status": goal.status}
        if sub:
            e["parent"] = t.id.split("/sa-", 1)[0]
        return e

    def _crew_entries(self, pending: dict[str, Any]) -> list[dict[str, Any]]:
        if self.agents is None:
            return []
        out = []
        for a in _safe(self.agents.list_agents, []):
            run = _safe(lambda: self.agents.store.live_run(a.id), None)
            status, activity, tid = IDLE, None, None
            if run is not None:
                tid = run.thread_id
                if run.status == "waiting_approval" or (tid in pending):
                    card = pending.get(tid)
                    status, activity = AWAITING, act("approval", tool=card.tool_name if card else None)
                else:
                    status = WORKING
                    activity = act("run_summary", text=run.summary) if run.summary else act(
                        "run_queued" if run.status == "queued" else "run_running")
                    if tid and self.tracker is not None and self.tracker.thread(tid).get("tool"):
                        activity = act("tool", tool=self.tracker.thread(tid)["tool"])
            elif a.status == "failed" or (a.last_run or {}).get("status") == "failed":
                status, activity = BLOCKED, act("run_failed")
            elif a.status == "paused":
                status, activity = PAUSED, act("agent_paused")
            elif a.next_run_at:
                activity = act("next_run", at=_iso(a.next_run_at))
            out.append({"id": f"agent:{a.id}", "kind": "agent", "name": a.name, "role": flat(a.role, 80),
                        **self._status(status, activity), "target": {"type": "agent", "id": a.id, "thread_id": tid},
                        "since": _iso((run.started_at or run.created_at) if run is not None else a.updated_at)})
        return out

    def _house_entries(self, member: Any, pending: dict[str, Any]) -> list[dict[str, Any]]:
        if self.room is None or member is None or not member.active:
            return []
        view = _safe(lambda: self.room.view(member.account_id), None)
        if not view:
            return []
        out = []
        for a in view["agents"]:
            slug = a["slug"]
            tid = _safe(lambda: self.room.thread_of(member, slug), None)
            card = pending.get(tid) if tid else None
            state = self.tracker.house_state(member.household_id, slug) if self.tracker is not None else IDLE
            if card is not None:
                status, activity = AWAITING, act("approval", tool=card.tool_name)
            elif not a["available"]:
                status, activity = PAUSED, act("unavailable")
            elif state == WORKING:
                status, activity = WORKING, act("house_working")
            else:
                status, activity = IDLE, None
            out.append({"id": f"house:{slug}", "kind": "house", "name": a["name"], "role": flat(a["role"], 80),
                        **self._status(status, activity), "target": {"type": "house", "id": slug}, "since": None})
        return out
