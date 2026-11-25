"""Options de session gardées EN MÉMOIRE par fil (`POST /v1/local/sessions`) : `tool_loop_guard`, `escalate`, `lang_hint`, `deep_reasoning`, `mcp_required`.

Même règle que `system_prompt` et `prefix` : l'hôte les redonne à la reprise, une ouverture sans le champ les efface,
`DELETE /v1/local/sessions/{fil}` aussi, et un sous-agent (`<fil>/sa-…`) hérite de celles de son fil parent."""
from __future__ import annotations

import threading
from typing import Any

_MAX_THREADS = 5000


class ThreadOption:
    """Une valeur par fil ; `None` = pas d'option posée (comportement par défaut)."""

    def __init__(self) -> None:
        self._by_thread: dict[str, Any] = {}
        self._lock = threading.Lock()

    def set(self, thread_id: str, value: Any) -> None:
        with self._lock:
            if value is None:
                self._by_thread.pop(thread_id, None)
                return
            self._by_thread[thread_id] = value
            while len(self._by_thread) > _MAX_THREADS:  # borne mémoire : on oublie les plus anciens fils
                self._by_thread.pop(next(iter(self._by_thread)), None)

    def clear(self, thread_id: str) -> None:
        self.set(thread_id, None)

    def get(self, session_key: str | None) -> Any:
        if not self._by_thread:
            return None
        key = session_key or ""
        with self._lock:
            while key not in self._by_thread and "/sa-" in key:
                key = key.rsplit("/sa-", 1)[0]
            return self._by_thread.get(key)


_ESCALATE = ThreadOption()
_TOOL_LOOP_GUARD = ThreadOption()
_LANG_HINT = ThreadOption()
_DEEP_REASONING = ThreadOption()
_MCP_REQUIRED = ThreadOption()

LANG_HINT_CODE = "bad_lang_hint"
LANG_HINT_ERROR = "lang_hint : booléen attendu (false = pas de ligne de langue dans l'en-tête du tour)"

DEEP_REASONING_CODE = "bad_deep_reasoning"
DEEP_REASONING_ERROR = ("deep_reasoning : booléen attendu (false = la montée vers le palier deep n'impose pas la réflexion, le "
                        "raisonnement suit alors celui du palier Ship)")

ESCALATE_CODE = "bad_escalate"
ESCALATE_ERROR = "escalate : booléen attendu (false = pas d'escalade automatique vers le palier deep)"


MCP_REQUIRED_CODE = "bad_mcp_required"
MCP_REQUIRED_ERROR = "mcp_required : liste de noms de serveurs MCP (chaînes non vides, au plus 64) attendue"
MCP_REQUIRED_MAX = 64
_MCP_REQUIRED_RETRY_S = 2.0
_MCP_REQUIRED_CACHE_S = 15.0


def mcp_required_registry() -> ThreadOption:
    """Valeur posée = tuple de noms de serveurs MCP INDISPENSABLES au fil ; rien de posé = comportement d'avant."""
    return _MCP_REQUIRED


def validate_mcp_required(raw: object) -> tuple[tuple[str, ...] | None, str | None]:
    """(noms, erreur) : absent/`null`/liste vide = None (défaut) ; sinon une liste de chaînes non vides (sans doublon, ordre
    gardé). Toute autre forme : erreur (400 `bad_mcp_required`). Les noms ne sont comparés aux serveurs déclarés qu'à l'appelant."""
    if raw is None:
        return None, None
    if not isinstance(raw, list) or len(raw) > MCP_REQUIRED_MAX:
        return None, MCP_REQUIRED_ERROR
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip() or len(item) > 128:
            return None, MCP_REQUIRED_ERROR
        if item not in names:
            names.append(item)
    return (tuple(names) or None), None


def mcp_required(session_key: str | None) -> tuple[str, ...]:
    return _MCP_REQUIRED.get(session_key) or ()


def parse_mcp_required_retry(value: object) -> float:
    """`local.mcp_required_retry_s` : durée (secondes) de l'unique nouvelle tentative avant de déclarer un serveur requis
    injoignable. Défaut 2 ; 0 = pas de nouvel essai ; borné à [0, 10]. ValueError sinon."""
    if value is None:
        return 2.0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 10:
        raise ValueError(f"local.mcp_required_retry_s invalide : {value!r} (0 = pas de nouvel essai, sinon jusqu'à 10 secondes)")
    return float(value)


def configure_mcp_required_retry(seconds: float) -> None:
    global _MCP_REQUIRED_RETRY_S
    _MCP_REQUIRED_RETRY_S = seconds


def parse_mcp_required_cache(value: object) -> float:
    """`local.mcp_required_cache_s` : durée (secondes) pendant laquelle un sondage RÉUSSI d'un serveur requis est gardé (jamais un
    échec). Défaut 15 ; 0 = pas de cache ; borné à [0, 60]. ValueError sinon."""
    if value is None:
        return 15.0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 60:
        raise ValueError(f"local.mcp_required_cache_s invalide : {value!r} (0 = pas de cache, sinon jusqu'à 60 secondes)")
    return float(value)


def configure_mcp_required_cache(seconds: float) -> None:
    global _MCP_REQUIRED_CACHE_S
    _MCP_REQUIRED_CACHE_S = seconds


def mcp_required_cache_s() -> float:
    return _MCP_REQUIRED_CACHE_S


def mcp_required_retry_s() -> float:
    return _MCP_REQUIRED_RETRY_S


def escalate_registry() -> ThreadOption:
    """Valeur posée = `False` (escalade éteinte pour ce fil) ; rien de posé = comportement d'avant."""
    return _ESCALATE


def tool_loop_guard_registry() -> ThreadOption:
    """Valeur posée = `False` (éteint) ou `(note_after, suspend_after)` ; rien de posé = réglage global."""
    return _TOOL_LOOP_GUARD


def validate_escalate(raw: object) -> tuple[bool | None, str | None]:
    """(valeur, erreur) : absent/`null` = None (défaut) ; un booléen STRICT sinon (0, 1, "false" : erreur)."""
    if raw is None:
        return None, None
    if isinstance(raw, bool):
        return raw, None
    return None, ESCALATE_ERROR


def deep_reasoning_registry() -> ThreadOption:
    """Valeur posée = `False` (la montée vers deep n'impose pas la réflexion) ; rien de posé = comportement d'avant."""
    return _DEEP_REASONING


def validate_deep_reasoning(raw: object) -> tuple[bool | None, str | None]:
    """(valeur, erreur) : absent/`null` = None (défaut : la réflexion demandée par la politique) ; un booléen STRICT sinon."""
    if raw is None:
        return None, None
    if isinstance(raw, bool):
        return raw, None
    return None, DEEP_REASONING_ERROR


def deep_reasoning_forced(session_key: str | None) -> bool:
    return _DEEP_REASONING.get(session_key) is not False


def lang_hint_registry() -> ThreadOption:
    """Valeur posée = `False` (pas de ligne de langue dans l'en-tête du tour) ; rien de posé = ligne présente."""
    return _LANG_HINT


def validate_lang_hint(raw: object) -> tuple[bool | None, str | None]:
    """(valeur, erreur) : absent/`null` = None (défaut : ligne présente) ; un booléen STRICT sinon."""
    if raw is None:
        return None, None
    if isinstance(raw, bool):
        return raw, None
    return None, LANG_HINT_ERROR


def lang_hint_enabled(session_key: str | None) -> bool:
    return _LANG_HINT.get(session_key) is not False


def escalation_allowed(session_key: str | None) -> bool:
    return _ESCALATE.get(session_key) is not False


__all__ = ["MCP_REQUIRED_CODE", "MCP_REQUIRED_ERROR", "configure_mcp_required_cache", "configure_mcp_required_retry", "mcp_required_cache_s", "parse_mcp_required_cache", "mcp_required", "mcp_required_registry", "mcp_required_retry_s",
           "parse_mcp_required_retry", "validate_mcp_required", "DEEP_REASONING_CODE", "DEEP_REASONING_ERROR", "ESCALATE_CODE", "ESCALATE_ERROR", "LANG_HINT_CODE", "LANG_HINT_ERROR", "ThreadOption", "deep_reasoning_forced", "deep_reasoning_registry", "escalate_registry",
           "escalation_allowed", "lang_hint_enabled", "lang_hint_registry", "tool_loop_guard_registry", "validate_deep_reasoning", "validate_escalate",
           "validate_lang_hint"]
