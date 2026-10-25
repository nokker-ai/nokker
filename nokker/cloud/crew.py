"""Deck multi-machines et agents placés (lot X15, docs/LOKKAL.md §6.7 et §7.19, docs/BUBBLES.md §9.5), côté nokker.ai.

Une passerelle, N machines liées au même compte, chacune ses sessions ; nokker.ai les rassemble :

- **placement** (`CloudPlacement`, posé sur `AgentsHub.placement_policy`) : `placement: {machine: "auto" | "mc_…" |
  "web"}` d'un agent défini chez nokker.ai. `auto` = chaque passage sur la machine éligible choisie par
  `nokker/cloud/placement.py` (la logique de X21) ; `mc_…` = cette machine-là, ou l'attente qu'elle revienne ; `web` =
  chez nokker.ai (offre : `cloud_link.web_agents`, et moteur `NOKKER_CREW_ENGINE=1`) ;
- **envoi d'un passage** (`CloudCrew.dispatch`, appelé par le moteur de nokker.ai) : par la passerelle, comme une
  session déléguée (X21) — jeton de délégation court qui ne nomme que CE passage (`crew: {op: "run", id}`), la machine
  le fait tourner avec SES portes et SES cartes (téléphone, X10), son état revient par
  `POST /v1/gateway/agent-runs/{id}/result` (jeton de machine) et le résultat finit dans le passage d'ici ;
- **déménagement** (`CloudCrew.move`, `POST /v1/agents/{id}/move {to}`) : refusé avec la liste des manques
  (ressources et GPU d'après l'inventaire, secrets PAR NOM demandés à la machine de destination elle-même) ; sinon la
  définition change de maison. Un agent défini SUR une machine (Lokkal) est d'abord exporté (lecture), puis créé ici,
  puis la machine le marque déménagé (pierre tombale : ses passages y restent lisibles) ;
- **deck** (`CloudCrew.deck`, `GET /v1/link/deck`) : par machine, sa charge, ses sessions actives, ses agents et ses
  cartes en attente, plus les agents `web` et `auto` — par NOMS et ÉTATS seulement. Ce que la machine dit d'elle vient
  de `POST /v1/gateway/machine/deck` (nettoyé par `clean_deck` : un texte de conversation n'y a pas de place ; un
  titre de fil n'est gardé que pour un compte dont les fils sont en miroir).

Cloisonnement : machines du compte (`machines_with_load`), deck et passages sous le locataire (RLS) ; une machine ou
un agent d'un autre compte n'existe pas (404).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from nokker.agents.spec import placement_target
from nokker.agents.types import (
    LIVE_RUN,
    RUN_STATUSES,
    STATUSES,
    TRIGGERS,
    Agent,
    AgentConflict,
    AgentError,
    AgentMissing,
    AgentNotFound,
)
from nokker.cloud import placement as pl

logger = logging.getLogger("nokker.cloud.crew")

MAX_DECK_BYTES = 64 * 1024
MAX_SESSIONS = 100
MAX_AGENTS = 200
SESSION_STATES = ("idle", "working", "awaiting_approval")
_THREAD = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
_AGENT = re.compile(r"^ag_[0-9a-f]{12}$")
_RUN = re.compile(r"^ar_[0-9a-f]{16}$")
_MACHINE = re.compile(r"^mc_[0-9a-f]{16}$")


def _is_account(sender: str | None) -> bool:
    return bool(sender) and str(sender).startswith("us_")


def target_label(to: str) -> str:
    return {"web": "nokker.ai (web)", "auto": "la machine la moins chargée (auto)"}.get(to, to)


# ─────────────────────────────── placement d'un agent (politique du hub) ───────────────────────────────


@dataclass
class CloudPlacement:
    """`placement` chez nokker.ai : `auto`, `web` (offre), une machine du compte du propriétaire de l'agent."""

    links: Any  # LinkService
    web_allowed: Callable[[str], bool] | None = None  # compte → agents web permis ; None = tous

    def normalize(self, machine: str, owner: str | None) -> dict[str, Any]:
        if machine == "auto":
            return {"machine": "auto"}
        if machine == "web":
            if self.web_allowed is not None and not (owner and self.web_allowed(owner)):
                raise AgentError("placement « web » : agents qui tournent chez nokker.ai réservés aux abonnés — "
                                 "placer l'agent sur une machine (auto ou mc_…)")
            return {"machine": "web"}
        if not _is_account(owner) or self.links.machine_of(owner, machine) is None:
            raise AgentError(f"placement : machine {machine} inconnue de ce compte (ou déliée)")
        return {"machine": machine}

    @staticmethod
    def is_remote(target: str | None) -> bool:
        return target == "auto" or bool(target and _MACHINE.match(target))


# ─────────────────────────────── deck publié par la machine ───────────────────────────────


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1e11 else None


def clean_deck(raw: Any, *, titles: bool) -> dict[str, Any] | None:
    """Ce qu'une machine publie pour le deck, réduit aux NOMS et ÉTATS bornés ; None si ce n'est pas un objet.
    `titles` faux (compte sans miroir) : aucun titre de fil n'est gardé, même envoyé."""
    if not isinstance(raw, dict):
        return None
    sessions = []
    for s in (raw.get("sessions") if isinstance(raw.get("sessions"), list) else [])[:MAX_SESSIONS]:
        if not isinstance(s, dict) or not _THREAD.match(str(s.get("thread_id") or "")):
            continue
        name = str(s.get("name") or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1][:80]  # un nom, pas un chemin
        title = s.get("title") if titles and isinstance(s.get("title"), str) else None
        sessions.append({"thread_id": str(s["thread_id"]), "name": name or None,
                         "state": s.get("state") if s.get("state") in SESSION_STATES else "idle",
                         "title": " ".join(title.split())[:120] or None if title else None})
    agents = []
    for a in (raw.get("agents") if isinstance(raw.get("agents"), list) else [])[:MAX_AGENTS]:
        if not isinstance(a, dict) or not _AGENT.match(str(a.get("id") or "")):
            continue
        lr = a.get("last_run") if isinstance(a.get("last_run"), dict) else None
        agents.append({"id": str(a["id"]), "name": str(a.get("name") or "")[:200],
                       "status": a.get("status") if a.get("status") in STATUSES else "paused",
                       "trigger": a.get("trigger") if a.get("trigger") in TRIGGERS else "manual",
                       "next_run_at": _num(a.get("next_run_at")),
                       "last_run": {"status": lr.get("status") if lr.get("status") in RUN_STATUSES else None,
                                    "ended_at": _num(lr.get("ended_at"))} if lr else None})
    pending = _num(raw.get("pending_approvals"))
    return {"v": 1, "sessions": sessions, "agents": agents,
            "pending_approvals": int(pending) if pending is not None and pending < 1e5 else 0}


class MemoryDeckStore:
    """Deck courant de chaque machine (tests, dev). `PgDeckStore` (nokker/cloud/pg.py, migration 028) : même
    interface, sous RLS."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, dict[str, Any], float]] = {}
        self.lock = threading.Lock()

    def set_deck(self, machine_id: str, tenant: str, deck: dict[str, Any], at: float) -> None:
        with self.lock:
            self.rows[machine_id] = (tenant, deck, at)

    def decks_of(self, tenant: str, machine_ids: list[str]) -> dict[str, tuple[dict[str, Any], float]]:
        with self.lock:
            return {mid: (v[1], v[2]) for mid in machine_ids if (v := self.rows.get(mid)) and v[0] == tenant}


# ─────────────────────────────── envoi d'un passage ───────────────────────────────


@dataclass
class DispatchResult:
    state: str  # sent | waiting | refused
    machine_id: str | None = None
    thread_id: str | None = None
    reason: str | None = None
    status: str | None = None  # refused : statut final du passage (failed, ou skipped pour des identifiants absents)


class _Unreachable(Exception):
    pass


@dataclass
class CloudCrew:
    links: Any  # LinkService
    signer: Any  # Signer
    deck_store: Any = field(default_factory=MemoryDeckStore)
    web_allowed: Callable[[str], bool] | None = None  # compte → agents `web` permis (offre) ; None = tous
    web_engine: bool = False  # moteur des agents allumé chez nokker.ai (NOKKER_CREW_ENGINE=1)
    titles_allowed: Callable[[str], bool] | None = None  # compte → fils en miroir (titres gardés au deck)
    approvals_relay: Any = None  # RelayService (X10) : cartes fortes en attente au deck
    agent_tenant: Callable[[Agent], str | None] | None = None  # magasin mémoire : locataire d'un agent
    engine: Any = None  # AgentEngine de nokker.ai (posé par serve)
    client: Any = None  # httpx.Client injectable (tests)
    scheme: str = "https"
    timeout_s: float = 20.0
    clock: Callable[[], float] = time.time

    @property
    def policy(self) -> CloudPlacement:
        return CloudPlacement(links=self.links, web_allowed=self.web_allowed)

    # ── appel d'une machine par la passerelle ──

    def _call(self, m: Any, account: str, tenant: str, op: str, ref: str, path: str,
              body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """POST vers la machine `m` par la passerelle (`https://<nom>.nokker.app`), jeton de délégation qui ne nomme
        que cette opération. `_Unreachable` : rien n'est arrivé (tunnel fermé, machine hors ligne)."""
        token = self.signer.delegation_token(account, tenant, machine=m.id, web_session=f"crew:{ref}",
                                             crew={"op": op, "id": ref})
        http = self.client or httpx.Client(timeout=self.timeout_s)
        try:
            r = http.post(f"{self.scheme}://{m.host}{path}", json=body,
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        except httpx.HTTPError as e:
            raise _Unreachable(type(e).__name__) from e
        if r.status_code in (502, 503, 504):  # le relais : machine hors ligne, tunnel fermé
            raise _Unreachable(str(r.status_code))
        try:
            data = r.json()
        except ValueError:
            data = {}
        return r.status_code, data if isinstance(data, dict) else {}

    def dispatch(self, a: Agent, run: Any, tenant: str, claim: Callable[[str | None], None] | None = None
                 ) -> DispatchResult:
        """Le passage `run` de `a` (placé `auto` ou `mc_…`) part vers sa machine. `sent` : la machine l'a pris ;
        `waiting` : aucune machine éligible en ligne (la raison le dit, le moteur réessaie) ; `refused` : refusé pour
        de bon (identifiants absents, machine trop ancienne)."""
        account = a.created_by
        if not _is_account(account):
            return DispatchResult("refused", reason="agent sans compte nokker.ai : aucune machine liée", status="failed")
        target = placement_target(a.placement)
        machines = self.links.machines_with_load(account, tenant)
        p = pl.place(machines, pl.needs_of(a.needs), self.clock(), only=None if target == "auto" else target)
        if p.machine is None:
            return DispatchResult("waiting", reason=p.reason)
        body = {"run_id": run.id, "trigger": run.trigger, "scheduled_for": run.scheduled_for, "input": run.input or "",
                "agent": {"id": a.id, "name": a.name, "role": a.role, "model": a.model, "tools": list(a.tools),
                          "instructions": a.instructions, "secrets": list(a.secrets), "budget": a.budget}}
        refusals: list[tuple[str, str]] = []  # (statut, raison)
        unreachable: list[str] = []
        for m in p.ranked:
            if claim is not None:
                claim(m.id)
            try:
                status, data = self._call(m, account, tenant, "run", run.id, "/v1/local/agent-runs", body)
            except _Unreachable:
                unreachable.append(f"« {m.name} » injoignable par la passerelle")
                continue
            if status in (200, 201, 202):
                thread = str(data.get("thread_id") or "") or None
                return DispatchResult("sent", machine_id=m.id, thread_id=thread)
            msg = _message(data) or f"HTTP {status}"
            if status == 409 and data.get("error", {}).get("missing"):
                secrets = all(x.get("kind") == "secret" for x in data["error"]["missing"] if isinstance(x, dict))
                refusals.append(("skipped" if secrets else "failed", f"« {m.name} » : {msg}"))
            elif status == 404:
                refusals.append(("failed", (f"« {m.name} » ne prend pas encore les agents du web (mettre "
                                           f"Nokker Lokkal à jour)")))
            else:
                refusals.append(("failed", f"« {m.name} » refuse le passage ({status}) : {msg}"))
        if claim is not None:
            claim(None)
        if unreachable:
            return DispatchResult("waiting", reason="; ".join(unreachable + [r for _, r in refusals]))
        status = "skipped" if all(s == "skipped" for s, _ in refusals) else "failed"
        return DispatchResult("refused", reason="; ".join(r for _, r in refusals), status=status)

    def cancel(self, run: Any, tenant: str) -> bool:
        """Annule le passage sur sa machine (au mieux : machine éteinte, il y finira seul). Appelé hors requête (fil du
        moteur) : le locataire est posé ici."""
        from nokker.tenancy.context import tenant_scope

        engine_store = self.engine.store if self.engine is not None else None
        with tenant_scope(tenant):
            a = engine_store.get(run.agent_id) if engine_store is not None else None
        account = a.created_by if a is not None else None
        m = next((x for x in self.links.machines_of(account) if x.id == run.machine_id), None) if account else None
        if m is None:
            return False
        try:
            status, _ = self._call(m, account, tenant, "run", run.id, f"/v1/local/agent-runs/{run.id}/cancel", {})
        except _Unreachable:
            return False
        return status == 200

    def machine_online(self, machine: Any) -> int:
        """La machine vient d'envoyer sa charge : les passages qui attendaient une machine repartent."""
        if self.engine is None:
            return 0
        return self.engine.retry_remote(tenant=machine.tenant)

    def result(self, machine: Any, run_id: str, body: Any) -> tuple[int, dict[str, Any]]:
        """`POST /v1/gateway/agent-runs/{run_id}/result` : dans le locataire de la machine authentifiée."""
        from nokker.tenancy.context import tenant_scope

        if self.engine is None or not _RUN.match(run_id):
            return 404, {"error": {"message": "not found"}}
        with tenant_scope(machine.tenant):
            return self.engine.remote_result(machine.id, run_id, body if isinstance(body, dict) else {})

    # ── déménagement ──

    def missing_for(self, secrets: list[str], needs: pl.Needs, to: str, account: str, tenant: str,
                    ref: str) -> list[dict[str, Any]]:
        """Ce qui manque à la destination `to` (web, auto, mc_…), par NOMS. Vide : le déménagement peut se faire."""
        out: list[dict[str, Any]] = []
        if to == "web":
            if self.web_allowed is not None and not self.web_allowed(account):
                out.append({"kind": "offer", "message": "agents qui tournent chez nokker.ai : réservés aux abonnés"})
            if not self.web_engine:
                out.append({"kind": "engine", "message": "moteur des agents éteint chez nokker.ai"})
            for kind, name in needs.resources():
                out.append({"kind": pl.SINGULAR[kind], "name": name,
                            "message": f"{pl.LABEL[kind]} « {name} » : n'existe que sur une machine"})
            if needs.gpu:
                out.append({"kind": "gpu", "message": "pas de GPU chez nokker.ai"})
            for s in secrets:
                out.append({"kind": "secret", "name": s,
                            "message": f"secret « {s} » : pas de coffre chez nokker.ai en v1 (un agent web n'en "
                                       "déclare pas)"})
            return out
        machines = self.links.machines_with_load(account, tenant)
        if to == "auto":
            p = pl.place(machines, needs, self.clock())
            if p.machine is None and p.fallback is None:
                seen = set()
                for mid, items in p.lacking.items():
                    for x in items:
                        key = (x["kind"], x.get("name"), mid)
                        if key not in seen:
                            seen.add(key)
                            out.append({**x, "machine_id": mid})
                return out or [{"kind": "machine", "message": p.reason or "aucune machine liée"}]
            return out  # secrets : vérifiés par la machine choisie à chaque passage
        m = next((x for x in machines if x.id == to), None)
        if m is None:
            return [{"kind": "machine", "name": to, "message": f"machine {to} inconnue de ce compte (ou déliée)"}]
        out.extend(pl.lacks(m, needs))
        if secrets:
            if not pl.is_online(m, self.clock()):
                out.append({"kind": "machine", "name": m.name,
                            "message": f"« {m.name} » hors ligne : ses secrets ne peuvent pas être vérifiés — "
                                       "réessayer machine allumée"})
                return out
            try:
                status, data = self._call(m, account, tenant, "check", ref, "/v1/local/agents/check",
                                          {"secrets": list(secrets)})
            except _Unreachable:
                out.append({"kind": "machine", "name": m.name, "message": f"« {m.name} » injoignable par la passerelle"})
                return out
            if status != 200:
                out.append({"kind": "machine", "name": m.name,
                            "message": f"« {m.name} » ne vérifie pas ses secrets ({status}) : mettre Nokker Lokkal à jour"})
                return out
            for x in data.get("missing") or []:
                if isinstance(x, dict) and x.get("name") in secrets:  # que des noms que l'agent déclare
                    out.append({"kind": "secret", "name": x["name"],
                                "message": f"secret « {x['name']} » à poser dans le coffre de « {m.name} »"})
        return out

    def _agent_here(self, hub: Any, agent_id: str, tenant: str) -> Agent | None:
        a = hub.store.get(agent_id)
        if a is not None and self.agent_tenant is not None and self.agent_tenant(a) != tenant:
            return None
        return a

    def move(self, hub: Any, agent_id: str, to: Any, by: str, tenant: str) -> Agent:
        target = placement_target({"machine": to}) if isinstance(to, str) else None
        if target is None:
            raise AgentError("to : « web », « auto » ou l'id d'une machine (mc_…)")
        if not _is_account(by):
            raise AgentError("déménagement : session d'un compte nokker.ai requise")
        a = self._agent_here(hub, agent_id, tenant)
        if a is not None:
            return self._move_here(hub, a, target, tenant)
        return self._move_from_machine(hub, agent_id, target, by, tenant)

    def _move_here(self, hub: Any, a: Agent, to: str, tenant: str) -> Agent:
        if hub.store.live_run(a.id) is not None:
            raise AgentConflict("un passage est en cours : attendre sa fin (ou l'annuler) avant de déménager")
        if (placement_target(a.placement) or "web") == to:
            return a
        missing = self.missing_for(a.secrets, pl.needs_of(a.needs), to, a.created_by, tenant, a.id)
        if missing:
            raise AgentMissing(f"déménagement vers {target_label(to)} refusé : il manque "
                               + "; ".join(x["message"] for x in missing), missing)
        a.placement = {"machine": to}
        return hub.refresh(a)

    def _find_on_machine(self, account: str, tenant: str, agent_id: str) -> tuple[Any, dict[str, Any]] | None:
        machines = self.links.machines_with_load(account, tenant)
        decks = self.deck_store.decks_of(tenant, [m.id for m in machines]) if machines else {}
        for m in machines:
            for card in (decks.get(m.id, ({}, 0))[0].get("agents") or []):
                if card.get("id") == agent_id:
                    return m, card
        return None

    def _move_from_machine(self, hub: Any, agent_id: str, to: str, account: str, tenant: str) -> Agent:
        from nokker.agents.spec import norm_fields

        found = self._find_on_machine(account, tenant, agent_id)
        if found is None:
            raise AgentNotFound(agent_id)
        m, card = found
        if card.get("status") == "running":
            raise AgentConflict("un passage est en cours sur la machine : attendre sa fin avant de déménager")
        try:
            status, data = self._call(m, account, tenant, "move", agent_id, f"/v1/local/agents/{agent_id}/export", {})
        except _Unreachable:
            raise AgentMissing(f"« {m.name} » injoignable : l'agent ne peut pas en partir maintenant",
                               [{"kind": "machine", "name": m.name, "message": f"« {m.name} » hors ligne"}]) from None
        if status != 200 or not isinstance(data.get("agent"), dict):
            raise AgentConflict(f"« {m.name} » ne rend pas l'agent ({status}) : {_message(data) or 'réponse illisible'}")
        d = data["agent"]
        secrets = [str(x) for x in d.get("secrets") or []]
        missing = self.missing_for(secrets, pl.needs_of(d.get("needs")), to, account, tenant, agent_id)
        if missing:
            raise AgentMissing(f"déménagement vers {target_label(to)} refusé : il manque "
                               + "; ".join(x["message"] for x in missing), missing)
        body = {k: d.get(k) for k in ("name", "role", "model", "tools", "instructions", "trigger", "secrets",
                                      "budget", "auto_approve", "needs")}
        f = norm_fields({**body, "placement": {"machine": to}}, partial=False, policy=self.policy, owner=account)
        writes = hub.write_rules(f.get("auto_approve") or [])
        new = Agent(id=agent_id, created_by=account, created_at=self.clock(), updated_at=self.clock(),
                    armed=False, status="paused", **f)
        if hub.store.get(agent_id) is not None:
            raise AgentConflict("un agent de même id existe déjà ici")
        hub.store.save(new)
        try:
            ok_status, _ = self._call(m, account, tenant, "move", agent_id, f"/v1/local/agents/{agent_id}/released",
                                      {"to": to})
        except _Unreachable:
            ok_status = 0
        if ok_status != 200:
            hub.store.delete(agent_id)  # rien n'a bougé : l'agent reste sur sa machine
            raise AgentConflict(f"« {m.name} » n'a pas confirmé le départ de l'agent : rien n'a bougé, réessayer")
        hub._emit("agent.created", {"agent": _agent_json(new)})
        if d.get("armed") and not writes:
            return hub._armed(hub.store.require(agent_id), self.clock())  # il tournait là-bas : il tourne ici
        # Une règle d'écriture approuvée là-bas ne vaut rien ici : l'agent arrive au repos, à réarmer (carte forte).
        return hub.refresh(hub.store.require(agent_id))

    # ── deck ──

    def report_deck(self, machine: Any, raw: Any) -> dict[str, Any] | None:
        titles = bool(self.titles_allowed and self.titles_allowed(machine.account))
        deck = clean_deck(raw, titles=titles)
        if deck is None:
            return None
        self.deck_store.set_deck(machine.id, machine.tenant, deck, self.clock())
        return deck

    def deck(self, account: str, tenant: str, hub: Any = None) -> dict[str, Any]:
        """`GET /v1/link/deck` : toutes les machines du compte et les agents du locataire, par noms et états."""
        from nokker.tenancy.context import tenant_scope

        now = self.clock()
        machines = self.links.machines_with_load(account, tenant)
        decks = self.deck_store.decks_of(tenant, [m.id for m in machines]) if machines else {}
        here: list[tuple[Agent, Any]] = []
        if hub is not None:
            with tenant_scope(tenant):
                for a in hub.store.list_all():
                    if self.agent_tenant is not None and self.agent_tenant(a) != tenant:
                        continue
                    if hub.moved_to(a):
                        continue
                    here.append((a, hub.store.live_run(a.id)))
        ids_here = {a.id for a, _ in here}
        by_machine: dict[str, list[dict[str, Any]]] = {m.id: [] for m in machines}
        web, auto = [], []
        for a, live in here:
            card = _card(a, live)
            target = _target(a)
            if target == "auto":
                auto.append(card)
            elif target in by_machine:
                by_machine[target].append(card)
            elif target and _MACHINE.match(target):
                continue  # machine déliée depuis : l'agent n'a plus de maison visible (le PATCH le dira)
            else:
                web.append(card)
        out_machines = []
        strong = self._strong_of(tenant)
        for m in sorted(machines, key=lambda x: (x.name, x.id)):
            deck, at = decks.get(m.id, (None, None))
            remote_cards = [{**c, "placement": m.id, "defined_at": "machine", "live_run": None}
                            for c in ((deck or {}).get("agents") or []) if c.get("id") not in ids_here]
            out_machines.append({
                "machine_id": m.id, "name": m.name, "host": m.host, "online": pl.is_online(m, now),
                "load_age_s": None if m.load_at is None else max(0, int(now - m.load_at)),
                "load": _load_view(m.load), "deck_at": at,
                "deck_age_s": None if at is None else max(0, int(now - at)),
                "sessions": list((deck or {}).get("sessions") or []),
                "agents": by_machine.get(m.id, []) + remote_cards,
                "pending_approvals": int((deck or {}).get("pending_approvals") or 0),
                "strong_approvals": strong(m.id)})
        return {"at": int(now), "machines": out_machines, "web": {"engine": self.web_engine, "agents": web},
                "auto": {"agents": auto}}

    def _strong_of(self, tenant: str) -> Callable[[str], list[dict[str, Any]]]:
        def strong(machine_id: str) -> list[dict[str, Any]]:
            store = getattr(self.approvals_relay, "store", None)
            if store is None:
                return []
            now = self.clock()
            base = self.links.base_url.rstrip("/")
            return [{"approval_id": c.approval_id, "expires_at": c.expires_at,
                     "url": f"{base}/approve/{machine_id}/{c.approval_id}"}
                    for c in store.of_machine(tenant, machine_id) if c.status == "pending" and c.expires_at > now]
        return strong


def _target(a: Agent) -> str | None:
    try:
        return placement_target(a.placement)
    except AgentError:
        return None


def _card(a: Agent, live: Any) -> dict[str, Any]:
    """Carte du deck d'un agent défini chez nokker.ai : noms et états (jamais la mission ni le compte rendu)."""
    lr = a.last_run or None
    return {"id": a.id, "name": a.name, "status": a.status, "placement": _target(a) or "web",
            "trigger": (a.trigger or {}).get("kind") or "manual", "next_run_at": a.next_run_at,
            "last_run": {"status": lr.get("status"), "ended_at": lr.get("ended_at")} if lr else None,
            "live_run": {"run_id": live.id, "status": live.status, "machine_id": live.machine_id}
            if live is not None and live.status in LIVE_RUN else None, "defined_at": "nokker.ai"}


def _load_view(load: Any) -> dict[str, Any] | None:
    if not isinstance(load, dict):
        return None
    cpu = load.get("cpu") or {}
    count = cpu.get("count") or 0
    return {"ram": {k: (load.get("ram") or {}).get(k) for k in ("total_mb", "free_mb")},
            "cpu": {"count": count, "load1": cpu.get("load1"),
                    "rel": round(float(cpu.get("load1") or 0) / float(count), 3) if count else None},
            "gpus": [{k: g.get(k) for k in ("name", "memory_total_mb", "memory_free_mb", "util_pct")}
                     for g in load.get("gpus") or [] if isinstance(g, dict)],
            "sessions": load.get("sessions") or 0, "active_turns": load.get("active_turns") or 0}


def _agent_json(a: Agent) -> dict[str, Any]:
    from nokker.agents.types import agent_to_json

    return agent_to_json(a)


def _message(data: dict[str, Any]) -> str:
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or "")[:300]
    return str(err or "")[:300]


__all__ = ["CloudCrew", "CloudPlacement", "DispatchResult", "MemoryDeckStore", "clean_deck", "target_label"]
