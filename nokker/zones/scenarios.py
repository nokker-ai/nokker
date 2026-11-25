"""Scénarios lancés par une zone (docs/LOKKAL-PHONE.md §4.2) : interface `ScenarioSource` et son implémentation sur le
registre des scénarios de la maison (`nokker/home/scenarios.py::ScenarioStore`, chantier ha-full : automatisations HA
`nokker_…`, propriétaire et force enregistrés à la création).

Règles, au nom du MEMBRE dont c'est la zone :
- un scénario n'est lancé que par son propriétaire, ou par un adulte du foyer (même règle que « mes scénarios ») ;
- scénario FORT (une serrure, une alarme… dans ses actions, force du registre) : jamais exécuté directement. Une carte
  forte est proposée (`card`), décidée par un humain sur son appareil. Un membre dont le rôle n'a AUCUN droit fort
  (profils mineurs, invité) ne la reçoit pas : `skipped` (`not_allowed`), car il ne pourrait pas le lancer à la main ;
- scénario direct : `automation.trigger` de son automatisation, par la passerelle (`started`) ;
- l'automatisation absente de HA, ou la passerelle en panne : `skipped`, avec une raison.
`bridge` : `home-bridge` (Lokkal avec la maison), `home-node` (nœud maison du téléphone, lot P10), sinon None.
"""
from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from typing import Any, Protocol

from nokker.homebridge.ha import tracker_id  # même règle des deux côtés (noyau, passerelle)

logger = logging.getLogger("nokker.zones")

ZONE_SCENARIO_TOOL = "zone_scenario"  # carte forte d'un scénario lancé par une zone : un geste du noyau
STARTED, CARD, SKIPPED = "started", "card", "skipped"


class ScenarioSource(Protocol):
    bridge: str | None

    def list(self, member: str) -> list[dict[str, Any]]: ...

    def run(self, member: str, scenario_id: str, *, zone: str, event: str) -> dict[str, Any]: ...

    def see(self, member: str, location_name: str) -> bool: ...


def location_for(zone: Any, event: str) -> str:
    """Lieu écrit dans HA : la zone `home` donne `home` en entrée ; une sortie donne `not_home` ; une autre zone
    donne son nom en entrée. Aucune devinette par le nom (dépendante de la langue)."""
    if event == "exit":
        return "not_home"
    return "home" if zone.home else zone.name[:40]


class RegistryScenarios:
    """`ScenarioSource` sur la source maison de Lokkal : son registre (`home.scenarios`), sa politique (`home.policy`)
    et sa passerelle (`home.client.session()`). `request_card(member, scenario_id, name, zone, event) → approval_id`."""

    bridge = "home-bridge"

    def __init__(self, home: Any, request_card: Callable[..., str] | None = None) -> None:
        self.home, self.request_card = home, request_card

    @property
    def _registry(self) -> Any:
        return getattr(self.home, "scenarios", None)

    def _may(self, member: str, item: dict[str, Any]) -> bool:
        return item.get("owner") == member or self.home.policy.role_of(member) == "adult"

    def list(self, member: str) -> list[dict[str, Any]]:
        reg = self._registry
        if reg is None:
            return []
        return [{"id": sid, "name": str(v.get("nom") or sid), "strong": bool(v.get("strong"))}
                for sid, v in sorted(reg.items().items()) if self._may(member, v)]

    def _entity(self, session: Any, scenario_id: str) -> str | None:
        for st in session.states():
            eid = str(st.get("entity_id") or "")
            if eid.startswith("automation.") and str((st.get("attributes") or {}).get("id") or "") == scenario_id:
                return eid
        return None

    def run(self, member: str, scenario_id: str, *, zone: str, event: str) -> dict[str, Any]:
        out = {"id": scenario_id}
        reg = self._registry
        item = reg.get(scenario_id) if reg is not None and scenario_id.startswith("nokker_") else None
        if item is None:
            return {**out, "status": SKIPPED, "reason": "unknown_scenario"}
        if not self._may(member, item):
            return {**out, "status": SKIPPED, "reason": "not_allowed"}
        if item.get("strong"):
            if not self.home.policy.allowed(member, "strong", "automation") or self.request_card is None:
                return {**out, "status": SKIPPED, "reason": "not_allowed"}
            try:
                approval = self.request_card(member, scenario_id, str(item.get("nom") or scenario_id), zone, event)
            except Exception:  # noqa: BLE001
                logger.warning("zone : carte du scénario %s non posée", scenario_id, exc_info=True)
                return {**out, "status": SKIPPED, "reason": "card_failed"}
            return {**out, "status": CARD, "approval_id": approval}
        return {**out, **self.trigger(scenario_id)}

    def trigger(self, scenario_id: str) -> dict[str, Any]:
        """`automation.trigger` de l'automatisation du scénario, par la passerelle."""
        try:
            session = self.home.client.session()
            try:
                eid = self._entity(session, scenario_id)
                if eid is None:
                    return {"status": SKIPPED, "reason": "absent"}
                session.call_service("automation", "trigger", [eid], {})
            finally:
                session.close()
        except Exception:  # noqa: BLE001 — passerelle ou HA en panne : rien n'est lancé, raison lisible
            logger.warning("zone : scénario %s non lancé", scenario_id, exc_info=True)
            return {"status": SKIPPED, "reason": "bridge_error"}
        return {"status": STARTED}

    def see(self, member: str, location_name: str) -> bool:
        """Présence du membre dans HA (`device_tracker.see` du traceur `nokker_<membre>`) : les scénarios d'arrivée et
        de départ de ha-full (déclencheurs sur `person`/`device_tracker`) partent alors dans HA."""
        try:
            session = self.home.client.session()
            try:
                session.tracker_see(tracker_id(member), location_name)
            finally:
                session.close()
        except Exception:  # noqa: BLE001
            logger.warning("zone : présence HA de %s non écrite", member, exc_info=True)
            return False
        return True

    def on_decision(self, approval: Any) -> str:
        """Carte forte décidée (exécuteur d'approbation) : lancé seulement si accordée ET toujours permis."""
        args = approval.arguments or {}
        sid, member = str(args.get("scenario") or ""), str(args.get("member") or "")
        if approval.status != "approved":
            return "scénario non lancé"
        reg = self._registry
        item = reg.get(sid) if reg is not None else None
        if item is None or not self._may(member, item):
            return "scénario retiré ou plus permis : rien n'est lancé"
        got = self.trigger(sid)
        return "scénario lancé" if got["status"] == STARTED else f"scénario non lancé ({got.get('reason')})"


def card_requester(approvals: Any, tenant_of: Callable[[str], str]) -> Callable[..., str]:
    """Pose la carte forte d'un scénario de zone (`ZONE_SCENARIO_TOOL`), sur un fil du noyau propre au membre."""

    def request(member: str, scenario_id: str, name: str, zone: str, event: str) -> str:
        from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
        from nokker.qa.untrusted import inline
        from nokker.tenancy.context import scoped_thread_id

        turn = Turn(thread_id=scoped_thread_id(tenant_of(member), f"zone-{tracker_id(member)}"), index=0,
                    input=InputCheck(text=""), channel="kernel", sender=member)
        call = ToolCall(id=f"zone-{secrets.token_hex(6)}", name=ZONE_SCENARIO_TOOL,
                        arguments={"member": member, "scenario": scenario_id, "zone": zone, "event": event})
        what = "Arrivée" if event == "enter" else "Départ"
        reason = (f"{what} : « {inline(zone, 40)} ». Lancer le scénario « {inline(name, 60)} » "
                  "(il contient une action forte : serrure, alarme…).")
        verdict = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="zone:scenario", reason=reason,
                          subject=call.id, strength="strong")
        return approvals.request(turn, call, verdict, None).id

    return request


__all__ = ["CARD", "SKIPPED", "STARTED", "ZONE_SCENARIO_TOOL", "RegistryScenarios", "ScenarioSource",
           "card_requester", "location_for", "tracker_id"]
