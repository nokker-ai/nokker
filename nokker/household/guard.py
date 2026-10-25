"""Garde-fous des profils mineurs d'un foyer (lot X23, drapeau `household_minors`, docs/LOKKAL.md §7.29.4).

Ce que le noyau applique à un tour dont l'expéditeur est un profil `teen` ou `child` :

  - **Envoi externe et action destructive** : toujours une carte FORTE, règle `household:guardian`, décidée par le
    REPRÉSENTANT (le payeur) depuis SES appareils de confiance, jamais par le mineur (approvals/base.py
    `decider_allowed`). Ni une liste d'adresses permises, ni un « GO » écrit, ni une règle d'agent ne la lèvent
    (`_FLOOR_RULES` du moteur Crew). L'envoi de masse reste refusé.
  - **Connecteurs et agents** : seulement ceux de la liste blanche du foyer (`household_policy`) ; un connecteur hors
    liste n'apparaît pas dans le registre du mineur, l'outil de brouillon d'agent non plus sans agent permis.
  - **Préfixe adapté à l'âge** : `prompts/prefix-young.md` après le préfixe produit, avec la tranche d'âge.
  - **Profil suspendu** (attestation retirée, foyer quitté, drapeau éteint) : aucun tour (nokker/household/quota.py).

Le reste (consentement d'entraînement, SSO, miroir, pages publiques, création d'agents) est refusé aux frontières
qui les servent, par `Household.is_minor`.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nokker.core.ports import ToolSpec, call_effect
from nokker.core.turn import Decision, Effect, Gate, ToolCall, Turn, Verdict
from nokker.household.model import GUARDIAN_RULE

YOUNG_PREFIX_PATH = Path(__file__).resolve().parent.parent / "prompts" / "prefix-young.md"
AGE_LINE = {"teen": "La personne qui t'écrit a entre 13 et 17 ans. / The person writing to you is 13 to 17 years old.",
            "child": "La personne qui t'écrit a moins de 13 ans. / The person writing to you is under 13 years old."}
AGENT_TOOLS = frozenset({"agent_draft", "pack_install", "pack_remove"})  # agents et packs d'agents (§7.30)


@dataclass
class MinorGates:
    """Portes du noyau précédées de la règle des profils mineurs (même forme que `qa/permissions.PermissionGates`)."""

    inner: Any
    minor_state: Callable[[str | None], Any]  # Household.minor_state

    def action(self, call: ToolCall, spec: ToolSpec | None, turn: Turn) -> list[Verdict]:
        st = self.minor_state(getattr(turn, "sender", None))
        if st is None:
            return self.inner.action(call, spec, turn)
        effect = call_effect(spec, call.arguments)
        if effect in (Effect.SEND_EXTERNAL, Effect.DESTRUCTIVE, Effect.SEND_BULK):
            inv = getattr(self.inner, "invariants", None)
            v = inv.check_action(call, spec, turn) if inv is not None else None
            if v is not None and v.decision is Decision.DENY:  # envoi de masse : refusé, pas de carte
                return [v]
            if effect is Effect.SEND_BULK:
                return [Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule="effect:send_bulk",
                                reason="envoi de masse toujours refusé", subject=call.id)]
            if not st.active:
                return [Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule="household:suspended",
                                reason="profil mineur suspendu", subject=call.id)]
            return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=GUARDIAN_RULE,
                            reason="profil mineur : un envoi vers l'extérieur ou une action irréversible attend "
                                   "l'accord de son représentant légal, depuis ses appareils",
                            subject=call.id, strength="strong")]
        return self.inner.action(call, spec, turn)

    def switch(self, reply: str, turn: Turn, tier: Any) -> list[Verdict]:
        return self.inner.switch(reply, turn, tier)

    def __getattr__(self, name: str) -> Any:  # invariants, deliverable… : ceux des portes du noyau
        return getattr(self.inner, name)


# Outils des sessions de code (§7.28) : jamais offerts à un profil mineur (terminal et écritures dans un bac à sable ;
# les portes d'une session suivent le mode, pas celles du mineur). Sur le web, ces noms ne viennent que de là.
# Sous-agents d'une session (`delegate*`) : jamais non plus. Skills du dépôt d'une session : par leur SOURCE (même nom
# que les skills des packs du web, qui restent ceux du compte).
CODE_SESSION_TOOLS = frozenset({"terminal", "write_file", "patch", "read_file", "search_files", "checkpoint_create",
                                "checkpoint_list", "checkpoint_diff", "checkpoint_restore", "delegate",
                                "delegate_status", "delegate_cancel"})
CODE_SESSION_SOURCES = frozenset({"builtin:code-skills"})


def minor_tool_filter(household: Any) -> Callable[[str], Callable[[ToolSpec], bool] | None]:
    """`TenantRegistry.restrict` : locataire → prédicat des outils offerts, ou None (pas un profil mineur)."""

    def restrict(tenant_id: str) -> Callable[[ToolSpec], bool] | None:
        tenancy = household.team.tenancy
        acc = tenancy.account_for_tenant(tenant_id) if tenancy is not None else None
        st = household.minor_state(acc.account_id) if acc is not None else None
        if st is None:
            return None
        allowed = set(st.policy.minor_connectors) if st.active else set()
        agents = bool(st.policy.minor_agents) and st.active

        def keep(spec: ToolSpec) -> bool:
            source = str(getattr(spec, "source", "") or "")
            if source.startswith("connector:"):
                return source.split(":", 1)[1] in allowed
            if spec.name in AGENT_TOOLS:
                return agents
            return spec.name not in CODE_SESSION_TOOLS and source not in CODE_SESSION_SOURCES

        return keep

    return restrict


@dataclass
class FilteredSource:
    """Une source d'outils vue par un profil mineur : les specs hors liste blanche disparaissent, et un appel par leur
    nom répond « outil inconnu » sans exécution (le registre ne route que vers les specs déclarées)."""

    inner: Any
    keep: Callable[[ToolSpec], bool]

    def specs(self) -> list[ToolSpec]:
        return [s for s in self.inner.specs() if self.keep(s)]

    def call(self, name: str, arguments: dict[str, Any], ctx: Any) -> Any:
        from nokker.core.turn import ToolResult

        if not any(s.name == name for s in self.specs()):
            return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)
        return self.inner.call(name, arguments, ctx)

    def __getattr__(self, name: str) -> Any:  # refresh, turn_done, available_for… : ceux de la source
        return getattr(self.inner, name)


@dataclass
class TenantMinorCache:
    """Locataire → profil mineur actif (`teen` | `child`) ou None, gardé `ttl_s` secondes : l'assemblage d'un tour le
    lit plusieurs fois (préfixe, empreinte). `forget(tid)` à chaque changement (Household.on_change). Un profil
    suspendu n'a pas de tour (quota) : None ici aussi."""

    household: Any
    ttl_s: float = 30.0

    def __post_init__(self) -> None:
        import threading

        self._cache: dict[str, tuple[float, str | None]] = {}
        self._lock = threading.Lock()

    def __call__(self, tenant_id: str) -> str | None:
        import time

        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(tenant_id)
        if hit is not None and now - hit[0] < self.ttl_s:
            return hit[1]
        tenancy = self.household.team.tenancy
        acc = tenancy.account_for_tenant(tenant_id) if tenancy is not None else None
        st = self.household.minor_state(acc.account_id) if acc is not None else None
        profile = st.profile if st is not None else None
        with self._lock:
            self._cache[tenant_id] = (now, profile)
        return profile

    def forget(self, tenant_id: str) -> None:
        with self._lock:
            self._cache.pop(tenant_id, None)


def young_prefix(profile: str) -> str:
    """Addendum du préfixe pour un profil mineur : le fichier dédié (neutre) et la tranche d'âge."""
    text = YOUNG_PREFIX_PATH.read_text(encoding="utf-8") if YOUNG_PREFIX_PATH.exists() else ""
    return f"{text.rstrip()}\n\n{AGE_LINE.get(profile, AGE_LINE['child'])}\n"


__all__ = ["AGENT_TOOLS", "CODE_SESSION_SOURCES", "CODE_SESSION_TOOLS", "FilteredSource", "MinorGates",
           "TenantMinorCache", "minor_tool_filter", "young_prefix"]
