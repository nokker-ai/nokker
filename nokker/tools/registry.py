"""`ToolRegistry` (noyau) : agrège des `ToolSource` (builtin, MCP…), sépare outils cœur et outils trouvés
à la demande (PLAN-P1 §2.2/§2.4). Les outils cœur (`core=True`) sont ceux du préfixe — au plus ≤ 8 au
total (budget §2.4) ; `index()` donne la liste courte (nom, description) des autres, pour le bloc du tour ;
leur schéma complet n'est chargé qu'après un `tool_find` (c'est `core/loop.py` qui gère ce chargement,
`loaded_tools` dans `ContextEngine.assemble`).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.core.loop import ESCALATE_TOOL, FIND_TOOL
from nokker.core.ports import ToolContext, ToolSource, ToolSpec
from nokker.core.turn import Effect, ToolResult

logger = logging.getLogger("nokker.tools.registry")


@dataclass
class StaticToolSource:
    """Un outil « local », implémenté en Python pur (pas de serveur MCP). Utile pour les tests et pour
    les quelques outils cœur qui n'ont pas de sens côté MCP (`clarify`, `recall_context`)."""

    entries: list[tuple[ToolSpec, Callable[[dict[str, Any], ToolContext], ToolResult]]] = field(
        default_factory=list)

    def specs(self) -> list[ToolSpec]:
        return [spec for spec, _ in self.entries]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        for spec, fn in self.entries:
            if spec.name == name:
                return fn(arguments, ctx)
        return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)


def _native_stub(name: str, message: str, param: str, param_desc: str
                 ) -> tuple[ToolSpec, Callable[[dict[str, Any], ToolContext], ToolResult]]:
    """`tool_find`/`escalate_to_human` sont interceptés par `core/loop.py` AVANT d'atteindre le registre
    (`Kernel._tool_step`) — ce `call()` ne doit donc jamais s'exécuter en pratique. Il existe pour que le
    nom ait un schéma minimal déclarable au préfixe et pour échouer clairement s'il était atteint quand
    même (p. ex. un appel direct au registre dans un test)."""
    # Le paramètre déclaré est celui que core/loop.py lit (`query` / `reason`) : schéma = contrat d'entraînement
    # de Nina (lot 2c, nokker_tasks.py), à garder identique des deux côtés (cf docs/CONTEXT-FORMAT.md).
    spec = ToolSpec(name=name, description=message,
                    parameters={"type": "object", "properties": {param: {"type": "string", "description": param_desc}},
                                "required": [param]},
                     effect=Effect.READ, core=True, source="builtin")

    def _fail(_args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
        return ToolResult(call_id="", name=name, content=f"{name} est géré nativement par core/loop.py "
                           "— ce chemin ne devrait pas être atteint.", is_error=True)

    return spec, _fail


def _clarify(args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
    question = str(args.get("question", "")).strip() or "(question vide)"
    return ToolResult(call_id="", name="clarify", content=question)


def builtin_core_source() -> StaticToolSource:
    """Les outils cœur qui ne viennent pas de CortHeXis (`memory_search`/`memory_get` sont exposés par
    `nokker.memory.corthexis.CorthexisMemory`, pas ici) — le préfixe de l'instance interne, §Outils cœur.

    `recall_context` RETIRÉ de la surface (décision Nick, P1-S1, 17.10.2026) : tant que le réservoir réel
    (chantier A-1) n'existe pas, cet outil ne pouvait QUE répondre « indisponible » — mesuré en réel : un
    outil qui répond toujours « indisponible » invite le modèle à réessayer (vu jusqu'à 2x sur la même
    tâche), pour rien. Un appel au nom `recall_context` tombera maintenant sur « outil inconnu » (Registry
    .call(), clairement un échec, pas une invite à réessayer) — à réintroduire avec A-1, pas avant."""
    clarify_spec = ToolSpec(name="clarify", description="Pose une seule question fermée quand il manque "
                             "une information qui change l'action.",
                             parameters={"type": "object", "properties": {
                                 "question": {"type": "string"}}, "required": ["question"]},
                             effect=Effect.READ, core=True, source="builtin")
    find_spec, find_fn = _native_stub(FIND_TOOL, "Trouve un outil métier qui n'est pas encore chargé ; "
                                       "son schéma est chargé au tour suivant.",
                                       "query", "ce que l'outil doit faire (mots-clés)")
    esc_spec, esc_fn = _native_stub(ESCALATE_TOOL, "Inscrit la question dans la file de décision humaine.",
                                    "reason", "ce que l'humain doit trancher, exactement")
    return StaticToolSource(entries=[
        (clarify_spec, _clarify), (find_spec, find_fn), (esc_spec, esc_fn),
    ])


@dataclass
class Registry:
    """Implémentation de `core.ports.ToolRegistry`. Agrège N `ToolSource` ; le premier qui déclare un nom
    gagne (collision journalisée par l'appelant s'il y en a — v0 ne détecte pas les doublons)."""

    sources: list[ToolSource] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._cache: list[ToolSpec] | None = None

    def refresh(self) -> None:
        """Invalide le cache (nouveaux outils MCP, ex. reconnexion) — à appeler périodiquement si besoin."""
        self._cache = None
        for src in self.sources:
            refresh = getattr(src, "refresh", None)
            if callable(refresh):
                refresh()

    def turn_done(self, thread_id: str) -> None:
        """Fin de tour : relaie aux sources qui le déclarent (ex. fermeture des onglets brosim)."""
        for src in self.sources:
            hook = getattr(src, "turn_done", None)
            if callable(hook):
                try:
                    hook(thread_id)
                except Exception:  # noqa: BLE001 — une source en panne ne casse jamais la fin d'un tour
                    logger.warning("turn_done de %s en échec", type(src).__name__, exc_info=True)

    def _all(self) -> list[ToolSpec]:
        if self._cache is None:
            specs: list[ToolSpec] = []
            seen: set[str] = set()
            for src in self.sources:
                for spec in src.specs():
                    if spec.name in seen:
                        continue
                    seen.add(spec.name)
                    specs.append(spec)
            self._cache = specs
        return self._cache

    def core(self) -> list[ToolSpec]:
        return [s for s in self._all() if s.core]

    def spec(self, name: str) -> ToolSpec | None:
        return next((s for s in self._all() if s.name == name), None)

    def find(self, query: str, limit: int = 5) -> list[ToolSpec]:
        words = [w for w in query.lower().split() if w]
        scored: list[tuple[int, ToolSpec]] = []
        for spec in self._all():
            if spec.core:
                continue
            haystack = f"{spec.name} {spec.description}".lower()
            score = sum(1 for w in words if w in haystack)
            if score > 0 or not words:
                scored.append((score, spec))
        scored.sort(key=lambda t: -t[0])
        return [s for _, s in scored[:limit]]

    def index(self) -> list[tuple[str, str]]:
        return [(s.name, s.description) for s in self._all() if not s.core]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        for src in self.sources:
            if any(s.name == name for s in src.specs()):
                return src.call(name, arguments, ctx)
        return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)

    def close(self) -> None:
        for src in self.sources:
            close = getattr(src, "close", None)
            if callable(close):
                close()


__all__ = ["Registry", "StaticToolSource", "builtin_core_source"]
