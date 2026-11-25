"""En-têtes MCP PAR SESSION (`mcp_headers` de l'ouverture d'une session, drapeau `local.mcp_session_headers`).

Un serveur MCP HTTP du noyau a des en-têtes fixes par instance. Un hôte qui veut une identité par session (jeton propre
au fil) les dépose ici à l'ouverture : `{serveur: {en-tête: valeur}}`. À l'appel d'un outil de ce serveur, depuis ce fil
(ou un sous-agent `<fil>/sa-…`), `McpToolSource` ouvre une connexion à part portant les en-têtes fixes PUIS ceux du fil.
Les valeurs ne sont ni journalisées, ni rendues par une route, ni montrées au modèle ; vides (défaut) : rien ne change."""
from __future__ import annotations

import re
import threading

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
MAX_SERVERS, MAX_HEADERS, MAX_VALUE = 16, 16, 4096
# En-têtes que l'hôte ne peut pas poser : ceux du transport.
_FORBIDDEN = frozenset({"host", "content-length", "content-type", "transfer-encoding", "connection", "upgrade",
                        "accept", "mcp-session-id", "mcp-protocol-version"})


def validate(raw: object) -> tuple[dict[str, dict[str, str]], str | None]:
    """(en-têtes par serveur, erreur) d'après le champ `mcp_headers` d'une ouverture de session."""
    if not isinstance(raw, dict) or len(raw) > MAX_SERVERS:
        return {}, f"mcp_headers : objet {{serveur: {{en-tête: valeur}}}} (au plus {MAX_SERVERS} serveurs)"
    out: dict[str, dict[str, str]] = {}
    for server, headers in raw.items():
        if not isinstance(server, str) or not server or not isinstance(headers, dict) or len(headers) > MAX_HEADERS:
            return {}, "mcp_headers : chaque serveur porte un objet d'en-têtes (16 au plus)"
        clean: dict[str, str] = {}
        for k, v in headers.items():
            if not isinstance(k, str) or not _NAME.fullmatch(k) or k.lower() in _FORBIDDEN:
                return {}, f"mcp_headers : nom d'en-tête refusé ({str(k)[:40]!r})"
            if not isinstance(v, str) or len(v) > MAX_VALUE or any(c in v for c in "\r\n\0"):
                return {}, f"mcp_headers : valeur refusée pour {k}"
            clean[k] = v
        out[server] = clean
    return out, None


class SessionHeaders:
    def __init__(self) -> None:
        self._by_thread: dict[str, dict[str, dict[str, str]]] = {}
        self._lock = threading.Lock()

    def set(self, thread_id: str, headers: dict[str, dict[str, str]]) -> None:
        """Pose (remplace) les en-têtes d'un fil ; vide = aucun."""
        with self._lock:
            if headers:
                self._by_thread[thread_id] = {s: dict(h) for s, h in headers.items()}
                while len(self._by_thread) > 5000:  # borne mémoire : on oublie les plus anciens fils
                    self._by_thread.pop(next(iter(self._by_thread)), None)
            else:
                self._by_thread.pop(thread_id, None)

    def get(self, thread_id: str | None, server: str) -> dict[str, str] | None:
        if not self._by_thread:
            return None
        tid = thread_id or ""
        with self._lock:
            while tid not in self._by_thread and "/sa-" in tid:
                tid = tid.rsplit("/sa-", 1)[0]
            h = self._by_thread.get(tid, {}).get(server)
            return dict(h) if h else None


_REGISTRY = SessionHeaders()


def registry() -> SessionHeaders:
    return _REGISTRY


__all__ = ["SessionHeaders", "registry", "validate"]
