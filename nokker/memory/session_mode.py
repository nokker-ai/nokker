"""Mémoire du noyau coupée PAR SESSION (`memory: "off" | "host"` à l'ouverture, docs/HOST-EMBED.md §9).

`off` : aucune mémoire du noyau pour ce fil. `host` : la mémoire est tenue par l'hôte. Dans les deux cas le noyau n'ajoute ni
ne lit rien de lui-même pour ce fil : pas de rappel automatique en tête de tour, pas de note apprise dans la fenêtre
(les consignes du dépôt, AGENTS.md et équivalents, restent : ce sont des instructions), et les outils de mémoire sont retirés du catalogue du
fil (mécanisme `deny_tools`, donc le préfixe suit le catalogue sans qu'aucune ligne du préfixe change). La différence est
dans l'intention seule : `host` dit que l'hôte fournit sa propre mémoire (par ses outils MCP) ; `off` qu'il n'y en a pas.
Mode en mémoire du noyau, comme `deny_tools` : l'hôte le redonne à la reprise. Vide (défaut) : comportement d'avant."""
from __future__ import annotations

import threading

MODES = ("off", "host")
# Outils de mémoire du noyau : la mémoire longue (`memory_search`, `memory_get`, `memory_write`), les notes (`memory_note`) et
# la mémoire de projet (`memory`).
TOOLS = ("memory_search", "memory_get", "memory_write", "memory_note", "memory")


class SessionMemory:
    def __init__(self) -> None:
        self._by_thread: dict[str, str] = {}
        self._lock = threading.Lock()

    def set(self, thread_id: str, mode: str | None) -> None:
        """Pose (remplace) le mode d'un fil ; None = comportement d'avant."""
        with self._lock:
            if mode:
                self._by_thread[thread_id] = mode
                while len(self._by_thread) > 5000:  # borne mémoire : on oublie les plus anciens fils
                    self._by_thread.pop(next(iter(self._by_thread)), None)
            else:
                self._by_thread.pop(thread_id, None)

    def mode(self, thread_id: str | None) -> str | None:
        if not self._by_thread:
            return None
        key = thread_id or ""
        with self._lock:
            while key not in self._by_thread and "/sa-" in key:
                key = key.rsplit("/sa-", 1)[0]
            return self._by_thread.get(key)

    def kernel_memory_off(self, thread_id: str | None) -> bool:
        """Le noyau ne lit ni n'écrit sa mémoire pour ce fil (ses sous-agents compris)."""
        return self.mode(thread_id) is not None


_REGISTRY = SessionMemory()


def registry() -> SessionMemory:
    return _REGISTRY


def validate(raw: object) -> str | None:
    """Message d'erreur si `memory` n'est pas `"off"` ou `"host"` ; None si valide."""
    if isinstance(raw, str) and raw in MODES:
        return None
    return f"memory : {' ou '.join(repr(m) for m in MODES)} attendu"


__all__ = ["MODES", "TOOLS", "SessionMemory", "registry", "validate"]
