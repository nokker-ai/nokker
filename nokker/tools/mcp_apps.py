"""Registre des apps MCP affichées (drapeau `mcp_apps`, docs/MCP-NEXT.md §3-§4).

Une app est enregistrée quand le hub émet `tool.app` : `app_id` → (locataire, fil, serveur, outil). Le client renvoie
les `tools/call` de l'app sur `POST /v1/apps/{app_id}/tools/call` ; le registre dit si l'appel est permis (même
locataire, membre du fil, outil du MÊME serveur dont la `visibility` contient « app ») et trouve la source qui
l'exécute. Les portes et cartes restent celles du noyau (`Kernel.app_tool_call`).

Mémoire du processus seulement (comme `api/events.py::EventBus`) : bornée en nombre et en âge ; après un redémarrage
une app affichée n'appelle plus rien (404), le client le dit.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import ToolResult

# Canal d'un appel d'outil venu d'une app (portes du noyau ; une carte décidée ne relance ni tour ni note au modèle).
APP_CHANNEL = "mcp-app"
logger = logging.getLogger("nokker.tools.mcp_apps")


@dataclass
class AppRecord:
    app_id: str
    tenant: str | None
    thread_id: str
    server: str
    tool: str
    call_id: str
    at: float


@dataclass
class _Server:
    source: Any  # source enveloppée (effets, caviardage SECRET) : c'est elle qui exécute
    raw: Any  # `McpToolSource` : `app_tools()`, `app_specs()`
    annotate: Callable[[ToolSpec], ToolSpec]


@dataclass
class AppRegistry:
    max_apps: int = 1000
    max_age_s: float = 24 * 3600.0
    clock: Callable[[], float] = time.time
    _apps: OrderedDict[str, AppRecord] = field(default_factory=OrderedDict)
    _servers: dict[str, _Server] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add_server(self, name: str, source: Any, raw: Any, annotate: Callable[[ToolSpec], ToolSpec] | None = None
                   ) -> None:
        self._servers[name] = _Server(source=source, raw=raw, annotate=annotate or (lambda s: s))

    def register(self, *, tenant: str | None, thread_id: str, server: str, tool: str, call_id: str) -> AppRecord:
        rec = AppRecord(app_id="app_" + secrets.token_urlsafe(12), tenant=tenant, thread_id=thread_id, server=server,
                        tool=tool, call_id=call_id, at=self.clock())
        with self._lock:
            self._apps[rec.app_id] = rec
            self._trim_locked()
        return rec

    def _trim_locked(self) -> None:
        cutoff = self.clock() - self.max_age_s
        while self._apps and (len(self._apps) > self.max_apps or next(iter(self._apps.values())).at < cutoff):
            self._apps.popitem(last=False)

    def get(self, app_id: str) -> AppRecord | None:
        with self._lock:
            self._trim_locked()
            return self._apps.get(app_id)

    def allowed(self, rec: AppRecord, name: str) -> bool:
        """Spec ext-apps : l'hôte DOIT refuser un `tools/call` d'app vers un outil sans « app » dans sa `visibility`,
        et tout appel vers un AUTRE serveur que celui de l'app."""
        srv = self._servers.get(rec.server)
        if srv is None:
            return False
        try:
            return name in srv.raw.app_tools()
        except Exception:  # noqa: BLE001 — serveur injoignable : rien n'est permis
            return False

    def spec(self, rec_or_server: AppRecord | str, name: str) -> ToolSpec | None:
        server = rec_or_server.server if isinstance(rec_or_server, AppRecord) else rec_or_server
        srv = self._servers.get(server)
        if srv is None:
            return None
        for s in [*srv.source.specs(), *(srv.annotate(x) for x in srv.raw.app_specs())]:
            if s.name == name:
                return s
        return None

    def server_of(self, name: str) -> str | None:
        """Serveur qui sert un outil réservé aux apps (pour l'exécuteur de cartes, qui n'a que le nom)."""
        for server, srv in self._servers.items():
            try:
                if name in srv.raw.app_tools():
                    return server
            except Exception:  # noqa: BLE001 — serveur injoignable : on passe au suivant
                logger.debug("mcp app : outils de %s indisponibles", server, exc_info=True)
        return None

    def call(self, server: str, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        srv = self._servers.get(server)
        if srv is None:
            return ToolResult(call_id="", name=name, content=f"serveur MCP inconnu : {server}", is_error=True)
        return srv.source.call(name, arguments, ctx)


__all__ = ["APP_CHANNEL", "AppRecord", "AppRegistry"]
