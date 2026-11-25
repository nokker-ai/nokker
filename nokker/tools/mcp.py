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
import time
import weakref
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import EmbeddedResource, TextContent, ToolListChangedNotification

from nokker.tools import mcp_next
from nokker.tools.schema import DESCRIPTION_MAX_CHARS, clip_description, compact_schema
from nokker import bg_stop
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult

logger = logging.getLogger("nokker.tools.mcp")


class _SyncMcp:
    """Boucle asyncio persistante dans un thread dédié + une session MCP streamable-HTTP tenue ouverte.

    `call(coro_factory)` exécute une coroutine sur cette boucle et bloque jusqu'au résultat — c'est ce qui
    permet à un port synchrone (`ToolSource`, `MemoryPort`) de parler à un serveur MCP async.
    """

    def __init__(self, url: str, headers: dict[str, str] | Callable[[], dict[str, str]] | None = None,
                 timeout_s: float = 30.0, guard: Callable[[str], Any] | None = None, *, modern: bool = False,
                 apps: bool = False, task_wait_s: float | None = None,
                 on_list_changed: Callable[[], None] | None = None) -> None:
        self.url = url
        # Garde SSRF (serveurs ajoutés par l'utilisateur, mode strict : `nokker/previews/ssrf.py`) : appelée à CHAQUE
        # ouverture de session, elle rend l'adresse vérifiée à laquelle la session se connecte (épinglée, sans
        # redirection). None : client historique, inchangé.
        self.guard = guard
        # Drapeaux `mcp_modern` / `mcp_apps` (docs/MCP-NEXT.md) : tous deux faux = chemin d'avant, inchangé
        # (`ClientSession` + `initialize`). L'un des deux posé : `mcp.client.Client` (négociation, MRTR, Tasks, apps).
        self.modern = modern
        self.apps = apps
        self.task_wait_s = task_wait_s
        self.on_list_changed = on_list_changed
        self._client = None
        self._closing: asyncio.Event | None = None
        self._holder: asyncio.Task | None = None
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
        self.last_used = time.monotonic()  # dernier usage (expiration des connexions de session inactives)
        self.active = 0  # appels en cours sur cette connexion de session (sous `McpToolSource._used_lock`) : jamais fermée

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
                try:
                    loop.run_forever()
                finally:  # la boucle rend son epoll et sa paire de sockets : sans `close()`, 3 FD par connexion
                    try:
                        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                        for t in pending:
                            t.cancel()
                        if pending:
                            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                        loop.run_until_complete(loop.shutdown_asyncgens())
                    except Exception:
                        logger.debug("mcp: arrêt de la boucle")
                    finally:
                        loop.close()

            self._thread = threading.Thread(target=runner, name=f"mcp-io:{self.url}", daemon=True)
            self._thread.start()
            ready.wait()
            bg_stop.track(self)  # arrêt en test seulement (`bg_stop.stop_all`) : `close()` est déjà idempotent

    def run(self, coro, timeout_s: float | None = None):
        """Exécute `coro` sur la boucle dédiée et attend le résultat (appel synchrone). En retard : la coroutine est
        annulée, elle ne garde ni la boucle ni la session occupées."""
        self._ensure_loop()
        assert self._loop is not None
        self.last_used = time.monotonic()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout_s or self.timeout_s)
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
            if self.guard is not None:
                from nokker.previews.ssrf import pinned_async_client_v2

                http_client = pinned_async_client_v2(self.guard(self.url), headers=headers, timeout_s=self.timeout_s)
            else:
                http_client = httpx2.AsyncClient(headers=headers, timeout=self.timeout_s)
            if self.modern or self.apps:
                self._session = await self._open_next(http_client)
                return self._session
            try:
                self._session = await self._open_legacy(http_client)
            except BaseException:
                self._holder = None
                raise
            return self._session

    async def _open_legacy(self, http_client: Any) -> ClientSession:
        """Transport ET session ouverts dans UNE tâche qui vit autant qu'eux (anyio refuse de quitter une portée
        d'annulation depuis une autre tâche : la fermeture depuis `close()` échouait et laissait les sockets)."""
        opened: asyncio.Future = asyncio.get_running_loop().create_future()
        self._closing = asyncio.Event()

        async def holder() -> None:
            try:
                async with streamable_http_client(self.url, http_client=http_client) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        opened.set_result(session)
                        await self._closing.wait()
            except BaseException as e:  # noqa: BLE001 — remis à l'ouvreur s'il attend encore
                if not opened.done():
                    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
                        e = e.exceptions[0]
                    opened.set_exception(e)
                else:
                    logger.warning("mcp(%s): session fermée sur erreur", self.url, exc_info=True)
            finally:
                await _aclose_http(http_client)

        self._holder = asyncio.get_running_loop().create_task(holder())
        return await opened

    async def _open_next(self, http_client: Any) -> ClientSession:
        """`mcp.client.Client` du SDK : `mode="auto"` (sonde `server/discover`, repli `initialize`) si `modern`, sinon
        `mode="legacy"` ; extensions annoncées selon les drapeaux. Aucun rappel d'élicitation ni d'échantillonnage."""
        from mcp.client import Client, advertise

        from nokker.tools.mcp_next import APP_MIME_TYPE, DEFAULT_TASK_WAIT_S, UI_EXTENSION, TasksExtension

        extensions: list[Any] = []
        if self.modern:
            extensions.append(TasksExtension(wait_s=self.task_wait_s or DEFAULT_TASK_WAIT_S))
        if self.apps:
            extensions.append(advertise(UI_EXTENSION, {"mimeTypes": [APP_MIME_TYPE]}))

        async def on_message(message: Any) -> None:
            root = getattr(message, "root", message)
            if isinstance(root, ToolListChangedNotification) and self.on_list_changed is not None:
                self.on_list_changed()

        client = Client(streamable_http_client(self.url, http_client=http_client),
                        mode="auto" if self.modern else "legacy", extensions=extensions or None,
                        message_handler=on_message, read_timeout_seconds=self.timeout_s, cache=None)
        # Entrée ET sortie du client dans UNE tâche qui vit autant que la session : anyio refuse de quitter une portée
        # d'annulation depuis une autre tâche (chaque `run` en est une).
        opened: asyncio.Future = asyncio.get_running_loop().create_future()
        self._closing = asyncio.Event()

        async def holder() -> None:
            try:
                async with client:
                    opened.set_result(client)
                    await self._closing.wait()
            except BaseException as e:  # noqa: BLE001 — remis à l'ouvreur s'il attend encore
                if not opened.done():
                    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
                        e = e.exceptions[0]  # le message utile, pas « unhandled errors in a TaskGroup »
                    opened.set_exception(e)
                else:
                    logger.warning("mcp(%s): session fermée sur erreur", self.url, exc_info=True)
            finally:
                await _aclose_http(http_client)

        self._holder = asyncio.get_running_loop().create_task(holder())
        self._client = await opened
        return client.session

    async def client_async(self) -> Any:
        """Le `mcp.client.Client` ouvert (drapeaux posés seulement) : `call_tool` y conduit MRTR et Tasks."""
        await self.session_async()
        return self._client

    def session(self) -> ClientSession:
        return self.run(self.session_async())

    def close(self, timeout_s: float = 3.0) -> None:
        """Ferme session, transport, client HTTP, boucle et fil ; idempotent, ne lève pas, borné par `timeout_s`.
        Rouvrable : le prochain `run` recrée boucle et session."""
        with self._lock:
            loop, thread = self._loop, self._thread
            if loop is None:
                return
            holder, closing = self._holder, self._closing
            self._loop = self._thread = self._session = self._client = self._holder = self._closing = None
            self._alock = None
            self._session_cm = self._transport_cm = None

        async def _close() -> None:
            if holder is not None and closing is not None:
                closing.set()
                await holder

        try:
            asyncio.run_coroutine_threadsafe(_close(), loop).result(timeout_s)
        except BaseException:  # noqa: BLE001 — la fermeture ne bloque ni ne lève
            logger.debug("mcp: erreur à la fermeture")
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        if thread is not None:
            thread.join(timeout=timeout_s)


async def _aclose_http(client: Any) -> None:
    aclose = getattr(client, "aclose", None)
    if aclose is not None:
        try:
            await aclose()
        except Exception:  # noqa: BLE001
            logger.debug("mcp: fermeture du client HTTP")


def text_of(result: Any, skip_ui: bool = False, skip_images: bool = False) -> str:
    """Concatène les blocs texte d'un `CallToolResult` (images/audio/liens ignorés en v0). `skip_ui` (drapeau
    `mcp_apps`) : une ressource `ui://` embarquée ne laisse aucune trace ici — elle part au client, pas au modèle.
    `skip_images` : une image ne laisse aucune trace (capture d'un geste du navigateur, lue à part pour la vue en
    direct, docs/BROWSER-LIVE.md) — sinon « [ImageContent] »."""
    parts = []
    for block in getattr(result, "content", None) or []:
        if isinstance(block, TextContent):
            parts.append(block.text)
        elif skip_images and getattr(block, "type", None) == "image":
            continue
        elif skip_ui and isinstance(block, EmbeddedResource) and str(block.resource.uri).startswith("ui://"):
            continue
        else:
            parts.append(f"[{type(block).__name__}]")
    return "\n".join(parts) if parts else ""


_LIVE_SOURCES: weakref.WeakSet = weakref.WeakSet()


def release_thread(thread_id: str) -> int:
    """Fin d'une session : ferme, dans toutes les sources MCP, les connexions que seul ce fil utilisait."""
    return sum(src.release_thread(thread_id) for src in list(_LIVE_SOURCES))


def close_sessions() -> None:
    """Arrêt du noyau : le fil de libération à l'idle, puis toutes les connexions à en-têtes de session, de toutes
    les sources (appels en cours compris : on arrête)."""
    _stop_idle_thread()
    for src in list(_LIVE_SOURCES):
        src.expire_idle(1e-9, now=time.monotonic() + 1.0, force=True)


def expire_idle(ttl_s: float) -> int:
    """Expiration des connexions MCP de session inactives, toutes sources."""
    return sum(src.expire_idle(ttl_s) for src in list(_LIVE_SOURCES))


# Libération à l'idle des connexions de session (`local.mcp_idle_s`) : UN fil de fond pour tout le processus, lancé à la
# première connexion de session, qui balaie toutes les sources. Sans lui, une connexion gardée (jusqu'à 64, ~3 FD
# chacune) ne se fermait qu'à l'éviction LRU ou à la fin du fil : le balayage d'expiration n'existait que dans le fil
# du bac à sable (`local.code.idle_ttl_s`, 0 par défaut) et se réglait sur ce délai-là.
MCP_IDLE_DEFAULT_S = 300.0
MCP_IDLE_MIN_S, MCP_IDLE_MAX_S = 30.0, 86400.0
_idle_s: float = MCP_IDLE_DEFAULT_S
_idle_step_s: float | None = None  # pas du balayage (tests) ; None = le quart du délai, entre 5 et 60 s
_idle_lock = threading.Lock()
_idle_thread: threading.Thread | None = None
_idle_stop: threading.Event | None = None


def configure_idle(seconds: float) -> None:
    """Délai d'inactivité (`local.mcp_idle_s`) : 0 = désactivé, sinon borné à [30, 86400] s. Valide l'appelant."""
    global _idle_s
    if seconds != 0 and not MCP_IDLE_MIN_S <= seconds <= MCP_IDLE_MAX_S:
        raise ValueError(f"mcp_idle_s invalide : {seconds!r} (0 = désactivé, sinon 30 à 86400 secondes)")
    _idle_s = float(seconds)
    if _idle_s <= 0:
        _stop_idle_thread()


def sweep_idle(now: float | None = None) -> int:
    """Un balayage : ferme, dans toutes les sources, les connexions de session inutilisées depuis `mcp_idle_s`."""
    if _idle_s <= 0:
        return 0
    closed = sum(src.expire_idle(_idle_s, now=now) for src in list(_LIVE_SOURCES))
    if closed:
        logger.debug("mcp: %d connexion(s) de session inactive(s) fermée(s)", closed)
    return closed


def _ensure_idle_thread() -> None:
    global _idle_thread, _idle_stop
    if _idle_s <= 0:
        return
    with _idle_lock:
        if _idle_thread is not None and _idle_thread.is_alive():
            return
        stop = _idle_stop = threading.Event()

        def loop() -> None:
            while not stop.wait(_idle_step_s or max(5.0, min(60.0, _idle_s / 4))):
                try:
                    sweep_idle()
                except Exception:  # le ménage ne s'arrête jamais sur une erreur
                    logger.debug("mcp: balayage des connexions inactives en échec", exc_info=True)

        _idle_thread = threading.Thread(target=loop, name="nokker-mcp-idle", daemon=True)
        _idle_thread.start()


def _stop_idle_thread() -> None:
    global _idle_thread, _idle_stop
    with _idle_lock:
        thread, stop, _idle_thread, _idle_stop = _idle_thread, _idle_stop, None, None
    if stop is not None:
        stop.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=5.0)


@dataclass(eq=False)
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
    # Outil appelé en fin de tour si le tour a utilisé ce serveur (ex. `browser_close` pour le navigateur partagé : un onglet
    # gardé ouvert bloque le suivant sur un site hostile — 2 onglets max par compte ; décision produit du 20.10). None = rien.
    close_on_turn_end: str | None = None
    # En-têtes résolus à l'ouverture de chaque session (références `secret:NOM`, nokker/secrets/refs.py) ; prime sur
    # `headers` quand il est posé.
    header_source: Callable[[], dict[str, str]] | None = None
    # Garde SSRF (voir `_SyncMcp.guard`) : posée pour les serveurs ajoutés par l'utilisateur en mode strict.
    guard: Callable[[str], Any] | None = None
    # Drapeaux (docs/MCP-NEXT.md), éteints par défaut : `modern` = révision 2026-07-28 (négociation, MRTR, Tasks,
    # listes en cache selon `ttlMs`) ; `apps` = MCP Apps (ressource `ui://` transmise au client, jamais au modèle).
    modern: bool = False
    apps: bool = False
    # Domaine dédié des apps (`mcp_apps_origin`, validé au démarrage) : vide = `sandbox: "allow-scripts"` seul.
    apps_origin: str = ""
    task_wait_s: float | None = None
    # Plancher de la durée de cache d'une liste d'outils quand le serveur annonce `ttlMs` (0 = « aussitôt périmée » ;
    # sans plancher, chaque tour relirait `tools/list`). Sans `ttlMs` : gardée jusqu'à `refresh()`, comme avant.
    list_ttl_floor_s: float = 60.0
    # Plafond (caractères) de la description d'un outil du serveur, gardée entière en deçà (`description_max_chars`).
    description_max_chars: int = DESCRIPTION_MAX_CHARS
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    def __post_init__(self) -> None:
        self._io = _SyncMcp(self.url, self.header_source or self.headers, self.timeout_s, guard=self.guard,
                            modern=self.modern, apps=self.apps, task_wait_s=self.task_wait_s,
                            on_list_changed=self.refresh)
        self._cache: list[ToolSpec] | None = None
        self._cache_until: float | None = None  # fin de validité de `_cache` (None = jusqu'à `refresh()`)
        self._ui: dict[str, dict[str, Any]] = {}  # outil -> {resource_uri, visibility} (drapeau `apps`)
        self._app_only: dict[str, ToolSpec] = {}  # outils `visibility: ["app"]` : jamais montrés au modèle
        self._used_by: set[str] = set()
        self._used_lock = threading.Lock()
        self._session_io: dict[tuple, _SyncMcp] = {}  # connexions à en-têtes de session (`mcp_session.py`)
        self._key_threads: dict[tuple, set[str]] = {}  # fils qui s'en servent (fermée quand le dernier part)
        _LIVE_SOURCES.add(self)

    @property
    def _next(self) -> bool:
        return self.modern or self.apps

    def _spec_of(self, t: Any) -> ToolSpec:
        desc = clip_description(t.description, self.description_max_chars, self.name, t.name)
        return ToolSpec(name=t.name, description=desc,
                        parameters=compact_schema(t.input_schema or {"type": "object", "properties": {}}),
                        effect=self.effects.get(t.name, Effect.WRITE), core=t.name in self.core_tools,
                        source=f"mcp:{self.name}")

    def _specs_next(self) -> list[ToolSpec]:
        if self._cache is not None and (self._cache_until is None or self.clock() < self._cache_until):
            return self._cache

        async def _list() -> tuple[list[Any], int | None]:
            client = await self._io.client_async()
            tools, cursor, ttl = [], None, None
            while True:  # toutes les pages (la liste est ordonnée de façon déterministe en 2026-07-28)
                page = await client.list_tools(cursor=cursor)
                tools.extend(page.tools)
                if "ttl_ms" in page.model_fields_set:
                    ttl = page.ttl_ms if ttl is None else min(ttl, page.ttl_ms)
                cursor = page.next_cursor
                if not cursor:
                    return tools, ttl

        tools, ttl_ms = self._io.run(_list())
        out, ui, app_only = [], {}, {}
        for t in tools:
            spec = self._spec_of(t)
            meta = mcp_next.tool_ui(t) if self.apps else None
            if meta is not None:
                ui[t.name] = meta
                if "model" not in meta["visibility"]:
                    app_only[t.name] = spec  # spec ext-apps : un outil sans « model » n'est pas dans la liste de l'agent
                    continue
            out.append(spec)
        self._ui, self._app_only = ui, app_only
        self._cache = out
        self._cache_until = (self.clock() + max(ttl_ms / 1000.0, self.list_ttl_floor_s)
                             if self.modern and ttl_ms is not None else None)
        return out

    def app_specs(self) -> list[ToolSpec]:
        """Outils appelables par une app seulement (`visibility: ["app"]`), hors de la surface du modèle."""
        if self._next:
            self.specs()
        return list(self._app_only.values())

    def app_tools(self) -> frozenset[str]:
        """Outils de CE serveur qu'une app peut appeler (`visibility` contient « app », spec ext-apps)."""
        if self._next:
            self.specs()
        return frozenset(n for n, m in self._ui.items() if "app" in m["visibility"]) | frozenset(self._app_only)

    def specs(self) -> list[ToolSpec]:
        if self._next:
            return self._specs_next()
        if self._cache is not None:
            return self._cache

        async def _list() -> list[ToolSpec]:
            session = await self._io.session_async()
            result = await session.list_tools()
            out = []
            for t in result.tools:
                out.append(ToolSpec(
                    name=t.name, description=clip_description(t.description, self.description_max_chars, self.name, t.name),
                    parameters=compact_schema(t.input_schema or {"type": "object", "properties": {}}),
                    effect=self.effects.get(t.name, Effect.WRITE),
                    core=t.name in self.core_tools, source=f"mcp:{self.name}",
                ))
            return out

        if self.guard is not None:
            from nokker.previews.ssrf import SsrfBlocked

            try:
                self._cache = self._io.run(_list())
            except SsrfBlocked as e:  # adresse refusée : ce serveur n'apporte aucun outil, les autres restent
                logger.warning("mcp(%s): adresse refusée par la garde SSRF : %s", self.name, e)
                self._cache = []
            return self._cache
        self._cache = self._io.run(_list())
        return self._cache

    def refresh(self) -> None:
        self._cache = None
        self._cache_until = None

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

    def _io_for(self, thread_id: str | None) -> _SyncMcp:
        """Connexion de l'appel : celle du serveur, ou, si le fil porte des en-têtes MCP de session (`mcp_session.py`),
        une connexion à part (en-têtes fixes PUIS ceux du fil), gardée par jeu d'en-têtes et fermée avec la source."""
        from nokker.tools import mcp_session

        extra = mcp_session.registry().get(thread_id, self.name)
        if not extra:
            return self._io
        base = self.header_source() if self.header_source is not None else dict(self.headers)
        merged = {**base, **extra}
        key = tuple(sorted(merged.items()))
        tid = thread_id or ""
        doomed: list[_SyncMcp] = []
        with self._used_lock:
            io = self._session_io.pop(key, None)  # relue puis réinsérée en dernier : l'ordre est celui du plus récent
            if io is None:
                while len(self._session_io) >= 64:  # borne : la moins récemment utilisée (hors appel en cours) est fermée
                    old_key = next((k for k, v in self._session_io.items() if not v.active),
                                   next(iter(self._session_io)))
                    doomed.append(self._session_io.pop(old_key))
                    self._key_threads.pop(old_key, None)
                io = _SyncMcp(self.url, merged, self.timeout_s, guard=self.guard, modern=self.modern, apps=self.apps,
                              task_wait_s=self.task_wait_s)
            self._session_io[key] = io
            self._key_threads.setdefault(key, set()).add(tid)
            io.last_used = time.monotonic()
            io.active += 1  # rendu par `_done` : une connexion qui sert un appel n'est jamais libérée à l'idle
        self._close_all(doomed)  # hors verrou : une fermeture lente ne bloque pas les autres appels
        _ensure_idle_thread()
        return io

    def _done(self, io: _SyncMcp) -> None:
        """Fin de l'appel sur `io` : l'inactivité compte à partir de maintenant."""
        if io is self._io:
            return
        with self._used_lock:
            io.active = max(0, io.active - 1)
            io.last_used = time.monotonic()

    def _close_all(self, ios: list[_SyncMcp]) -> None:
        for io in ios:
            try:
                io.close()
            except Exception:
                logger.debug("mcp(%s): fermeture d'une connexion de session", self.name, exc_info=True)

    def release_thread(self, thread_id: str) -> int:
        """Fin d'une session : le fil (et ses sous-agents) n'a plus de connexion à en-têtes ; celles que plus aucun fil
        n'utilise sont fermées. Rend le nombre de connexions fermées."""
        doomed: list[_SyncMcp] = []
        with self._used_lock:
            for key in list(self._key_threads):
                users = self._key_threads[key]
                users -= {t for t in users if t == thread_id or t.startswith(thread_id + "/sa-")}
                if not users:
                    del self._key_threads[key]
                    io = self._session_io.pop(key, None)
                    if io is not None:
                        doomed.append(io)
        self._close_all(doomed)
        return len(doomed)

    def expire_idle(self, ttl_s: float, now: float | None = None, force: bool = False) -> int:
        """Ferme les connexions de session sans appel depuis `ttl_s` secondes (rouvertes au prochain appel). Une
        connexion qui sert un appel en cours n'est jamais fermée (sauf `force`, à l'arrêt). Jamais la connexion commune."""
        if ttl_s <= 0:
            return 0
        now = time.monotonic() if now is None else now
        doomed: list[_SyncMcp] = []
        with self._used_lock:
            for key, io in list(self._session_io.items()):
                if now - io.last_used >= ttl_s and (force or not io.active):
                    doomed.append(self._session_io.pop(key))
                    self._key_threads.pop(key, None)
        self._close_all(doomed)
        return len(doomed)

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if self.close_on_turn_end and name != self.close_on_turn_end:
            with self._used_lock:
                self._used_by.add(ctx.thread_id)
        io = self._io_for(ctx.thread_id)
        try:
            return self._call_on(io, name, arguments, ctx)
        finally:
            self._done(io)

    def _call_on(self, io: _SyncMcp, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if self._next:
            return self._call_next(name, arguments, ctx, io)

        async def _call():
            session = await io.session_async()
            return await session.call_tool(name, arguments)

        try:
            result = io.run(_call())
        except Exception as e:
            logger.warning("mcp(%s): outil %s en échec", self.name, name, exc_info=True)
            from nokker.tools.result_fence import mcp_error_text

            return ToolResult(call_id="", name=name, content=mcp_error_text(self.name, name, e), is_error=True)
        from nokker.tools.result_fence import fence_mcp_result

        # r44 : le texte d'un serveur MCP est un contenu de tiers, clôturé (même fonction que le portage).
        return ToolResult(call_id="", name=name, content=fence_mcp_result(self.name, name, text_of(result)),
                           is_error=bool(getattr(result, "is_error", False)))

    def _call_next(self, name: str, arguments: dict[str, Any], ctx: ToolContext, io: _SyncMcp | None = None
                   ) -> ToolResult:
        """Appel par `mcp.client.Client` : MRTR et Tasks conduits par le SDK ; avec `apps`, la ressource `ui://` de
        l'outil part dans `ToolResult.app` (client seulement) et le modèle n'en reçoit que `APP_SUMMARY`. Un appel
        venu d'une app (canal `mcp-app`) rend le texte de l'outil tel quel : ni nouvelle app, ni résumé."""
        io = io or self._io
        from_app = ctx.channel == "mcp-app"
        apps = self.apps and not from_app
        if apps:
            try:
                self.specs()  # `_meta.ui` des outils : lu avec la liste (en cache)
            except Exception:  # noqa: BLE001 — l'appel lui-même dira si le serveur répond
                logger.debug("mcp(%s): liste des outils indisponible avant l'appel", self.name, exc_info=True)
        ui = self._ui.get(name) if apps else None

        async def _call():
            client = await io.client_async()
            result = await client.call_tool(name, arguments)
            resource = mcp_next.embedded_ui(result) if apps else None
            if resource is None and ui is not None and not getattr(result, "is_error", False):
                read = await client.read_resource(ui["resource_uri"])
                resource = next((c for c in read.contents if str(c.uri) == ui["resource_uri"]), None)
            return result, resource

        try:
            result, resource = io.run(_call())
        except Exception as e:
            logger.warning("mcp(%s): outil %s en échec", self.name, name, exc_info=True)
            from nokker.tools.result_fence import mcp_error_text

            # r44 : ce que dit le noyau hors clôture, le message d'erreur du serveur dedans
            return ToolResult(call_id="", name=name, content=mcp_error_text(self.name, name, e), is_error=True)
        app = None
        if resource is not None:
            app = mcp_next.app_payload(server=self.name, tool=name, resource=resource,
                                       visibility=(ui or {}).get("visibility") or ["model", "app"],
                                       arguments=arguments, result=result, apps_origin=self.apps_origin)
        from nokker.tools.result_fence import fence_mcp_result

        content = text_of(result, skip_ui=self.apps)
        if isinstance(result, mcp_next.KernelNote):
            # note du noyau : hors clôture ; tâche en échec : phrase du noyau hors clôture, message du serveur dedans
            if result.note:
                content = fence_mcp_result(self.name, name, content, note=result.note)
        elif not from_app:
            # r44 : texte du serveur clôturé pour le modèle ; une app (canal mcp-app) reçoit le texte de l'outil tel quel
            content = fence_mcp_result(self.name, name, content)
        if app is not None:
            summary = mcp_next.APP_SUMMARY.format(tool=name)
            content = f"{content}\n{summary}" if content else summary
        return ToolResult(call_id="", name=name, content=content, is_error=bool(getattr(result, "is_error", False)),
                          app=app)

    def close(self) -> None:
        self._io.close()
        with self._used_lock:
            extra, self._session_io = list(self._session_io.values()), {}
            self._key_threads = {}
        self._close_all(extra)


__all__ = ["McpToolSource", "_SyncMcp", "close_sessions", "configure_idle", "expire_idle", "release_thread",
           "sweep_idle", "text_of"]
