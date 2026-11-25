"""Prompt système de l'HÔTE, par session (`system_prompt` de `POST /v1/local/sessions`, ou message `role: system` de
`POST /v1/chat/completions`, docs/HOST-EMBED.md §12).

Il entre dans le préfixe STABLE du fil, juste APRÈS le préfixe du noyau (donc après ses invariants) et avant l'index des
outils : une instruction de l'hôte ne passe jamais devant eux, et les portes (cartes d'approbation, refus, filets de
secrets) sont dans le code : aucun texte ne les retire. Le texte est celui de l'hôte, envoyé tel quel, octet pour octet.

Gardé EN MÉMOIRE pour le fil (comme `deny_tools`, `memory`, `upstream_headers`) : un redémarrage du noyau l'oublie, l'hôte le
redonne à la reprise. Une ouverture sans le champ l'efface ; `DELETE /v1/local/sessions/{fil}` aussi. Jamais écrit au
journal, au ledger, dans un événement ni sur disque. Vide (défaut) : le préfixe est celui d'avant, à l'octet près."""
from __future__ import annotations

import hashlib
import logging
import threading

logger = logging.getLogger("nokker.context.host_prompt")

DEFAULT_MAX_BYTES = 128 * 1024  # `local.system_prompt_max_bytes` ; ne compte QUE le texte (les outils de l'hôte passent par MCP)
CEILING_BYTES = 512 * 1024  # borne du réglage
ERROR_TYPE = "system_prompt : chaîne attendue"
CODE = "bad_system_prompt"


def max_bytes(configured: object) -> int:
    """Plafond effectif : le réglage de l'hôte (entier > 0), borné à `CEILING_BYTES` ; absent ou illisible : le défaut."""
    if isinstance(configured, bool) or not isinstance(configured, int) or configured <= 0:
        return DEFAULT_MAX_BYTES
    return min(configured, CEILING_BYTES)


def validate(raw: object, limit: int = DEFAULT_MAX_BYTES) -> tuple[str, str | None]:
    """(texte, erreur). `null` ou chaîne vide : aucun prompt, pas d'erreur. Jamais de troncature : au-delà de `limit`
    octets (UTF-8), refus qui dit la taille reçue et la limite. Le message d'erreur ne reprend jamais le texte."""
    if raw is None:
        return "", None
    if not isinstance(raw, str):
        return "", ERROR_TYPE
    size = len(raw.encode("utf-8", "surrogatepass"))
    if size > limit:
        return "", f"system_prompt : {size} octets reçus, {limit} au plus (UTF-8)"
    return raw, None


class HostPrompts:
    def __init__(self) -> None:
        self._by_thread: dict[str, str] = {}
        self._lock = threading.Lock()

    def set(self, thread_id: str, text: str) -> None:
        """Pose (remplace) le prompt d'un fil ; vide = aucun."""
        with self._lock:
            if text:
                self._by_thread[thread_id] = text
                while len(self._by_thread) > 5000:  # borne mémoire : on oublie les plus anciens fils
                    self._by_thread.pop(next(iter(self._by_thread)), None)
            else:
                self._by_thread.pop(thread_id, None)

    def clear(self, thread_id: str) -> None:
        self.set(thread_id, "")

    def exact(self, thread_id: str) -> str:
        """Prompt posé sur CE fil (sans repli sur le fil parent)."""
        with self._lock:
            return self._by_thread.get(thread_id, "")

    def get(self, session_key: str | None) -> str:
        """Prompt du fil désigné ; un sous-agent `<fil>/sa-…` a celui de son fil."""
        if not self._by_thread:
            return ""
        key = session_key or ""
        with self._lock:
            while key not in self._by_thread and "/sa-" in key:
                key = key.rsplit("/sa-", 1)[0]
            return self._by_thread.get(key, "")

    def adopt(self, thread_id: str, text: str) -> bool:
        """Prompt venu d'un `role: system` de chat/completions : posé si aucun, remplacé s'il change (le préfixe change :
        un avertissement, sans le texte). Texte vide : rien ne change (le prompt de la session reste). Vrai si changé."""
        if not text:
            return False
        before = self.exact(thread_id)
        if before == text:
            return False
        self.set(thread_id, text)
        if before:
            logger.warning("prompt système de l'hôte changé sur le fil %s (%d -> %d caractères) : le préfixe change, "
                           "le cache de préfixe repart", thread_id, len(before), len(text))
        return True

    def __repr__(self) -> str:  # jamais le texte dans un journal
        return f"HostPrompts(threads={len(self._by_thread)})"


_REGISTRY = HostPrompts()


def registry() -> HostPrompts:
    return _REGISTRY


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


__all__ = ["CEILING_BYTES", "CODE", "DEFAULT_MAX_BYTES", "ERROR_TYPE", "HostPrompts", "fingerprint", "max_bytes", "registry",
           "validate"]
