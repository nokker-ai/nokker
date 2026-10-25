"""`AgentsHub` — Crew (docs/BUBBLES.md §9.4, §9.5) : la carte d'un agent, son cycle armé ⇄ au repos, ses événements.

Deux chemins créent un agent : `POST /v1/agents` (`nokker/api/server.py`) et l'activation d'une bulle `agent_draft`
(`nokker/bubbles/hub.py::BubbleHub.action`, qui ferme la bulle en plus) ; les mêmes règles s'appliquent (`spec.py`).
L'exécution (ordonnanceur, passages, carte forte d'armement) est dans `engine.AgentEngine`, posé sur `engine` ; sans
moteur (la bêta), la carte se gère mais rien ne tourne : un déclencheur autre que `manual` est refusé à la création et
au PATCH (400 `NO_ENGINE_SCHEDULED`, revue du train du 2.11, défaut 6), lancer ou armer une écriture répond 503.

Événements, diffusés à TOUTE identité vérifiée du locataire (pas de filtre par membre — même principe que
`approval.*`, docs/CLIENT-API.md : Crew est une vue d'équipe) : `agent.created {agent}`, `agent.updated {agent}` (tout
changement, dont `status`, `next_run_at`, `last_run`), `agent.deleted {id}`, `agent.run {agent_id, run}`.

`status` est calculé, jamais posé par un client : `running` tant qu'un passage est vivant (en file, en cours, en
attente d'une carte), sinon `failed` si l'agent est armé et que son dernier passage a échoué, sinon `armed` ou
`paused` selon que l'humain l'a armé. Un agent `failed` reste armé : ses échéances continuent (désarmer les arrête).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from nokker.agents.base import BaseAgents
from nokker.agents.cron import next_run
from nokker.agents.spec import norm_fields, norm_placement, parse_when
from nokker.agents.types import (
    Agent, AgentConflict, AgentError, AgentNotFound, AgentRun, agent_to_json, run_to_json,
)
from nokker.core.turn import Effect

logger = logging.getLogger("nokker.agents.hub")

# Changer l'un de ces champs sur un agent armé le désarme (§9.5) : ce que l'humain a armé n'est plus ce qui tournerait.
DISARMING_FIELDS = ("trigger", "tools", "auto_approve", "secrets")


class EngineUnavailable(AgentError):
    """Pas de moteur ou pas d'approbations (503)."""


NO_ENGINE_SCHEDULED = "les agents programmés ne sont pas disponibles sur cette instance (pas de moteur)"
NO_ENGINE_WRITE = ("pas de moteur d'agents sur cette instance : un agent qui lève une écriture (auto_approve) ne peut "
                   "pas y être armé")


def rules_fingerprint(rules: list[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(rules), ensure_ascii=False).encode()).hexdigest()


@dataclass
class AgentsHub:
    store: BaseAgents
    publish: Callable[[str, dict], Any] | None = None  # EventBus.publish
    engine: Any = None  # engine.AgentEngine (posé par serve) ; None = rien ne tourne
    clock: Callable[[], float] = time.time
    # Effet de l'outil d'une règle `auto_approve` (spec du registre) ; None ou inconnu = écriture (prudent).
    rule_effect: Callable[[str], Effect | None] | None = None
    # Identifiant `mc_…` de la machine qui porte le noyau (Lokkal lié), pour accepter `placement: {machine: <elle>}`.
    local_machine: Callable[[], str | None] | None = None
    # Abonnés aux changements d'état des passages (incidents, plan : lot X18), appelés dans le locataire du passage
    # après `agent.run` ; une erreur d'abonné ne casse jamais un passage.
    run_listeners: list[Callable[[AgentRun], Any]] = field(default_factory=list)
    # Chez nokker.ai (X15, `nokker/cloud/crew.py`) : `placement` (`auto`, `web`, les machines du compte) et le
    # déménagement (`POST /v1/agents/{id}/move`). None (Lokkal, ou nokker.ai sans lien) : seule cette machine.
    placement_policy: Any = None
    mover: Any = None

    # ─────────────────────────────── utilitaires ───────────────────────────────

    def _emit(self, event_type: str, data: dict) -> None:
        if self.publish is not None:
            self.publish(event_type, data)

    def _machine(self) -> str | None:
        try:
            return self.local_machine() if self.local_machine is not None else None
        except Exception:  # noqa: BLE001 — état local illisible : aucune machine nommée
            return None

    def write_rules(self, rules: list[str]) -> list[str]:
        """Règles d'écriture parmi `rules` : outil dont l'effet déclaré n'est pas READ, ou inconnu."""
        from nokker.qa.permissions import Rule

        out = []
        for text in rules:
            tool = Rule.parse(text).tool
            effect = self.rule_effect(tool) if self.rule_effect is not None else None
            if effect is not Effect.READ:
                out.append(text)
        return out

    def next_occurrence(self, a: Agent, now: float) -> float | None:
        kind = a.trigger.get("kind")
        if kind == "schedule":
            nxt = next_run(a.trigger["cron"], a.trigger.get("tz") or "UTC",
                           datetime.fromtimestamp(now, tz=timezone.utc))
            return nxt.timestamp() if nxt is not None else None
        if kind == "once":
            at = parse_when(a.trigger["at"])
            return at if at > now else None
        return None

    def derive_status(self, a: Agent, live: AgentRun | None = None) -> str:
        if live is not None:
            return "running"
        if a.armed:
            return "failed" if (a.last_run or {}).get("status") == "failed" else "armed"
        return "paused"

    def refresh(self, a: Agent, *, publish: bool = True, touch: bool = True) -> Agent:
        """Recalcule `status`, enregistre, publie `agent.updated`."""
        a.status = self.derive_status(a, self.store.live_run(a.id))
        a = self.store.save(a, touch=touch)
        if publish:
            self._emit("agent.updated", {"agent": agent_to_json(a)})
        return a

    def publish_run(self, run: AgentRun) -> None:
        self._emit("agent.run", {"agent_id": run.agent_id, "run": run_to_json(run)})
        for listener in list(self.run_listeners):
            try:
                listener(run)
            except Exception:  # noqa: BLE001
                logger.exception("abonné des passages en échec (%s)", run.id)

    def _check_event_writes(self, trigger: dict, rules: list[str]) -> None:
        if trigger.get("kind") == "event":
            writes = self.write_rules(rules)
            if writes:
                raise AgentError("auto_approve : un agent déclenché par un événement ne lève jamais d'écriture "
                                 f"({', '.join(writes)}) — ses écritures passent par une carte")

    def _check_engine(self, trigger: dict) -> None:
        """Sans moteur, rien ne tournerait à l'échéance ni sur l'événement : refusé net plutôt qu'armé pour rien."""
        if self.engine is None and (trigger or {}).get("kind", "manual") != "manual":
            raise AgentError(NO_ENGINE_SCHEDULED)

    def _cancel_arm_card(self, a: Agent, why: str) -> None:
        if a.arm_approval_id and self.engine is not None:
            self.engine.drop_card(a.arm_approval_id, why)
        a.arm_approval_id = None

    # ─────────────────────────────── lecture ───────────────────────────────

    def list_agents(self) -> list[Agent]:
        return self.store.list_all()

    def get_agent(self, agent_id: str) -> Agent:
        return self.store.require(agent_id)

    def list_runs(self, agent_id: str, *, limit: int = 20, before: str | None = None
                  ) -> tuple[list[AgentRun], str | None]:
        self.store.require(agent_id)
        return self.store.runs(agent_id, limit=limit, before=before)

    # ─────────────────────────────── création, modification ───────────────────────────────

    def create_agent(self, *, name: str, role: str, model: str = "", tools: list[str] | None = None,
                     created_by: str = "nina", **fields: Any) -> Agent:
        """Champs §9.5 déjà validés (`create_from_body`) ou absents (défauts : manuel, sans secret). Manuel sans règle
        d'écriture → `armed` ; sinon `paused` (à armer, avec la carte forte s'il lève une écriture)."""
        trigger = fields.get("trigger") or {"kind": "manual"}
        rules = fields.get("auto_approve") or []
        self._check_engine(trigger)
        self._check_event_writes(trigger, rules)
        # Placé sur une machine (X15) : ses portes à elle, aucune règle levée — rien à signer pour l'armer.
        remote = self.is_remote(Agent(id="", name="", role="", model="", placement=fields.get("placement")))
        armed = trigger.get("kind") == "manual" and (remote or not self.write_rules(rules))
        a = self.store.create(name=name, role=role, model=model, tools=tools, created_by=created_by,
                              status="armed" if armed else "paused", armed=armed, **fields)
        self._emit("agent.created", {"agent": agent_to_json(a)})
        return a

    def create_from_body(self, body: dict[str, Any], created_by: str) -> Agent:
        f = norm_fields(body, partial=False, local_machine=self._machine(), policy=self.placement_policy,
                        owner=created_by)
        return self.create_agent(created_by=created_by, **f)

    def patch_agent(self, agent_id: str, body: dict[str, Any], by: str) -> Agent:
        a = self.store.require(agent_id)
        self._not_moved(a)
        f = norm_fields(body, partial=True, local_machine=self._machine(), policy=self.placement_policy,
                        owner=a.created_by)
        if self.mover is not None and "placement" in f and f["placement"] != a.placement:
            # Changer de maison = un déménagement contrôlé (LOKKAL §6.7, X15) : la destination est vérifiée là-bas.
            raise AgentError("placement : un changement de machine passe par POST /v1/agents/{id}/move (il vérifie "
                             "ce que la destination offre)")
        self._check_engine(f.get("trigger", a.trigger))
        self._check_event_writes(f.get("trigger", a.trigger), f.get("auto_approve", a.auto_approve))
        changed = [k for k in DISARMING_FIELDS if k in f and f[k] != getattr(a, k)]
        for k, v in f.items():
            setattr(a, k, v)
        if changed:
            self._cancel_arm_card(a, f"agent modifié par {by} ({', '.join(changed)})")
            if a.armed:
                a.armed = False  # §9.5 : ce que l'humain a armé n'est plus ce qui tournerait
                a.next_run_at = None
        return self.refresh(a)

    # ─────────────────────────────── armer, désarmer ───────────────────────────────

    def arm(self, agent_id: str, by: str) -> tuple[Agent, str | None]:
        """(agent, None) si armé tout de suite ; (agent, approval_id) si une carte FORTE doit d'abord être signée
        (règle d'écriture dans `auto_approve`, pas encore approuvée sous cette forme)."""
        a = self.store.require(agent_id)
        self._not_moved(a)
        self.check_placement(a)
        self._check_engine(a.trigger)  # agent programmé d'avant ce contrôle : il ne s'arme plus sans moteur
        now = self.clock()
        if a.trigger.get("kind") == "once" and parse_when(a.trigger["at"]) <= now:
            raise AgentConflict("trigger once : échéance passée, choisir une nouvelle date (PATCH)")
        writes = self.write_rules(a.auto_approve)
        # Passages envoyés à une machine (X15) : ses portes à elle, aucune règle levée — pas de carte d'armement.
        if writes and not self.is_remote(a) and a.approved_rules != rules_fingerprint(writes):
            if self.engine is None:
                raise EngineUnavailable(NO_ENGINE_WRITE)
            if a.arm_approval_id and self.engine.card_pending(a.arm_approval_id):
                return a, a.arm_approval_id
            a.arm_approval_id = self.engine.request_arm_card(a, writes, rules_fingerprint(writes), by)
            a = self.refresh(a)
            return a, a.arm_approval_id
        return self._armed(a, now), None

    def _armed(self, a: Agent, now: float) -> Agent:
        a.armed = True
        a.arm_approval_id = None
        a.next_run_at = self.next_occurrence(a, now)
        return self.refresh(a)

    def arm_decided(self, agent_id: str, approved: bool, fingerprint: str, by: str | None) -> str:
        """Décision de la carte forte d'armement (exécuteur d'approbation, toutes origines : local, téléphone)."""
        a = self.store.get(agent_id)
        if a is None:
            raise AgentNotFound("agent supprimé depuis la demande")
        a.arm_approval_id = None
        if not approved:
            self.refresh(a)
            return "armement refusé"
        if rules_fingerprint(self.write_rules(a.auto_approve)) != fingerprint:
            self.refresh(a)
            raise AgentConflict("règles auto_approve changées depuis la demande : réarmer")
        a.approved_rules = fingerprint
        self._armed(a, self.clock())
        return f"agent « {a.name} » armé par {by or 'inconnu'}"

    def disarm(self, agent_id: str, by: str) -> Agent:
        a = self.store.require(agent_id)
        self._cancel_arm_card(a, f"désarmé par {by}")
        a.armed = False
        a.next_run_at = None
        return self.refresh(a)

    # ─────────────────────────────── passages ───────────────────────────────

    def run_now(self, agent_id: str, by: str) -> AgentRun:
        if self.engine is None:
            raise EngineUnavailable("moteur Crew non branché sur cette instance")
        a = self.store.require(agent_id)
        self._not_moved(a)
        if not getattr(self.engine, "local_runs", True) and not self.is_remote(a):
            raise EngineUnavailable(self.engine.cannot_run(a) or "moteur des agents éteint sur cette instance")
        return self.engine.enqueue(a, "manual")

    # ─────────────────────────────── placement, déménagement (X15) ───────────────────────────────

    @staticmethod
    def moved_to(a: Agent) -> str | None:
        """Agent déménagé ailleurs (pierre tombale sur la machine qu'il a quittée) : sa destination, sinon None."""
        p = a.placement or {}
        return str(p.get("machine") or "?") if p.get("moved_at") else None

    def _not_moved(self, a: Agent) -> None:
        to = self.moved_to(a)
        if to:
            raise AgentConflict(f"agent déménagé (vers {to}) : il vit désormais là-bas, ses passages d'ici restent "
                                "lisibles")

    def check_placement(self, a: Agent) -> dict[str, Any] | None:
        """Le placement de `a` est-il encore valable ici (AgentError sinon) ? Lokkal : null ou cette machine ;
        nokker.ai : `auto`, `web` (offre), une machine du compte."""
        return norm_placement(a.placement, self._machine(), policy=self.placement_policy, owner=a.created_by)

    def is_remote(self, a: Agent) -> bool:
        """Ses passages partent vers une machine (nokker.ai : `auto` ou `mc_…`)."""
        from nokker.agents.spec import placement_target

        if self.placement_policy is None or self.moved_to(a):
            return False
        try:
            return self.placement_policy.is_remote(placement_target(a.placement))
        except AgentError:
            return False

    def move(self, agent_id: str, to: Any, by: str, tenant: str) -> Agent:
        """`POST /v1/agents/{id}/move {to}` (LOKKAL §6.7) : refusé avec la liste des manques (`AgentMissing`), sinon la
        définition change de maison. L'historique des passages reste là où ils ont tourné."""
        if self.mover is None:
            raise AgentError("le déménagement d'un agent se fait depuis le deck de nokker.ai (machine liée)")
        return self.mover.move(self, agent_id, to, by, tenant)

    def cancel_run(self, agent_id: str, run_id: str, by: str) -> AgentRun:
        run = self.store.get_run(run_id)
        if run is None or run.agent_id != agent_id:
            raise AgentNotFound(f"passage {run_id}")
        if self.engine is None:
            return run
        return self.engine.cancel(run, by)

    def delete_agent(self, agent_id: str, by: str) -> None:
        a = self.store.require(agent_id)
        live = self.store.live_run(agent_id)
        if live is not None and self.engine is not None:
            self.engine.cancel(live, by)
        self._cancel_arm_card(a, f"agent supprimé par {by}")
        self.store.delete(agent_id)
        self._emit("agent.deleted", {"id": agent_id})

    def webhook_secret(self, agent_id: str) -> tuple[str, str]:
        """Nouvelle clé HMAC du déclencheur `webhook:<id>` (l'ancienne ne vaut plus) : (clé, id du webhook). Rendue
        UNE fois ; le noyau n'en garde que la forme chiffrée si `NOKKER_AGENTS_ENC_KEY` est posée."""
        if self.engine is None:
            raise EngineUnavailable("moteur Crew non branché sur cette instance")
        a = self.store.require(agent_id)
        event = str(a.trigger.get("event") or "")
        if a.trigger.get("kind") != "event" or not event.startswith("webhook:"):
            raise AgentError("trigger : l'agent n'est pas déclenché par webhook:<id>")
        key, stored = self.engine.new_webhook_key()
        a.webhook_key = stored
        self.store.save(a)
        return key, event.split(":", 1)[1]


__all__ = ["DISARMING_FIELDS", "NO_ENGINE_SCHEDULED", "NO_ENGINE_WRITE", "AgentsHub", "EngineUnavailable",
           "rules_fingerprint"]
