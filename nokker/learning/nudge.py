"""Invitation de fin de tour (docs/LEARNING.md §4), drapeau `learning_nudges` (défaut false : rien ne change).

Critère, simple et vérifiable sur le tour fini :
  - le tour a RÉUSSI : pas d'erreur, pas d'escalade, aucune carte en attente, une réponse non vide ;
  - il a fait au moins `min_tools` appels d'outils (défaut 8) ;
  - il n'a appelé ni `skill_create`, ni `skill_update`, ni `memory_note` (déjà fait : rien à suggérer) ;
  - pas de sous-agent (`<fil>/sa-<id>`) : c'est le fil de l'humain qui apprend.

Alors le noyau ajoute UNE ligne fixe, `NUDGE_LINE`, au contexte du PROCHAIN tour du fil (même voie que « [hors tour] »,
`Kernel.add_owner_note`) : jamais un appel modèle de plus, jamais de texte variable (le cache de préfixe n'en souffre
pas), au plus une fois par fil toutes les `cooldown_turns` (défaut 20)."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

# Bloc fixe vu par le modèle (gel : validé par la session d'entraînement avant activation, docs/LEARNING.md §7).
NUDGE_LINE = ("[apprentissage] Le tour précédent a réussi après de nombreux appels d'outils. Si une méthode "
              "réutilisable en ressort, propose-la avec skill_create (ou une note avec memory_note) ; sinon ignore "
              "cette ligne.")
DEFAULT_MIN_TOOLS = 8
DEFAULT_COOLDOWN = 20
_ALREADY = frozenset({"skill_create", "skill_update", "memory_note"})


def tool_calls(turn: Any) -> list[Any]:
    return [c for m in getattr(turn, "messages", []) or [] for c in (getattr(m, "tool_calls", None) or [])]


def should_nudge(turn: Any, min_tools: int = DEFAULT_MIN_TOOLS) -> bool:
    if getattr(turn, "error", None) or getattr(turn, "escalated", False) or getattr(turn, "pending_approvals", None):
        return False
    if not str(getattr(turn, "reply", "") or "").strip() or "/sa-" in str(getattr(turn, "thread_id", "")):
        return False
    calls = tool_calls(turn)
    return len(calls) >= max(1, int(min_tools)) and not any(c.name in _ALREADY for c in calls)


@dataclass
class Nudger:
    """`after_turn(turn)` → `NUDGE_LINE` ou None ; appelé par la boucle à la fin du tour (`Kernel.learning`)."""

    min_tools: int = DEFAULT_MIN_TOOLS
    cooldown_turns: int = DEFAULT_COOLDOWN
    # nokker.ai : seulement les tours d'un compte qui a les outils (`us_…`) ; None = tous les tours (Lokkal).
    sender_prefix: str | None = None
    _last: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def after_turn(self, turn: Any) -> str | None:
        if self.sender_prefix is not None and not str(getattr(turn, "sender", "") or "").startswith(self.sender_prefix):
            return None
        if not should_nudge(turn, self.min_tools):
            return None
        tid, idx = str(turn.thread_id), int(getattr(turn, "index", 0))
        with self._lock:
            last = self._last.get(tid)
            if last is not None and idx - last < self.cooldown_turns:
                return None
            self._last[tid] = idx
        return NUDGE_LINE


__all__ = ["DEFAULT_COOLDOWN", "DEFAULT_MIN_TOOLS", "NUDGE_LINE", "Nudger", "should_nudge"]
