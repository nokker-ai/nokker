"""Source d'outils MCP streamable-HTTP (`tools.ToolSource`), SDK `mcp` (même version que hermes-nokker,
pin `mcp==2.0.0` — voir `pyproject.toml`).

Les ports du noyau sont SYNCHRONES (`core/loop.py` n'est pas async) ; le SDK `mcp` est async. Chaque
`McpToolSource` possède donc son propre thread avec une boucle asyncio persistante et UNE session MCP
tenue ouverte entre les appels (pas de poignée de main à chaque outil) ; `_SyncMcp` porte ce mécanisme et
est réutilisé par `nokker/memory/corthexis.py` (même pattern de connexion, serveur différent).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from nokker.tools.schema import compact_schema
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult

logger = logging.getLogger("nokker.tools.mcp")


class _SyncMcp:
    """Boucle asyncio persistante dans un thread dédié + une session MCP streamable-HTTP tenue ouverte.

    `call(coro_factory)` exécute une coroutine sur cette boucle et bloque jusqu'au résultat — c'est ce qui
    permet à un port synchrone (`ToolSource`, `MemoryPort`) de parler à un serveur MCP async.
    """

    def __init__(self, url: str, headers: dict[str, str] | Callable[[], dict[str, str]] | None = None,
                 timeout_s: float = 30.0) -> None:
        self.url = url
        # Un appelable est lu à chaque ouverture de session : en-têtes tirés du coffre au moment de l'usage (X4,
        # références `secret:NOM`), jamais figés au démarrage.
        self.headers = headers or {}
        self.timeout_s = timeout_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: ClientSession | None = None
        self._transport_cm = None
        self._session_cm = None
        self._lock = threading.Lock()  # création de la boucle (entre threads ; jamais tenu à travers un await)
        # Création de la session, SUR la boucle : un `asyncio.Lock`, parce qu'une coroutine qui attend doit RENDRE la
        # boucle. Un `threading.Lock` tenu à travers les `await` de la poignée de main bloquait le thread de la boucle
        # dès qu'une 2e coroutine arrivait sur une session froide (rappel du tour dans le pool + memory_search du
        # modèle) : interblocage définitif jusqu'au redémarrage, reproduit en revue le 20.10.2026.
        self._alock: asyncio.Lock | None = None

    def _ensure_loop(self) -> None:
        if self._loop is not None:
            return
        with self._lock:  # deux premiers appelants : une seule boucle, un seul thread
            if self._loop is not None:
                return
            ready = threading.Event()

            def runner() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                ready.set()
                loop.run_forever()

            self._thread = threading.Thread(target=runner, name=f"mcp-io:{self.url}", daemon=True)
            self._thread.start()
            ready.wait()

    def run(self, coro):
        """Exécute `coro` sur la boucle dédiée et attend le résultat (appel synchrone). En retard : la coroutine est
        annulée, elle ne garde ni la boucle ni la session occupées."""
        self._ensure_loop()
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(self.timeout_s)
        except FutureTimeoutError:
            future.cancel()
            raise

    async def session_async(self) -> ClientSession:
        if self._session is not None:
            return self._session
        if self._alock is None:  # créé sur la boucle ; aucun await entre le test et l'affectation
            self._alock = asyncio.Lock()
        async with self._alock:
            if self._session is not None:  # relu après acquisition — une autre coroutine a pu la créer
                return self._session
            headers = self.headers() if callable(self.headers) else self.headers
            http_client = httpx2.AsyncClient(headers=headers, timeout=self.timeout_s)
            self._transport_cm = streamable_http_client(self.url, http_client=http_client)
            read, write = await self._transport_cm.__aenter__()
            self._session_cm = ClientSession(read, write)
            session = await self._session_cm.__aenter__()
            await session.initialize()
            self._session = session
            return session

    def session(self) -> ClientSession:
        return self.run(self.session_async())

    def close(self) -> None:
        if self._loop is None:
            return

        async def _close() -> None:
            if self._session_cm is not None:
                await self._session_cm.__aexit__(None, None, None)
            if self._transport_cm is not None:
                await self._transport_cm.__aexit__(None, None, None)

        try:
            self.run(_close())
        except Exception:
            logger.warning("mcp(%s): erreur à la fermeture", self.url, exc_info=True)
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5.0)


def text_of(result: Any) -> str:
    """Concatène les blocs texte d'un `CallToolResult` (images/audio/liens ignorés en v0)."""
    parts = []
    for block in getattr(result, "content", None) or []:
        if isinstance(block, TextContent):
            parts.append(block.text)
        else:
            parts.append(f"[{type(block).__name__}]")
    return "\n".join(parts) if parts else ""


@dataclass
class McpToolSource:
    """Un serveur MCP streamable-HTTP = une source d'outils. `core_tools` = noms présents au préfixe
    (≤ 8 au total sur l'ensemble des sources, PLAN §2.4) ; `effects` donne l'effet déclaré par outil — le
    serveur MCP ne porte pas cette information, donc c'est nous qui la déclarons ici (défaut WRITE,
    prudent, cf. `ToolSpec.effect`)."""

    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    core_tools: frozenset[str] = frozenset()
    effects: dict[str, Effect] = field(default_factory=dict)
    timeout_s: float = 30.0
    # Outil appelé en fin de tour si le tour a utilisé ce serveur (ex. `browser_close` pour brosim : un onglet
    # gardé ouvert bloque le suivant sur un site hostile — 2 onglets max par compte ; Nick 20.10). None = rien.
    close_on_turn_end: str | None = None
    # En-têtes résolus à l'ouverture de chaque session (références `secret:NOM`, nokker/secrets/refs.py) ; prime sur
    # `headers` quand il est posé.
    header_source: Callable[[], dict[str, str]] | None = None

    def __post_init__(self) -> None:
        self._io = _SyncMcp(self.url, self.header_source or self.headers, self.timeout_s)
        self._cache: list[ToolSpec] | None = None
        self._used_by: set[str] = set()
        self._used_lock = threading.Lock()

    def specs(self) -> list[ToolSpec]:
        if self._cache is not None:
            return self._cache

        async def _list() -> list[ToolSpec]:
            session = await self._io.session_async()
            result = await session.list_tools()
            out = []
            for t in result.tools:
                desc = (t.description or "").strip().splitlines()[0] if t.description else ""
                out.append(ToolSpec(
                    name=t.name, description=desc[:240],
                    parameters=compact_schema(t.input_schema or {"type": "object", "properties": {}}),
                    effect=self.effects.get(t.name, Effect.WRITE),
                    core=t.name in self.core_tools, source=f"mcp:{self.name}",
                ))
            return out

        self._cache = self._io.run(_list())
        return self._cache

    def refresh(self) -> None:
        self._cache = None

    def turn_done(self, thread_id: str) -> None:
        """Fin de tour : appelle `close_on_turn_end` si CE fil a utilisé le serveur. La session MCP est partagée
        par tous les fils : on ne ferme que si aucun autre fil ne l'utilise en même temps (sinon le dernier qui
        termine fermera)."""
        if not self.close_on_turn_end:
            return
        with self._used_lock:
            if thread_id not in self._used_by:
                return
            self._used_by.discard(thread_id)
            if self._used_by:
                return

        async def _close():
            session = await self._io.session_async()
            return await session.call_tool(self.close_on_turn_end, {})

        try:
            self._io.run(_close())
        except Exception:
            logger.warning("mcp(%s): %s en fin de tour en échec", self.name, self.close_on_turn_end, exc_info=True)

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if self.close_on_turn_end and name != self.close_on_turn_end:
            with self._used_lock:
                self._used_by.add(ctx.thread_id)

        async def _call():
            session = await self._io.session_async()
            return await session.call_tool(name, arguments)

        try:
            result = self._io.run(_call())
        except Exception as e:
            logger.warning("mcp(%s): outil %s en échec", self.name, name, exc_info=True)
            return ToolResult(call_id="", name=name, content=f"erreur MCP {self.name}/{name}: {e}", is_error=True)
        return ToolResult(call_id="", name=name, content=text_of(result),
                           is_error=bool(getattr(result, "is_error", False)))

    def close(self) -> None:
        self._io.close()


__all__ = ["McpToolSource", "_SyncMcp", "text_of"]
