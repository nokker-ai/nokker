"""Contexte d'un sous-agent fork (drapeau `delegate_fork`, veille 01.11 §3.1).

Un `delegate` classique démarre l'enfant dans une fenêtre neuve : son fil `<parent>/sa-<id>` n'a ni historique ni
mémoire de session, et son message système (mémoire de session du fil) comme ses schémas (registre filtré) diffèrent de
ceux du parent. Le cache de préfixe du parent ne lui sert presque pas, et le parent doit tout réexpliquer dans le but.

Le fork part du contexte EXACT du dernier appel modèle du parent dans ce tour (`Kernel.fork_base`) : même message
système (préfixe, index des outils, profil, mémoire de projet, mémoire de session), même historique, même tour en cours
jusqu'au dernier résultat d'outil, mêmes schémas d'outils. L'enfant y AJOUTE seulement son propre tour (en-tête du tour
et consigne de fork, puis ses appels) : tout ce que le parent a déjà envoyé est un préfixe, octet pour octet, que le
cache de l'amont sert sans le recalculer (même `session_key` que le parent pour l'affinité Router).

Ce qui ne change pas : le fil de l'enfant, ses portes (le registre filtré de `delegate` refuse à l'appel les outils
interdits même si leurs schémas, hérités du parent, sont visibles), sa mémoire de fil (crochets `before_turn`,
`after_turn`, réservoir : ceux du contexte du parent, appliqués au fil de l'enfant), son résumé rendu au parent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nokker.core.ports import Assembled, Recall, ToolSpec
from nokker.core.turn import Tier, Turn


def _tool_name(t: dict[str, Any]) -> str:
    return str((t.get("function") or {}).get("name") or t.get("name") or "")


def _estimate(messages: list[dict[str, Any]]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages) // 4


@dataclass
class ForkedContext:
    """`ContextEngine` de l'enfant fork : `inner` (le contexte du parent) assemble le tour de l'enfant ; on remplace son
    message système par la base du parent et on garde la suite. Tout autre attribut est celui de `inner`."""

    inner: Any
    base_messages: list[dict[str, Any]]
    base_tools: list[dict[str, Any]]
    session_key: str = ""
    _base_names: frozenset = field(default=frozenset(), init=False, repr=False)

    def __post_init__(self) -> None:
        self._base_names = frozenset(_tool_name(t) for t in self.base_tools)

    def __getattr__(self, name: str) -> Any:  # crochets optionnels du contexte (history_window, before_turn…)
        if name.startswith("__") or name in ("inner", "base_messages", "base_tools", "session_key", "_base_names"):
            raise AttributeError(name)
        return getattr(self.inner, name)

    def assemble(self, turn: Turn, history: list[Turn], recall: list[Recall], loaded_tools: list[ToolSpec],
                 tier: Tier) -> Assembled:
        own = self.inner.assemble(turn, history, recall, loaded_tools, tier)
        rest = own.messages[1:] if own.messages and own.messages[0].get("role") == "system" else own.messages
        # Schémas : ceux du parent d'abord (même ordre, même texte), puis ceux que l'enfant charge en plus.
        tools = list(self.base_tools) + [t for t in own.tools if _tool_name(t) not in self._base_names]
        layers = dict(own.layer_tokens)
        layers["fork_base"] = _estimate(self.base_messages)
        return Assembled(messages=[*self.base_messages, *rest], tools=tools, prefix_sha256=own.prefix_sha256,
                         layer_tokens=layers)

    def admit_result(self, result: Any) -> Any:
        return self.inner.admit_result(result)


__all__ = ["ForkedContext"]
