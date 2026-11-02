"""Droits dans la salle Maison (docs/HOUSE-ROOM.md §3) : calculés sur le MEMBRE qui parle, jamais sur la salle.

- `RoomMember` : l'identité humaine d'un message (compte, SON locataire, foyer, profil, payeur), lue en direct dans le
  foyer à chaque message (un départ ou une suspension ferme la salle au message suivant) ;
- `may_address` : un agent ne répond qu'aux profils de sa liste (`AgentDef.profiles`) ;
- `HouseGates` : portes des outils de la maison, posées AVANT celles du noyau du membre (qui restent : un profil
  mineur garde sa carte forte du représentant, un refus reste un refus). Le niveau d'un appel vient de son effet :
  lecture → `read`, écriture → `act` (carte standard selon le mode du membre : le toc-toc peut la décider), effet
  destructif, envoi externe ou dépense → `strong` (carte FORTE, dans le locataire du membre, décidée depuis SES
  appareils : une carte forte reste individuelle, jamais décidée par la salle ni par un autre membre). Au-delà du
  droit du profil (`Settings.rights`) : refus `house:acl`, sans carte.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nokker.core.ports import call_effect
from nokker.core.turn import Decision, Effect, Gate, ToolCall, Turn, Verdict
from nokker.house.roster import RIGHTS, AgentDef, Settings

ACL_RULE = "house:acl"
STRONG_RULE = "house:strong"
SUSPENDED_RULE = "house:suspended"
PROFILE_LABEL = {"adult": "adulte", "teen": "ado, 13-17 ans", "child": "enfant, moins de 13 ans"}


@dataclass(frozen=True)
class RoomMember:
    account_id: str
    tenant_id: str  # SON locataire : ses tours, ses fils, ses cartes
    household_id: str
    household_tenant: str  # locataire du foyer (le payeur) : la salle y vit
    profile: str
    payer: bool
    active: bool  # adhésion active (et, pour un mineur, profil non suspendu)
    handle: str = ""  # « membre A », « membre B »… (ordre d'entrée) : ce que la salle et le modèle voient de lui

    @property
    def minor(self) -> bool:
        return self.profile in ("teen", "child")

    def label(self) -> str:
        who = "le payeur" if self.payer else self.handle or "un membre"
        return f"{who} ({PROFILE_LABEL.get(self.profile, self.profile)})"


def handles_of(rows: list[Any]) -> dict[str, str]:
    """Compte → « membre A/B/… » par ordre d'entrée dans le foyer (payeur compris) : stable tant que personne ne part."""
    ordered = sorted(rows, key=lambda r: (r.added_at, r.account_id))
    return {r.account_id: f"membre {chr(ord('A') + i)}" for i, r in enumerate(ordered)}


def resolve_member(household: Any, account_id: str | None) -> RoomMember | None:
    """Le membre du foyer derrière ce compte, lu dans SON locataire (`Household.membership`), ou None (pas de foyer,
    compte inactif, foyer d'un autre type). Un profil mineur suspendu est rendu `active=False`."""
    if household is None or not account_id:
        return None
    found = household.membership(account_id)
    if found is None:
        return None
    acc, p, row = found
    active = row.status == "active"
    if row.minor:
        st = household.minor_state(account_id)
        active = active and st is not None and st.active
    try:
        rows = household.store.hh_members(acc, p.id)
    except Exception:  # noqa: BLE001 — un libellé manquant ne ferme pas la salle
        rows = [row]
    return RoomMember(account_id=account_id, tenant_id=acc.tenant_id, household_id=p.id, household_tenant=p.tenant_id,
                      profile=row.profile, payer=bool(row.payer), active=active,
                      handle=handles_of(rows).get(account_id, ""))


def right_of(member: RoomMember, settings: Settings) -> str:
    if not member.active:
        return "none"
    return settings.rights.get(member.profile, "none")


def may_address(member: RoomMember, agent: AgentDef) -> bool:
    return member.active and agent.enabled and member.profile in agent.profiles


def level_of(effect: Effect) -> str:
    if effect is Effect.READ:
        return "read"
    if effect is Effect.WRITE:
        return "act"
    return "strong"  # DESTRUCTIVE, SEND_EXTERNAL, SEND_BULK, SPEND, SECRET : jamais une action ordinaire


def allowed(level: str, right: str) -> bool:
    return RIGHTS.index(level) <= RIGHTS.index(right)


@dataclass
class HouseGates:
    """Portes d'un tour de la salle : le droit du MEMBRE (relu à chaque appel par `member_of(sender)`), puis les portes
    du noyau. Ne lève jamais un refus ni une carte des portes du noyau ; ajoute seulement."""

    inner: Any
    member_of: Callable[[str | None], RoomMember | None]
    settings: Settings
    house_tools: frozenset[str]  # outils de la maison de CE noyau de salle (les autres : portes du noyau seules)

    def action(self, call: ToolCall, spec: Any, turn: Turn) -> list[Verdict]:
        if call.name not in self.house_tools:
            return list(self.inner.action(call, spec, turn))
        m = self.member_of(getattr(turn, "sender", None))
        if m is None or not m.active:
            return [Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule=SUSPENDED_RULE,
                            reason="salle Maison : la personne n'est plus membre active du foyer", subject=call.id)]
        level = level_of(call_effect(spec, call.arguments))
        right = right_of(m, self.settings)
        if not allowed(level, right):
            return [Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule=ACL_RULE,
                            reason=f"salle Maison : le profil {PROFILE_LABEL.get(m.profile, m.profile)} n'a pas ce "
                                   f"droit ({level})", subject=call.id)]
        verdicts = list(self.inner.action(call, spec, turn))
        if level == "strong" and not any(v.decision is Decision.DENY for v in verdicts):
            # Carte FORTE individuelle : dans le locataire du membre (son fil), décidée depuis SES appareils.
            verdicts = [v for v in verdicts if v.decision is not Decision.ALLOW] + [Verdict(
                gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=STRONG_RULE,
                reason="salle Maison : action sensible, carte forte de la personne qui la demande",
                subject=call.id, strength="strong")]
        return verdicts

    def switch(self, reply: str, turn: Turn, tier: Any) -> list[Verdict]:
        return self.inner.switch(reply, turn, tier)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


__all__ = ["ACL_RULE", "PROFILE_LABEL", "STRONG_RULE", "SUSPENDED_RULE", "HouseGates", "RoomMember", "allowed",
           "handles_of", "level_of", "may_address", "resolve_member", "right_of"]
