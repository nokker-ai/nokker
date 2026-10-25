"""Agents du deck multi-machines, côté machine (lot X15, docs/LOKKAL.md §6.7 et §7.19).

nokker.ai arrive par la passerelle avec un jeton de délégation qui ne nomme qu'UNE opération (`crew: {op, id}`) :

- `run` — `POST /v1/local/agent-runs` : un passage d'un agent défini chez nokker.ai et placé ici (`auto` ou ce
  `mc_…`). Il tourne dans le moteur Crew de CETTE machine (`AgentEngine.run_guest`) : SES portes et SES cartes (les
  fortes vont au téléphone, X10), la liste d'outils, le budget et les secrets PAR NOM de la définition (un secret absent
  du coffre : 409 avec les noms, rien ne tourne) ; aucune règle `auto_approve` n'est levée ici. Réponse tout de suite
  (`202`), l'état du passage repart ensuite par `POST /v1/gateway/agent-runs/{id}/result` (jeton de machine) : en
  cours, en attente d'une carte, puis le résultat. `POST /v1/local/agent-runs/{id}/cancel` l'arrête ;
- `check` — `POST /v1/local/agents/check {secrets}` : lesquels de CES noms manquent au coffre (avant un déménagement
  vers cette machine) ; jamais une valeur, jamais un autre nom ;
- `move` — `POST /v1/local/agents/{id}/export` (la définition d'un agent posé ici, pour le déménager), puis
  `POST /v1/local/agents/{id}/released {to}` : l'agent est désarmé et marqué déménagé (pierre tombale) ; ses passages
  restent ici, lisibles.

Rien d'autre de la machine n'est ouvert par ces jetons (`allows`), ni une session (X21), ni un fil, ni le coffre.

Le **deck** publié à nokker.ai (`snapshot`, envoyé avec la charge par `LoadReporter`) : sessions de code ouvertes (fil,
NOM du dossier, état ; titre seulement si les fils de la machine sont en miroir), agents posés ici (nom, état,
déclencheur, prochaine échéance, dernier passage), nombre de cartes en attente. Jamais un texte de conversation.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

logger = logging.getLogger("nokker.local.crew")

_RE_RUN = re.compile(r"^ar_[0-9a-f]{16}$")
_RE_CANCEL = re.compile(r"^/v1/local/agent-runs/(ar_[0-9a-f]{16})/cancel$")
_RE_MOVE = re.compile(r"^/v1/local/agents/(ag_[0-9a-f]{12})/(export|released)$")
_RE_TO = re.compile(r"^(auto|web|mc_[0-9a-f]{16})$")
REPORT_DELAYS_S = (0.0, 2.0, 10.0, 30.0)


def allows(method: str, path: str, crew: Any) -> bool:
    """Un jeton de délégation qui porte `crew` : la route de SON opération, sur SON objet, rien d'autre."""
    if not isinstance(crew, dict) or method != "POST":
        return False
    op, ref = str(crew.get("op") or ""), str(crew.get("id") or "")
    if op == "run":
        m = _RE_CANCEL.match(path)
        return path == "/v1/local/agent-runs" or (m is not None and m.group(1) == ref)
    if op == "check":
        return path == "/v1/local/agents/check"
    if op == "move":
        m = _RE_MOVE.match(path)
        return m is not None and m.group(1) == ref
    return False


def _err(status: int, message: str, **extra: Any) -> tuple[int, dict[str, Any]]:
    return status, {"error": {"message": message, **extra}}


@dataclass
class LocalCrew:
    hub: Any  # AgentsHub de la machine (moteur posé sur `hub.engine`)
    base_url: str = "https://app.nokker.ai"
    token: Callable[[], str | None] = field(default=lambda: None)  # jeton de machine (coffre local)
    owner: str = "local-owner"  # identité sous laquelle tournent les passages invités (le propriétaire)
    tenant: str = INTERNAL_TENANT
    client: Any = None  # httpx.Client injectable (tests)
    sync: bool = False  # tests : chaque retour part tout de suite, sans fil
    # Deck : fils dont un tour est en cours, sessions de code ouvertes (fil → dossier), cartes en attente, titre d'un
    # fil, et « les fils de cette machine sont en miroir » (seule condition pour publier un titre).
    active: Callable[[], set[str]] = field(default=set)
    sessions: Callable[[], dict[str, str]] = field(default=dict)
    approvals: Any = None
    title_of: Callable[[str], str | None] | None = None
    mirrored: Callable[[], bool] = field(default=lambda: False)
    _q: Any = field(default=None, init=False, repr=False)
    _worker: threading.Thread | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def engine(self) -> Any:
        return getattr(self.hub, "engine", None)

    # ── routes de la passerelle ──

    def handle(self, method: str, path: str, body: Any, claims: dict[str, Any] | None
               ) -> tuple[int, dict[str, Any]] | None:
        """Routes `/v1/local/agent-runs*` et `/v1/local/agents/*` : seulement par la passerelle, avec un jeton de
        délégation de nokker.ai qui nomme l'opération ; None si la route n'est pas d'ici."""
        if not (path == "/v1/local/agent-runs" or path.startswith(("/v1/local/agent-runs/", "/v1/local/agents/"))):
            return None
        crew = (claims or {}).get("crew")
        if not claims or not allows(method, path, crew):
            return _err(403, "routes des agents du web : par la passerelle, jeton de nokker.ai qui nomme l'opération")
        body = body if isinstance(body, dict) else {}
        from nokker.agents.types import AgentConflict, AgentError, AgentMissing

        try:
            with tenant_scope(self.tenant):
                if path == "/v1/local/agent-runs":
                    return self._run(body, crew)
                m = _RE_CANCEL.match(path)
                if m:
                    return self._cancel(m.group(1))
                if path == "/v1/local/agents/check":
                    return self._check(body)
                m = _RE_MOVE.match(path)
                if m and m.group(2) == "export":
                    return self._export(m.group(1))
                if m:
                    return self._released(m.group(1), body)
        except AgentMissing as e:
            return _err(409, str(e), missing=e.missing)
        except AgentConflict as e:
            return _err(409, str(e))
        except AgentError as e:
            return _err(400, str(e))
        return None

    def _run(self, body: dict[str, Any], crew: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        run_id = str(body.get("run_id") or "")
        if not _RE_RUN.match(run_id) or run_id != crew.get("id"):
            return _err(400, "run_id : celui que nomme le jeton")
        if self.engine is None:
            return _err(503, "moteur des agents éteint sur cette machine (local.crew.enabled)")
        sched = body.get("scheduled_for")
        run = self.engine.run_guest(body.get("agent"), run_id, owner=self.owner,
                                    trigger=str(body.get("trigger") or "manual"),
                                    input_block=str(body.get("input") or ""),
                                    scheduled_for=float(sched) if isinstance(sched, (int, float)) else None)
        return 202, {"run_id": run.id, "status": run.status, "thread_id": run.thread_id}

    def _cancel(self, run_id: str) -> tuple[int, dict[str, Any]]:
        run = self.hub.store.get_run(run_id)
        if run is None or not run.guest:
            return _err(404, "passage inconnu ici")
        if self.engine is not None:
            run = self.engine.cancel(run, "nokker.ai")
        return 200, {"run_id": run.id, "status": run.status}

    def _check(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        from nokker.agents.spec import norm_secrets

        names = norm_secrets(body.get("secrets"))
        if self.engine is None:
            return _err(503, "moteur des agents éteint sur cette machine (local.crew.enabled)")
        missing = self.engine._missing_secrets(names)
        return 200, {"missing": [{"kind": "secret", "name": n.split(" ", 1)[0], "message": n} for n in missing]}

    def _export(self, agent_id: str) -> tuple[int, dict[str, Any]]:
        from nokker.agents.types import agent_to_json

        a = self.hub.store.get(agent_id)
        if a is None or self.hub.moved_to(a):
            return _err(404, "agent inconnu ici")
        if self.hub.store.live_run(a.id) is not None:
            return _err(409, "un passage est en cours : attendre sa fin avant de déménager")
        out = agent_to_json(a)
        out["armed"] = bool(a.armed)
        return 200, {"agent": out}

    def _released(self, agent_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        to = str(body.get("to") or "")
        if not _RE_TO.match(to):
            return _err(400, "to : auto, web ou mc_…")
        a = self.hub.store.get(agent_id)
        if a is None:
            return _err(404, "agent inconnu ici")
        if self.hub.moved_to(a):
            return 200, {"moved": True}  # rejoué : déjà fait
        if self.hub.store.live_run(a.id) is not None:
            return _err(409, "un passage est en cours : attendre sa fin avant de déménager")
        self.hub._cancel_arm_card(a, "agent déménagé")
        a.armed, a.next_run_at = False, None
        a.placement = {"machine": to, "moved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")}
        self.hub.refresh(a)
        return 200, {"moved": True}

    # ── retour de l'état des passages invités ──

    def report(self, run: Any) -> None:
        """`AgentEngine.on_guest` : l'état du passage repart vers nokker.ai, dans l'ordre (un fil, une file)."""
        body = {"status": run.status, "thread_id": run.thread_id, "summary": run.summary, "cost": run.cost,
                "started_at": run.started_at, "ended_at": run.ended_at}
        if self.sync:
            self._send(run.id, body)
            return
        with self._lock:
            if self._worker is None:
                self._q = queue.Queue()
                self._worker = threading.Thread(target=self._drain, name="nokker-crew-report", daemon=True)
                self._worker.start()
        self._q.put((run.id, body))

    def _drain(self) -> None:
        while True:
            run_id, body = self._q.get()
            for delay in REPORT_DELAYS_S:
                if delay:
                    time.sleep(delay)
                if self._send(run_id, body):
                    break

    def _send(self, run_id: str, body: dict[str, Any]) -> bool:
        try:
            token = self.token()
        except Exception:  # noqa: BLE001
            token = None
        if not token:
            return False
        try:
            http = self.client or httpx.Client(timeout=10.0)
            r = http.post(f"{self.base_url.rstrip('/')}/v1/gateway/agent-runs/{run_id}/result", json=body,
                          headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            logger.info("passage %s : état non remis (%s)", run_id, type(e).__name__)
            return False
        return r.status_code in (200, 404)  # 404 : nokker.ai ne le connaît plus, inutile d'insister

    # ── deck ──

    def snapshot(self) -> dict[str, Any]:
        """Ce que cette machine publie pour le deck : noms et états seulement."""
        active = set(self.active() or ())
        waiting: dict[str, int] = {}
        pending = 0
        with tenant_scope(self.tenant):
            if self.approvals is not None:
                try:
                    cards = self.approvals.pending()
                except Exception:  # noqa: BLE001 — un deck sans cartes vaut mieux que pas de deck
                    cards = []
                pending = len(cards)
                for c in cards:
                    waiting[c.thread_id] = waiting.get(c.thread_id, 0) + 1
            titles = bool(self.mirrored())
            sessions = []
            for tid, cwd in sorted((self.sessions() or {}).items()):
                state = "awaiting_approval" if waiting.get(tid) else "working" if tid in active else "idle"
                title = None
                if titles and self.title_of is not None:
                    try:
                        title = self.title_of(tid)
                    except Exception:  # noqa: BLE001
                        title = None
                sessions.append({"thread_id": tid, "name": os.path.basename(str(cwd).rstrip("/")) or None,
                                 "state": state, "title": title})
            agents = []
            for a in self.hub.store.list_all():
                if self.hub.moved_to(a):
                    continue
                lr = a.last_run or None
                agents.append({"id": a.id, "name": a.name, "status": a.status,
                               "trigger": (a.trigger or {}).get("kind") or "manual", "next_run_at": a.next_run_at,
                               "last_run": {"status": lr.get("status"), "ended_at": lr.get("ended_at")} if lr else None})
        return {"v": 1, "sessions": sessions[:100], "agents": agents[:200], "pending_approvals": pending}


__all__ = ["LocalCrew", "allows"]
