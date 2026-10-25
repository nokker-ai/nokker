"""Client du pool de navigateurs de la bêta (contrat : docs/BROWSER-POOL.md, fournisseur : le service du pool).

UNE source d'outils pour tous les testeurs : chaque appel lit le locataire du tour (`current_tenant()`), ouvre ou
reprend la session navigateur de CE locataire (`POST /sessions {tenant_id}` : 201 nouvelle, 200 la même si elle vit
encore), puis passe l'appel MCP à `mcp_url` avec le jeton de la session. Un testeur ne voit jamais le Chrome d'un
autre : la session est rangée par locataire, et le gestionnaire ne sert une session qu'avec son jeton.

- Schémas : ceux du canari (`browser_pool_tools.json`, capturés le 21.10), sans `browser_lane`/`browser_profile`
  (retirés par le contrat) ni `browser_file_upload` (aucun fichier du testeur dans le pod). Pas d'appel au pool pour
  lister les outils : ils existent avant toute session.
- Session expirée ou fermée (404, JSON-RPC -32001) : rouverte une fois, l'appel rejoué. Plafond atteint (429) : le
  testeur l'apprend en une phrase (« occupé, réessaie dans N s »), jamais une trace. Démarrage raté (503) : idem.
- Fin de tour : `browser_close` si le tour a utilisé le navigateur (onglets rendus, Chrome et profil gardés ; même
  règle que le canari) ; l'inactivité (5 min) ferme ensuite la session côté pool.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.tenancy.context import INTERNAL_TENANT, current_tenant
from nokker.tools.effects import ARGUMENT_EFFECTS, DEFAULT_EFFECTS

logger = logging.getLogger("nokker.tools.browser_pool")
SCHEMAS = Path(__file__).parent / "browser_pool_tools.json"
_EXPIRED = (-32001, -32003)  # session expirée ou fermée ; pod injoignable


@dataclass
class _Session:
    id: str
    mcp_url: str
    token: str
    expires_at: float
    io: Any = None


@dataclass
class BrowserPoolSource:
    base_url: str  # NOKKER_BROWSER_POOL_URL
    api_token: Any  # () -> NOKKER_BROWSER_POOL_TOKEN
    timeout_s: float = 60.0
    client: Any = None  # httpx.Client injectable (tests)
    mcp_factory: Any = None  # (url, headers) -> objet avec call(name, args) -> (texte, is_error) ; tests
    _sessions: dict[str, _Session] = field(default_factory=dict, init=False, repr=False)
    _used: set[tuple[str, str]] = field(default_factory=set, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def specs(self) -> list[ToolSpec]:
        out = []
        for t in json.loads(SCHEMAS.read_text(encoding="utf-8")):
            effect = DEFAULT_EFFECTS.get(t["name"]) or Effect(t.get("effect") or "write")
            out.append(ToolSpec(name=t["name"], description=(t.get("description") or "")[:240],
                                parameters=t["parameters"], effect=effect, source="mcp:browser-pool",
                                effect_of=ARGUMENT_EFFECTS.get(t["name"])))
        return out

    # ── sessions ──
    def _http(self) -> Any:
        return self.client or httpx.Client(timeout=self.timeout_s)

    def _open(self, tenant: str) -> _Session | tuple[int, str]:
        r = self._http().post(self.base_url.rstrip("/") + "/sessions", json={"tenant_id": tenant},
                              headers={"Authorization": f"Bearer {self.api_token()}"})
        if r.status_code in (200, 201):
            d = r.json()
            return _Session(id=d["session_id"], mcp_url=d["mcp_url"], token=d["token"],
                            expires_at=_ts(d.get("expires_at")))
        retry = r.headers.get("Retry-After") or (r.json() if "json" in r.headers.get("content-type", "") else {}).get(
            "retry_after_s", 30)
        return r.status_code, str(retry)

    def _session_for(self, tenant: str, fresh: bool = False) -> _Session | tuple[int, str]:
        with self._lock:
            s = self._sessions.get(tenant)
            if s is not None and not fresh and time.time() < s.expires_at - 5:
                return s
            self._sessions.pop(tenant, None)
        opened = self._open(tenant)
        if isinstance(opened, _Session):
            with self._lock:
                self._sessions[tenant] = opened
        return opened

    def _mcp(self, s: _Session) -> Any:
        if s.io is None:
            headers = {"Authorization": f"Bearer {s.token}"}
            s.io = (self.mcp_factory or _McpCaller)(s.mcp_url, headers)
        return s.io

    # ── appels ──
    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tenant = current_tenant()
        if not tenant or tenant == INTERNAL_TENANT:
            return ToolResult(call_id="", name=name, is_error=True, content="navigateur réservé aux comptes de la bêta")
        args = {k: v for k, v in arguments.items() if k != "lane"}  # sans effet dans le pool (contrat)
        for attempt in (0, 1):
            s = self._session_for(tenant, fresh=attempt == 1)
            if not isinstance(s, _Session):
                status, retry = s
                why = (f"le navigateur est occupé (trop de testeurs en même temps) : réessaie dans {retry} s"
                       if status == 429 else "le navigateur n'a pas pu démarrer : réessaie dans un instant")
                return ToolResult(call_id="", name=name, is_error=True, content=why)
            text, is_error, code = self._mcp(s).call(name, args)
            if code in _EXPIRED and attempt == 0:
                continue  # session expirée : rouverte une fois, appel rejoué
            with self._lock:
                self._used.add((tenant, ctx.thread_id))
            return ToolResult(call_id="", name=name, content=text, is_error=is_error)
        return ToolResult(call_id="", name=name, is_error=True, content="session navigateur perdue : réessaie")

    def turn_done(self, thread_id: str) -> None:
        tenant = current_tenant()
        with self._lock:
            used = (tenant, thread_id) in self._used
            self._used.discard((tenant, thread_id))
            s = self._sessions.get(tenant)
        if used and s is not None:
            try:
                self._mcp(s).call("browser_close", {})
            except Exception:  # noqa: BLE001 — fermeture des onglets au mieux
                logger.warning("pool : browser_close de fin de tour en échec", exc_info=True)


class _McpCaller:
    """Appel MCP streamable-HTTP (SDK `mcp`, même client que `McpToolSource`) ; rend (texte, is_error, code JSON-RPC)."""

    def __init__(self, url: str, headers: dict[str, str]) -> None:
        from nokker.tools.mcp import _SyncMcp

        self._io = _SyncMcp(url, headers, 60.0)

    def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool, int | None]:
        from nokker.tools.mcp import text_of

        async def _call():
            session = await self._io.session_async()
            return await session.call_tool(name, args)

        try:
            result = self._io.run(_call())
        except Exception as e:  # noqa: BLE001 — erreur JSON-RPC du gestionnaire, ou réseau
            code = getattr(getattr(e, "error", None), "code", None)
            if code is None and ("404" in str(e) or "-32001" in str(e)):
                code = -32001
            return f"navigateur : {e}", True, code
        return text_of(result), bool(getattr(result, "is_error", False)), None


def _ts(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        from datetime import datetime

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time() + 300


__all__ = ["BrowserPoolSource"]
