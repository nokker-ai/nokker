"""Ressources du noyau par locataire (docs/TENANCY.md §5) : outils et mémoire longue.

Le noyau est construit une fois (`api/server.py::build_kernel`) et sert tous les locataires : le choix se fait donc à
CHAQUE appel, d'après `current_tenant()`, jamais à la construction. Hors d'un `tenant_scope`, tout appel lève `NoTenant`.

- Interne (`tn_ninabot`) : le registre et la mémoire d'avant le multi-locataire, inchangés.
- Testeur : cœurs + `recall_context` (mémoire de session de SON fil) + web + bulles + SES connecteurs
  (`ConnectorProvider`). Jamais les MCP partagés (`mcp_tool_servers` : workspace, navigateur partagé de l'opérateur) ni sokkan-memory
  (`memory_search`/`memory_get`, rappel automatique) : un nom absent du jeu répond « outil inconnu », sans exécution.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from nokker.core.ports import MemoryPort, Recall, ToolContext, ToolSource, ToolSpec
from nokker.core.turn import ToolResult
from nokker.tenancy.connectors import ConnectorProvider, NoConnectors
from nokker.tenancy.context import INTERNAL_TENANT, current_tenant
from nokker.tools.registry import Registry

logger = logging.getLogger("nokker.tenancy.resources")


class TenantRegistry:
    """`core.ports.ToolRegistry` qui délègue au jeu du locataire courant. Partagé tel quel avec le ContextEngine
    (`self.tools`) et l'exécuteur d'approbations : chacun voit le jeu du locataire du tour en cours.

    Un `Registry` par testeur, en cache (son cache de specs est donc stable d'un tour à l'autre, comme le hash du
    préfixe) ; `forget(tid)` le reconstruit au prochain appel (connecteur ajouté ou retiré, machine Lokkal liée ou
    révoquée), `refresh()` les oublie tous et rafraîchit le registre interne, qui contient toutes les sources
    partagées. Une source qui déclare `available_for(tid)` n'est offerte qu'aux locataires pour qui elle rend vrai."""

    def __init__(self, internal: Registry, tester_sources: list[ToolSource],
                 connectors: ConnectorProvider | None = None) -> None:
        self.internal = internal
        self.tester_sources = list(tester_sources)
        self.connectors: ConnectorProvider = connectors or NoConnectors()
        # Profil mineur d'un foyer (lot X23, `nokker/household/guard.py::minor_tool_filter`) : locataire → prédicat des
        # outils offerts (liste blanche des connecteurs et agents), ou None. None (défaut) : rien ne change.
        self.restrict: Any = None
        self._tenants: dict[str, Registry] = {}
        self._lock = threading.Lock()

    def current(self) -> Registry:
        tid = current_tenant()
        if tid == INTERNAL_TENANT:
            return self.internal
        with self._lock:
            reg = self._tenants.get(tid)
        if reg is not None:
            return reg
        sources, sure = self._available(tid)
        sources = [*sources, *self.connectors.sources_for(tid)]
        keep = self.restrict(tid) if self.restrict is not None else None
        if keep is not None:
            from nokker.household.guard import FilteredSource

            sources = [FilteredSource(src, keep) for src in sources]
        reg = Registry(sources=sources)
        if not sure:
            return reg  # disponibilité illisible : rien en cache, on relira au prochain appel
        with self._lock:
            return self._tenants.setdefault(tid, reg)

    def _available(self, tid: str) -> tuple[list[ToolSource], bool]:
        """Sources du testeur offertes à CE locataire : une source qui déclare `available_for(tid)` n'y est que si elle
        rend vrai (outils Lokkal : machine liée et non révoquée). Une vérification en échec écarte la source, sans
        mettre le résultat en cache (`sure` faux)."""
        out: list[ToolSource] = []
        sure = True
        for src in self.tester_sources:
            check = getattr(src, "available_for", None)
            if check is None:
                out.append(src)
                continue
            try:
                if check(tid):
                    out.append(src)
            except Exception:  # noqa: BLE001 — base injoignable : l'outil manque à ce tour, il reviendra
                logger.warning("disponibilité de %s illisible pour %s", type(src).__name__, tid, exc_info=True)
                sure = False
        return out, sure

    def forget(self, tenant_id: str) -> None:
        with self._lock:
            self._tenants.pop(tenant_id, None)

    # ───────── core.ports.ToolRegistry ─────────

    def core(self) -> list[ToolSpec]:
        return self.current().core()

    def spec(self, name: str) -> ToolSpec | None:
        return self.current().spec(name)

    def find(self, query: str, limit: int = 5) -> list[ToolSpec]:
        return self.current().find(query, limit)

    def index(self) -> list[tuple[str, str]]:
        return self.current().index()

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return self.current().call(name, arguments, ctx)

    # ───────── crochets de Registry ─────────

    def refresh(self) -> None:
        self.internal.refresh()
        with self._lock:
            self._tenants.clear()

    def turn_done(self, thread_id: str) -> None:
        self.current().turn_done(thread_id)

    def close(self) -> None:
        # Les sources partagées sont toutes dans le registre interne ; celles des connecteurs appartiennent au
        # fournisseur (ConnectorProvider).
        self.internal.close()


class NullMemory:
    """Aucune mémoire longue : pas de rappel, rien de proposé, aucun outil. Celle d'un testeur, et celle de tout le
    monde quand `memory_mcp_url` est vide (bêta OVH : aucune connexion à la mémoire de l'instance interne)."""

    def recall(self, query: str, k: int = 5) -> list[Recall]:
        return []

    def propose(self, thread_id: str, facts: list[str]) -> None:
        return None

    def specs(self) -> list[ToolSpec]:
        return []

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)


class TenantMemory:
    """`core.ports.MemoryPort` du noyau : la mémoire de l'interne pour `tn_ninabot`, `NullMemory` pour tout autre.

    `recall` est résolu à l'ACCÈS (`kernel.memory.recall`), dans le fil de l'appelant, où le locataire est fixé : le
    rappel part ensuite dans un pool de threads (`core/loop.py`) avec la bonne mémoire déjà choisie, que le pool emporte
    le contexte (`submit_in_context`) ou non. Hors scope, l'accès lève `NoTenant`."""

    def __init__(self, internal: MemoryPort,
                 tenant_memory: Callable[[str], MemoryPort | None] | None = None) -> None:
        self.internal = internal
        self.null = NullMemory()
        # Mémoire d'un locataire testeur quand il en a une : la mémoire commune de son foyer (lot X23,
        # nokker/household/memory.py) ; None (défaut, drapeau éteint) ou rien pour ce locataire : NullMemory.
        self.tenant_memory = tenant_memory

    def for_tenant(self) -> MemoryPort:
        tid = current_tenant()
        if tid == INTERNAL_TENANT:
            return self.internal
        if self.tenant_memory is not None:
            try:
                mem = self.tenant_memory(tid)
            except Exception:  # noqa: BLE001 — une mémoire illisible ne casse jamais le tour : rien de rappelé
                logger.warning("mémoire du locataire %s illisible", tid, exc_info=True)
                mem = None
            if mem is not None:
                return mem
        return self.null

    @property
    def recall(self) -> Callable[..., list[Recall]]:
        return self.for_tenant().recall

    @property
    def recall_for(self) -> Callable[..., list[Recall]]:
        """Rappel qui connaît le fil (mémoire de projet, X1) ; une mémoire qui ne le porte pas : `recall` tel quel."""
        mem = self.for_tenant()
        fn = getattr(mem, "recall_for", None)
        return fn if fn is not None else (lambda thread_id, query, k=5: mem.recall(query, k))

    def propose(self, thread_id: str, facts: list[str]) -> None:
        self.for_tenant().propose(thread_id, facts)


class TenantEscalator:
    """`core.ports.Escalator` : celui de l'interne (« Je transmets à l'équipe ») pour `tn_ninabot` ; chez un testeur, aucun
    destinataire interne nommé — la décision revient à l'utilisateur."""

    def __init__(self, internal: Any) -> None:
        self.internal = internal

    def escalate(self, turn: Any, reason: str) -> str:
        if current_tenant() == INTERNAL_TENANT:
            return self.internal.escalate(turn, reason)
        logger.info("escalade (testeur) : fil=%s tour=%d motif=%s", turn.thread_id, turn.index, reason)
        head = "Je m'arrête ici : cette décision te revient, ou à une personne compétente"
        return f"{head} — {reason}." if reason else f"{head}."


__all__ = ["NullMemory", "TenantEscalator", "TenantMemory", "TenantRegistry"]
