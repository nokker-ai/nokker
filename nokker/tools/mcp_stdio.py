"""nokker/tools/mcp_stdio.py — serveurs MCP en transport stdio (docs/NOKKAN-MCP-STDIO.md), drapeau `mcp_stdio.enabled`.

Un serveur stdio est un PROCESSUS lancé pour le compte de l'utilisateur : de l'exécution de code. Il tourne donc DANS
le bac à sable du fil (`CodeToolSource.script_command` : `docker exec -i` dans le container du fil, comme
`execute_code`), jamais dans le processus ni le container du noyau ; le noyau ne tient que les deux bouts du tube
(stdin/stdout) et y parle MCP avec le client stdio du SDK `mcp`.

- Un processus par (serveur, fil), lancé au premier appel du fil (paresseux), arrêté à l'inactivité, à la fermeture du
  noyau ou quand le bac à sable part ; mort constatée (`Connection closed`) : l'appel en cours rend une erreur (jamais
  rejoué : une écriture ne s'exécute pas deux fois), le suivant relance le processus.
- Identité : variables d'environnement de la SESSION (`SessionEnv`, posées par l'hôte du noyau) ou du coffre
  (`secret:NOM`), déclarées PAR NOM. Les valeurs ne passent ni par un argument de commande (`docker exec -e NOM` : la
  valeur voyage dans l'environnement du client docker), ni par un journal, ni par un schéma. Une valeur d'identité
  est traitée comme un SECRET : au lancement elle rejoint les valeurs connues du noyau (`invariants.known`), et tout
  ce que le serveur rend (résultat normal, erreur, description d'outil) passe par le caviardage existant
  (`nokker/qa/secrets.py::redact`), qui la remplace par une poignée `[secret:…]`. stderr du serveur est jeté.
- Outils : dans le registre comme ceux des serveurs HTTP (source `mcp:<serveur>`, effet WRITE par défaut, mêmes portes
  et mêmes cartes), d'après un CATALOGUE gardé sur disque : le registre n'a pas de fil, et un processus n'existe que
  dans un fil. Le catalogue est relevé au premier lancement (fin du premier tour d'un fil à session de code) et
  rafraîchi à chaque lancement.
- Serveur déclaré dans `config.yaml` : une carte forte à son PREMIER lancement, décision mémorisée par serveur et
  par empreinte de la déclaration (`Approvals`) ; `mcp_stdio.trust_config: true` (éteint par défaut) garde l'ancien
  comportement (config.yaml vaut approbation).
- Interdit hors Lokkal (`stdio_refusal`) : multi-locataire, sessions de code multi-utilisateur. Éteint par défaut
  partout ; éteint = ce module n'est pas importé par le noyau.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets as _secrets
import shlex
import tempfile
import threading
import time
import weakref
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult

log = logging.getLogger("nokker.tools.mcp_stdio")

FILE_NAME = "mcp-stdio-servers.yaml"
APPROVALS_FILE = "mcp-stdio-approvals.json"
CATALOG_DIR = "mcp-stdio"
MIN_REDACT_LEN = 4  # en dessous, une valeur d'identité n'entre pas au caviardage (elle abîmerait tout texte)
IDENTITY_KIND = "identite-session"  # poignée d'une valeur d'identité : [secret:identite-session-<empreinte>]
MAX_SERVERS = 16
MAX_ARGS = 32
MAX_ENV = 16
MAX_TOOLS = 128
SESSION_REF = "session"  # valeur posée par l'hôte pour CE fil, sous le même nom (`SessionEnv`)
THREAD_REF = "thread"  # l'identifiant du fil
MARK_ENV = "NOKKER_MCP"  # marque du processus dans le bac à sable (ménage à l'arrêt, comme NOKKER_BG)

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_REF = re.compile(r"^secret:([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$")
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# Variables qui changent ce que le processus CHARGE : jamais posées par une déclaration.
_RESERVED_ENV = re.compile(r"^(?:PATH|HOME|IFS|ENV|BASH_ENV|SHELL|LD_.*|DYLD_.*|NOKKER_.*|PYTHONSTARTUP|PYTHONPATH|"
                           r"NODE_OPTIONS|DOCKER_.*)$")

# Textes rendus au modèle (gelés, docs/nokkan-mcp-stdio-textes.md) : ils passent tous par `mcp_error_text`.
ERR_NO_SESSION = "pas de session de code sur ce fil : un serveur MCP stdio tourne dans son bac à sable"
ERR_BACKEND = "un serveur MCP stdio ne tourne que dans un bac à sable docker"
ERR_ENV = "variable de session absente : {names}"
ERR_START = "le serveur n'a pas démarré dans le bac à sable"
ERR_DEAD = "le processus du serveur s'est arrêté ; il sera relancé au prochain appel"
ERR_TIMEOUT = "délai dépassé ({seconds} s)"
ERR_LIMIT = "trop de serveurs MCP stdio en cours"
ERR_PENDING = "serveur en attente de l'accord de la personne (carte)"


class McpStdioError(ValueError):
    """Déclaration refusée (le message revient à l'utilisateur ou au modèle : jamais de valeur dedans)."""


class _Kernel(RuntimeError):
    """Échec côté noyau, au texte fixe (rendu par `mcp_error_text`, hors clôture)."""


# ─────────────────────────────── déclaration ───────────────────────────────


def _looks_like_value(text: str) -> bool:
    from nokker.agents.spec import _looks_like_value as looks

    return looks(text)


def check_env(raw: Any) -> dict[str, str]:
    """`env` d'une déclaration : {NOM: `secret:NOM` | `session` | `thread`}. Une valeur en clair est refusée."""
    if raw in (None, "", {}):
        return {}
    if isinstance(raw, list):  # liste de noms : chacun vient de la session
        raw = {str(n): SESSION_REF for n in raw}
    if not isinstance(raw, dict) or len(raw) > MAX_ENV:
        raise McpStdioError(f"env : un objet de {MAX_ENV} variables au plus")
    out: dict[str, str] = {}
    for key, value in raw.items():
        key, value = str(key).strip(), str(value or "").strip()
        if not _ENV_NAME.match(key) or _RESERVED_ENV.match(key):
            raise McpStdioError(f"env : nom de variable refusé {key[:40]!r}")
        if value not in (SESSION_REF, THREAD_REF) and not _REF.match(value):
            raise McpStdioError(f"env {key} : secret:NOM, session ou thread attendu, jamais une valeur")
        out[key] = value
    return out


def check_stdio_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """Déclaration validée et normalisée d'un serveur stdio (jamais de valeur de secret, jamais de commande que le
    noyau n'exécute pas même approuvée)."""
    name = str(raw.get("name") or "").strip()
    if not _NAME.match(name):
        raise McpStdioError("nom refusé : minuscules, chiffres et tirets, 40 au plus")
    command = str(raw.get("command") or "").strip()
    if not command or len(command) > 200 or _BAD_CHARS.search(command) or " " in command:
        raise McpStdioError("commande refusée : un programme (sans espace), ses arguments dans args")
    args = raw.get("args") or []
    if not isinstance(args, list) or len(args) > MAX_ARGS:
        raise McpStdioError(f"args : une liste de {MAX_ARGS} arguments au plus")
    args = [str(a) for a in args]
    for a in [command, *args]:
        if len(a) > 500 or _BAD_CHARS.search(a):
            raise McpStdioError("args : argument refusé (trop long, ou caractère de contrôle)")
        if _looks_like_value(a) or any(_looks_like_value(p) for p in re.split(r"[=:,]", a)):
            raise McpStdioError("args : un argument ressemble à une clé (elle passe par env: secret:NOM)")
    from nokker.tools.code import hardline_reason

    if hardline_reason(shlex.join([command, *args])):
        raise McpStdioError("commande refusée : jamais exécutée, même approuvée")
    entry: dict[str, Any] = {"name": name, "command": command}
    if args:
        entry["args"] = args
    env = check_env(raw.get("env"))
    if env:
        entry["env"] = env
    purpose = " ".join(str(raw.get("purpose") or "").split())[:200]
    if purpose:
        entry["purpose"] = purpose
    from nokker.tools.mcp_config import McpConfigError, check_read_tools

    try:
        reads, confirmed = check_read_tools(
            raw.get("read_tools"), raw["confirm_writes"] if "confirm_writes" in raw else raw.get("confirmed_writes"))
    except McpConfigError as e:
        raise McpStdioError(str(e)) from None
    if reads:
        entry["read_tools"] = reads
    if confirmed:
        entry["confirmed_writes"] = confirmed
    entry["approved"] = bool(raw.get("approved", False))
    return entry


def secret_names(entry: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for value in (entry.get("env") or {}).values():
        m = _REF.match(str(value))
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def same_declaration(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Même commande, mêmes arguments, mêmes variables : ce qu'une approbation couvre."""
    return all((a.get(k) or None) == (b.get(k) or None) for k in ("command", "args", "env"))


def fingerprint(entry: dict[str, Any]) -> str:
    """Empreinte de ce qu'une approbation couvre : la commande, ses arguments, ses variables (noms et provenance).
    Un libellé ou des `read_tools` qui changent ne la changent pas."""
    body = json.dumps([str(entry.get("command") or ""), [str(a) for a in entry.get("args") or []],
                       sorted((str(k), str(v)) for k, v in (entry.get("env") or {}).items())], ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class Approvals:
    """Accords donnés aux serveurs de `config.yaml` (`<état>/mcp-stdio-approvals.json`, 0600) : nom → empreinte de la
    déclaration approuvée. Relu à chaque question (la carte est signée pendant que le noyau tourne) ; un fichier
    absent ou illisible n'approuve rien."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def granted(self, entry: dict[str, Any]) -> bool:
        return self._read().get(str(entry.get("name") or "")) == fingerprint(entry)

    def grant(self, entry: dict[str, Any]) -> None:
        with self._lock:
            data = {**self._read(), str(entry["name"]): fingerprint(entry)}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".mcp-stdio-approvals-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise


def load_stdio_servers(path: Path) -> list[dict[str, Any]]:
    """Déclarations valides de `mcp-stdio-servers.yaml` ; une entrée illisible est sautée, jamais devinée."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return []
    except (OSError, yaml.YAMLError) as e:
        log.warning("%s illisible, ignoré : %s", path, e)
        return []
    out: list[dict[str, Any]] = []
    for raw in (data.get("servers") or [])[:MAX_SERVERS] if isinstance(data, dict) else []:
        try:
            out.append(check_stdio_entry(raw if isinstance(raw, dict) else {}))
        except McpStdioError as e:
            log.warning("serveur MCP stdio ignoré dans %s : %s", path, e)
    return out


def write_stdio_servers(path: Path, servers: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".mcp-stdio-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("# Serveurs MCP stdio déclarés (mcp_server_add, import). Variables par NOM ; approved: false = à "
                    "approuver.\n")
            yaml.safe_dump({"servers": servers}, f, sort_keys=False, allow_unicode=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def stdio_refusal(config: Any) -> str | None:
    """Motif pour lequel les serveurs stdio sont interdits sur CETTE instance, ou None (permis). Permis seulement dans
    Lokkal (un propriétaire, son bac à sable) ; interdit en multi-locataire et pour les sessions de code des comptes."""
    if getattr(config, "local", None) is None:
        return "instance multi-locataire (pas de bloc `local`) : serveurs MCP stdio interdits"
    code = getattr(config, "code", None)
    if isinstance(code, dict) and code.get("enabled"):
        return "sessions de code multi-utilisateur (`code.enabled`) : serveurs MCP stdio interdits"
    return None


def stdio_enabled(config: Any) -> bool:
    block = getattr(config, "mcp_stdio", None)
    return isinstance(block, dict) and block.get("enabled") is True


# ─────────────────────────────── identité de session ───────────────────────────────


class SessionEnv:
    """Variables d'environnement d'une session, posées par l'HÔTE du noyau (jamais par le modèle) : fil → {NOM:
    valeur}. En mémoire seulement ; aucune valeur n'en sort que vers l'environnement d'un serveur qui déclare ce nom."""

    def __init__(self) -> None:
        self._by_thread: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    def set(self, thread_id: str, values: dict[str, Any]) -> list[str]:
        """Remplace les variables du fil ; rend leurs NOMS. `McpStdioError` si un nom ou une valeur est refusé."""
        if not isinstance(values, dict) or len(values) > 4 * MAX_ENV:
            raise McpStdioError("variables de session : un objet {NOM: valeur}")
        clean: dict[str, str] = {}
        for key, value in values.items():
            key = str(key)
            if not _ENV_NAME.match(key) or _RESERVED_ENV.match(key):
                raise McpStdioError(f"variables de session : nom refusé {key[:40]!r}")
            if not isinstance(value, str) or len(value) > 4096 or _BAD_CHARS.search(value):
                raise McpStdioError(f"variables de session : valeur de {key} refusée (texte d'une ligne attendu)")
            clean[key] = value
        with self._lock:
            self._by_thread[thread_id] = clean
        return sorted(clean)

    def get(self, thread_id: str) -> dict[str, str]:
        with self._lock:
            return dict(self._by_thread.get(thread_id) or {})

    def drop(self, thread_id: str) -> None:
        with self._lock:
            self._by_thread.pop(thread_id, None)

    def __repr__(self) -> str:  # jamais de valeur dans un journal
        return f"SessionEnv({len(self._by_thread)} fils)"


def _base_thread(thread_id: str) -> str:
    """Un sous-agent `<fil>/sa-<id>` partage le bac à sable, les serveurs et l'identité de son fil parent."""
    return thread_id.split("/sa-", 1)[0]


def identity_name(value: str) -> str:
    """Nom de poignée d'une valeur d'identité : stable, sans la valeur ni le nom de la variable."""
    from nokker.qa.secrets import _type_name

    return _type_name(IDENTITY_KIND, value)


# ─────────────────────────────── connexion à un processus ───────────────────────────────


class _Conn:
    """Un processus serveur dans un bac à sable et sa session MCP. Ouverture ET fermeture dans UNE tâche (anyio refuse
    de quitter une portée d'annulation depuis une autre tâche)."""

    def __init__(self, cmd: list[str], env: dict[str, str], mark: str, clock: Callable[[], float]) -> None:
        self.cmd, self.env, self.mark, self.clock = cmd, env, mark, clock
        # Environnement du CLIENT docker : les variables nommées par `-e` (lues là par docker), plus les réglages
        # `DOCKER_*` du noyau (DOCKER_HOST = le proxy du socket dans Lokkal) — le SDK ne transmet de lui-même que HOME,
        # PATH… Ces réglages restent côté client : seuls les noms passés par `-e` entrent dans le bac à sable.
        self.client_env = {**{k: v for k, v in os.environ.items() if k.startswith("DOCKER_")}, **env}
        self.session: Any = None
        self.dead = False
        self.last_used = clock()
        self.busy = 0
        self._closing: asyncio.Event | None = None
        self._holder: asyncio.Task | None = None

    async def open(self) -> Any:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        loop = asyncio.get_running_loop()
        opened: asyncio.Future = loop.create_future()
        self._closing = asyncio.Event()
        params = StdioServerParameters(command=self.cmd[0], args=self.cmd[1:], env=dict(self.client_env))

        async def holder() -> None:
            try:
                # stderr du serveur : jeté (il peut porter son environnement ; rien n'en va au journal du noyau).
                with open(os.devnull, "w", encoding="utf-8") as sink:
                    async with stdio_client(params, errlog=sink) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            if not opened.done():
                                opened.set_result(session)
                            await self._closing.wait()
            except BaseException as e:  # noqa: BLE001 — remis à l'ouvreur s'il attend encore
                while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
                    e = e.exceptions[0]
                if not opened.done():
                    opened.set_exception(e if isinstance(e, Exception) else _Kernel(ERR_START))
            finally:
                self.dead = True

        self._holder = loop.create_task(holder())
        try:
            self.session = await opened
        except BaseException:
            await self.aclose()
            raise
        return self.session

    async def aclose(self) -> None:
        self.dead = True
        if self._closing is not None:
            self._closing.set()
        if self._holder is not None and not self._holder.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._holder), 8.0)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — fermeture au mieux
                self._holder.cancel()


def _closed(exc: BaseException) -> bool:
    """Le tube est fermé : le processus est mort (ou le bac à sable est parti)."""
    try:
        import anyio
        from mcp.shared.exceptions import MCPError
        from mcp.types import CONNECTION_CLOSED
    except ImportError:  # pragma: no cover
        return False
    if isinstance(exc, MCPError):
        return getattr(getattr(exc, "error", None), "code", None) == CONNECTION_CLOSED
    return isinstance(exc, (anyio.ClosedResourceError, anyio.BrokenResourceError, BrokenPipeError, ConnectionError))


# ─────────────────────────────── source d'outils ───────────────────────────────


_LIVE_SOURCES: weakref.WeakSet = weakref.WeakSet()


def release_thread(thread_id: str) -> None:
    """Fin d'une session : le processus MCP stdio du fil s'arrête (toutes sources)."""
    for src in list(_LIVE_SOURCES):
        try:
            src.close_thread(thread_id)
        except Exception:  # noqa: BLE001
            log.debug("mcp-stdio: arrêt du processus d'un fil")


@dataclass(eq=False)
class StdioMcpToolSource:
    """Un serveur MCP stdio = une source d'outils (`mcp:<nom>`), un processus par fil dans le bac à sable du fil.

    `resolve(thread_id)` rend la source de code du fil (`script_command`, `run_hook`, `backend`) ou None hors session.
    `secret(nom)` rend la valeur d'un secret du coffre (ou lève). `on_catalog()` : le catalogue a changé (le registre
    doit relire les specs)."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)  # NOM -> secret:NOM | session | thread
    resolve: Callable[[str], Any] = field(default=lambda thread_id: None, repr=False)
    session_env: SessionEnv | None = field(default=None, repr=False)
    secret: Callable[[str], str] | None = field(default=None, repr=False)
    catalog_path: Path | None = None
    on_catalog: Callable[[], None] | None = field(default=None, repr=False)
    # Valeurs connues du NOYAU (`kernel.invariants.known`), lues à l'usage : les valeurs d'identité y entrent au
    # lancement, et le caviardage du noyau (résultats de tout outil, réponse, fil) les retire. None : hors noyau.
    known: Callable[[], Any] | None = field(default=None, repr=False)
    # Serveur de config.yaml sans `trust_config` : `gate()` dit si SA déclaration est approuvée (`Approvals`) ;
    # `ask(fil, déclaration)` pose la carte (vrai si posée). None : pas de porte (déclaré sous carte, ou de confiance).
    gate: Callable[[], bool] | None = field(default=None, repr=False)
    ask: Callable[[str, dict[str, Any]], bool] | None = field(default=None, repr=False)
    effects: dict[str, Effect] = field(default_factory=dict)
    timeout_s: float = 30.0
    start_timeout_s: float = 30.0
    idle_ttl_s: float = 900.0
    max_processes: int = 8
    description_max_chars: int = 4000  # plafond d'une description d'outil, gardée entière en deçà (nokker.tools.schema)
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    def __post_init__(self) -> None:
        self._conns: dict[str, _Conn] = {}
        _LIVE_SOURCES.add(self)
        self._mu = threading.Lock()
        self._start_mu: dict[str, threading.Lock] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._catalog: list[dict[str, Any]] | None = self._read_catalog()
        self._probe_failed: set[str] = set()
        self._asked: str | None = None  # empreinte de la déclaration pour laquelle la carte a été posée
        from nokker.qa.secrets import KnownSecrets

        self._known = KnownSecrets()  # valeurs injectées par CETTE source : son propre filet, même hors noyau

    # ── caviardage : le mécanisme existant du noyau, pas un second ──
    def _remember(self, env: dict[str, str]) -> None:
        """Les valeurs injectées rejoignent les valeurs connues : celles de la source et celles du noyau."""
        try:
            kernel = self.known() if self.known is not None else None
        except Exception:  # noqa: BLE001 — noyau pas encore construit : le filet de la source suffit
            kernel = None
        for key, ref in self.env.items():
            value = env.get(key)
            if ref == THREAD_REF or not value or len(value) < MIN_REDACT_LEN:
                continue
            m = _REF.match(ref)
            name = m.group(1) if m else identity_name(value)
            self._known.add(name, value)
            if kernel is not None and hasattr(kernel, "add"):
                kernel.add(name, value)

    def _redact(self, text: str) -> str:
        from nokker.qa.secrets import redact

        return redact(text, self._known, weak=False).text

    def declaration(self) -> dict[str, Any]:
        """Ce que la personne approuve : les arguments de `mcp_server_add` pour ce serveur."""
        return {"name": self.name, "command": self.command, "args": list(self.args), "env": dict(self.env)}

    def _open_gate(self) -> bool:
        try:
            return self.gate is None or bool(self.gate())
        except Exception:  # noqa: BLE001 — accord illisible : pas d'accord
            return False

    # ── boucle asyncio dédiée (les ports du noyau sont synchrones, comme `_SyncMcp`) ──
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._mu:
            if self._loop is None:
                ready = threading.Event()

                def runner() -> None:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    self._loop = loop
                    ready.set()
                    loop.run_forever()

                self._thread = threading.Thread(target=runner, name=f"mcp-stdio:{self.name}", daemon=True)
                self._thread.start()
                ready.wait()
            assert self._loop is not None
            return self._loop

    def _run(self, coro: Any, timeout: float) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        try:
            return future.result(timeout)
        except FutureTimeoutError:
            future.cancel()
            raise

    # ── catalogue ──
    def _read_catalog(self) -> list[dict[str, Any]] | None:
        if self.catalog_path is None:
            return None
        try:
            data = json.loads(Path(self.catalog_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        tools = data.get("tools") if isinstance(data, dict) else None
        if not isinstance(tools, list) or data.get("command") != [self.command, *self.args]:
            return None  # commande changée : catalogue à relever
        return [t for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)][:MAX_TOOLS]

    def _set_catalog(self, tools: list[Any]) -> None:
        from nokker.tools.schema import clip_description

        cat = []
        for t in tools[:MAX_TOOLS]:
            desc = clip_description(self._redact((t.description or "").strip()), self.description_max_chars, self.name, t.name)
            schema = t.input_schema or {"type": "object", "properties": {}}
            # Une valeur injectée ne doit jamais se retrouver dans un schéma (un serveur qui s'y décrirait avec).
            cat.append({"name": t.name, "description": desc,
                        "input_schema": json.loads(self._redact(json.dumps(schema, ensure_ascii=False)))})
        if cat == self._catalog:
            return
        self._catalog = cat
        if self.catalog_path is not None:
            try:
                p = Path(self.catalog_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(".tmp")
                tmp.write_text(json.dumps({"command": [self.command, *self.args], "tools": cat}, ensure_ascii=False),
                               encoding="utf-8")
                os.replace(tmp, p)
            except OSError as e:
                log.warning("mcp-stdio(%s): catalogue non écrit : %s", self.name, type(e).__name__)
        if self.on_catalog is not None:
            try:
                self.on_catalog()
            except Exception:  # noqa: BLE001 — le catalogue reste, le registre le relira au prochain rafraîchissement
                log.warning("mcp-stdio(%s): rafraîchissement du registre en échec", self.name)

    def specs(self) -> list[ToolSpec]:
        from nokker.tools.schema import compact_schema

        if not self._open_gate():  # déclaration pas (ou plus) approuvée : rien n'est offert
            return []
        return [ToolSpec(name=t["name"], description=str(t.get("description") or ""),
                         parameters=compact_schema(t.get("input_schema") or {"type": "object", "properties": {}}),
                         effect=self.effects.get(t["name"], Effect.WRITE), core=False, source=f"mcp:{self.name}")
                for t in self._catalog or []]

    def refresh(self) -> None:  # le catalogue ne se relit qu'à un lancement : rien à oublier ici
        return None

    # ── environnement ──
    def _environment(self, thread_id: str) -> dict[str, str]:
        """Valeurs des variables déclarées, pour CE fil. `_Kernel` si une variable de session manque (un serveur lancé
        sans son identité pourrait se croire sans restriction) ; le message ne nomme que des NOMS."""
        held = self.session_env.get(thread_id) if self.session_env is not None else {}
        out: dict[str, str] = {}
        missing: list[str] = []
        for key, ref in self.env.items():
            if ref == THREAD_REF:
                out[key] = thread_id
            elif ref == SESSION_REF:
                if key in held:
                    out[key] = held[key]
                else:
                    missing.append(key)
            else:
                m = _REF.match(ref)
                try:
                    value = self.secret(m.group(1)) if m and self.secret is not None else None
                except Exception:  # noqa: BLE001 — coffre scellé, secret absent : jamais de détail
                    value = None
                if not isinstance(value, str) or not value:
                    missing.append(key)
                else:
                    out[key] = value
        if missing:
            raise _Kernel(ERR_ENV.format(names=", ".join(sorted(missing))))
        return out

    # ── cycle de vie ──
    def _conn(self, thread_id: str) -> _Conn:
        """La connexion du fil, lancée si besoin (un seul lancement à la fois par fil)."""
        with self._mu:
            conn = self._conns.get(thread_id)
            lock = self._start_mu.setdefault(thread_id, threading.Lock())
        if conn is not None and not conn.dead:
            return conn
        with lock:
            with self._mu:
                conn = self._conns.get(thread_id)
                if conn is not None and not conn.dead:
                    return conn
                self._conns.pop(thread_id, None)
            if conn is not None:
                self._stop(thread_id, conn)  # mort constatée : le reste du processus est ramassé avant la relance
            if not self._open_gate():
                raise _Kernel(ERR_PENDING)
            src = self.resolve(thread_id)
            if src is None:
                raise _Kernel(ERR_NO_SESSION)
            if getattr(src, "backend", "docker") != "docker":
                raise _Kernel(ERR_BACKEND)
            self.reap_idle()
            with self._mu:
                if len(self._conns) >= self.max_processes:
                    raise _Kernel(ERR_LIMIT)
            env = self._environment(thread_id)
            self._remember(env)  # AVANT le lancement : rien de ce que le serveur dira n'échappe au caviardage
            mark = _secrets.token_hex(8)
            env[MARK_ENV] = mark
            try:
                cmd = src.script_command(thread_id, [self.command, *self.args], env_names=sorted(env))
            except PermissionError:
                raise _Kernel(ERR_BACKEND) from None
            conn = _Conn(cmd, env, mark, self.clock)

            async def _open() -> list[Any]:
                session = await conn.open()
                return list((await session.list_tools()).tools)

            try:
                tools = self._run(_open(), self.start_timeout_s)
            except (KeyboardInterrupt, SystemExit):
                self._stop(thread_id, conn)
                raise
            except BaseException as e:  # noqa: BLE001 — annulation comprise (CancelledError n'est pas une Exception)
                # Jamais `exc_info` ni le message : une exception du lancement peut citer l'environnement.
                log.warning("mcp-stdio(%s): lancement en échec sur le fil %s (%s)", self.name, thread_id,
                            type(e).__name__)
                self._stop(thread_id, conn)
                if isinstance(e, FutureTimeoutError):
                    raise _Kernel(ERR_TIMEOUT.format(seconds=int(self.start_timeout_s))) from None
                raise _Kernel(ERR_START) from None
            with self._mu:
                self._conns[thread_id] = conn
            self._set_catalog(tools)
            return conn

    def _stop(self, thread_id: str, conn: _Conn) -> None:
        self._stop_all([(thread_id, conn)])

    def _stop_all(self, conns: list[tuple[str, _Conn]]) -> None:
        """Arrêt propre : fermeture des sessions (stdin fermé ; le SDK laisse 2 s au serveur, puis termine le client),
        toutes ensemble, puis ménage DANS le bac à sable de ce qui porterait encore la marque (un `docker exec` tué
        ne tue pas toujours son processus)."""
        if not conns:
            return
        if self._loop is not None:
            async def _close() -> None:
                await asyncio.gather(*(c.aclose() for _, c in conns), return_exceptions=True)

            try:
                self._run(_close(), 15.0)
            except Exception:  # noqa: BLE001 — fermeture au mieux
                log.warning("mcp-stdio(%s): fermeture en échec (%d processus)", self.name, len(conns))
        for thread_id, conn in conns:
            try:
                src = self.resolve(thread_id)
                if src is not None and hasattr(src, "run_hook"):
                    src.run_hook(thread_id, _REAP.format(mark=conn.mark), "", 15)
            except Exception:  # noqa: BLE001 — bac à sable déjà parti : rien n'y tourne plus
                pass

    def reap_idle(self) -> int:
        """Arrête les processus inactifs depuis `idle_ttl_s` (aucun appel en cours). Rend le nombre arrêté."""
        now = self.clock()
        with self._mu:
            idle = [(t, c) for t, c in self._conns.items()
                    if c.dead or (c.busy == 0 and now - c.last_used > self.idle_ttl_s)]
            for t, _ in idle:
                self._conns.pop(t, None)
        self._stop_all(idle)
        return len(idle)

    def close_thread(self, thread_id: str) -> None:
        """Fin du fil : son processus s'arrête (l'identité du fil reste dans `SessionEnv`, que d'autres serveurs
        lisent ; c'est l'hôte qui l'oublie, `SessionEnv.drop`)."""
        thread_id = _base_thread(thread_id)
        with self._mu:
            conn = self._conns.pop(thread_id, None)
            self._start_mu.pop(thread_id, None)
        if conn is not None:
            self._stop(thread_id, conn)

    def running(self) -> list[str]:
        with self._mu:
            return sorted(t for t, c in self._conns.items() if not c.dead)

    def turn_done(self, thread_id: str) -> None:
        """Fin de tour : premier relevé du catalogue (fil à session de code, une tentative par fil) et ménage des
        processus inactifs."""
        thread_id = _base_thread(thread_id)
        if not self._allowed():
            return
        if not self._open_gate():
            self._ask(thread_id)
        elif self._catalog is None:
            self._probe(thread_id)
        self.reap_idle()

    def _probe(self, thread_id: str) -> None:
        if thread_id in self._probe_failed:
            return
        try:
            if self.resolve(thread_id) is not None:
                self._conn(thread_id)
        except Exception as e:  # noqa: BLE001 — pas de catalogue : pas d'outils, le motif au journal sans valeur
            self._probe_failed.add(thread_id)
            log.warning("mcp-stdio(%s): catalogue non relevé sur le fil %s : %s", self.name, thread_id,
                        e if isinstance(e, _Kernel) else type(e).__name__)

    def _ask(self, thread_id: str) -> None:
        """Premier lancement d'un serveur de config.yaml pas encore approuvé : UNE carte, dans le premier fil à session
        de code qui finit un tour ; pas de nouvelle carte tant que la déclaration ne change pas (une carte refusée
        n'est pas reposée avant le redémarrage du noyau). Rien n'est lancé."""
        if self.ask is None:
            return
        decl = self.declaration()
        mark = fingerprint(decl)
        if self._asked == mark:
            return
        try:
            if self.resolve(thread_id) is None:
                return
            if self.ask(thread_id, decl):
                self._asked = mark
        except Exception as e:  # noqa: BLE001 — la carte n'a pas pu être posée : redemandée au tour suivant
            log.warning("mcp-stdio(%s): carte d'approbation non posée (%s)", self.name, type(e).__name__)

    def approved(self, thread_id: str) -> None:
        """Suite de la carte signée : premier lancement dans le fil de la carte (relevé du catalogue), puis le registre
        relit les specs. Sans effet si l'accord n'est pas là."""
        if not self._open_gate():
            return
        thread_id = _base_thread(thread_id)
        self._probe_failed.discard(thread_id)
        had = self._catalog is not None
        self._probe(thread_id)
        if had and self.on_catalog is not None:  # catalogue déjà gardé : les outils redeviennent offerts
            try:
                self.on_catalog()
            except Exception:  # noqa: BLE001
                log.warning("mcp-stdio(%s): rafraîchissement du registre en échec", self.name)

    @staticmethod
    def _allowed() -> bool:
        """Deuxième verrou du refus multi-locataire : seul le locataire interne (le propriétaire de Lokkal)."""
        from nokker.tenancy.context import INTERNAL_TENANT, NoTenant, current_tenant

        try:
            return current_tenant() == INTERNAL_TENANT
        except NoTenant:
            return True  # hors de tout locataire : appel direct (tests, bancs), comme les autres sources

    # ── appels ──
    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from nokker.tools.mcp import text_of
        from nokker.tools.registry import unknown_tool_result
        from nokker.tools.result_fence import fence_mcp_result, mcp_error_text

        if not self._allowed() or not self._open_gate() or not any(t["name"] == name for t in self._catalog or []):
            return unknown_tool_result(name)
        thread_id = _base_thread(ctx.thread_id)

        def failed(exc: BaseException) -> ToolResult:
            return ToolResult(call_id="", name=name, is_error=True,
                              content=self._redact(mcp_error_text(self.name, name, exc)))

        try:
            conn = self._conn(thread_id)
        except _Kernel as e:
            return failed(e)

        async def _call() -> Any:
            return await conn.session.call_tool(name, arguments)

        conn.busy += 1
        try:
            result = self._run(_call(), self.timeout_s)
        except FutureTimeoutError:
            return failed(_Kernel(ERR_TIMEOUT.format(seconds=int(self.timeout_s))))
        except Exception as e:  # noqa: BLE001
            if _closed(e) or conn.dead:
                conn.dead = True  # relancé au prochain appel ; celui-ci n'est pas rejoué
                log.warning("mcp-stdio(%s): processus arrêté pendant %s (fil %s)", self.name, name, thread_id)
                return failed(_Kernel(ERR_DEAD))
            log.warning("mcp-stdio(%s): outil %s en échec (%s)", self.name, name, type(e).__name__)
            return failed(e)
        finally:
            conn.busy -= 1
            conn.last_used = self.clock()
        # Tout ce qui revient du serveur (résultat normal ou erreur, tous les blocs) passe par le caviardage existant :
        # une valeur d'identité ou un secret injecté y devient une poignée [secret:…]. Le noyau refait la même passe
        # sur chaque résultat d'outil (`Invariants.redact_result`) : ici, c'est le filet de la source, hors noyau.
        text = self._redact(text_of(result))
        return ToolResult(call_id="", name=name, content=fence_mcp_result(self.name, name, text),
                          is_error=bool(getattr(result, "is_error", False)))

    def close(self) -> None:
        with self._mu:
            conns = list(self._conns.items())
            self._conns.clear()
        self._stop_all(conns)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=5.0)
            self._loop = None


# Ménage dans le bac à sable : ce qui porte encore la marque du processus (lue dans /proc, un seul grep) reçoit TERM,
# puis KILL s'il en reste. Rien ne porte la marque : rien n'est signalé.
_REAP = ('m="NOKKER_MCP={mark}"; f() {{ grep -lsxz "$m" /proc/[0-9]*/environ 2>/dev/null; }}; '
         'k() {{ for e in $(f); do p="${{e#/proc/}}"; kill "-$1" "${{p%/environ}}" 2>/dev/null; done; }}; '
         '[ -n "$(f)" ] || exit 0; k TERM; sleep 0.5; k KILL; true')


def stdio_sources(servers: list[dict[str, Any]], *, state_dir: Path, resolve: Callable[[str], Any],
                  session_env: SessionEnv, secret: Callable[[str], str] | None, effects: Any = None,
                  on_catalog: Callable[[], None] | None = None, block: dict[str, Any] | None = None,
                  approvals: Approvals | None = None, ask: Callable[[str, dict[str, Any]], bool] | None = None,
                  known: Callable[[], Any] | None = None) -> list[Any]:
    """Une source par déclaration APPROUVÉE (une entrée `approved: false` n'apporte aucun outil et ne lance rien), et
    une par serveur de config.yaml (`origin: config`) en attente de sa carte : celle-là est construite derrière une
    porte (`gate`), n'offre rien et ne lance rien tant que SA déclaration n'est pas approuvée (`approvals`).
    Mêmes enveloppes que les serveurs HTTP : table des effets, puis `read_tools` (`ReadDeclaredSource`)."""
    from nokker.tools.effects import EffectsTable, SecretScrubbingToolSource
    from nokker.tools.mcp_config import ReadDeclaredSource

    block = block or {}
    effects = effects if effects is not None else EffectsTable()
    kw = {k: float(block[k]) for k in ("timeout_s", "start_timeout_s", "idle_ttl_s") if k in block}
    if "max_processes" in block:
        kw["max_processes"] = int(block["max_processes"])
    out: list[Any] = []
    for srv in servers:
        gated = not srv.get("approved") and srv.get("origin") == "config" and approvals is not None
        if not srv.get("approved") and not gated:
            continue
        if gated:
            decl = {k: srv.get(k) for k in ("name", "command", "args", "env")}
            kw_gate: dict[str, Any] = {"gate": (lambda d=decl: approvals.granted(d)), "ask": ask}
        else:
            kw_gate = {}
        from nokker.tools.schema import description_cap

        raw = StdioMcpToolSource(name=srv["name"], command=srv["command"], args=list(srv.get("args") or []),
                                 description_max_chars=description_cap(srv.get("description_max_chars")),
                                 env=dict(srv.get("env") or {}), resolve=resolve, session_env=session_env,
                                 secret=secret, on_catalog=on_catalog, known=known, **kw_gate,
                                 catalog_path=Path(state_dir) / CATALOG_DIR / f"{srv['name']}.json", **kw)
        source: Any = SecretScrubbingToolSource(inner=raw, effects=effects)
        reads = [str(t) for t in srv.get("read_tools") or []]
        if reads:
            source = ReadDeclaredSource(inner=source, read_tools=reads, effects=effects,
                                        confirmed_writes=[str(t) for t in srv.get("confirmed_writes") or []])
        out.append(source)
    return out


def raw_of(source: Any) -> StdioMcpToolSource | None:
    """La source nue sous ses enveloppes (arrêt d'un fil, état), ou None si ce n'en est pas une."""
    seen = 0
    while not isinstance(source, StdioMcpToolSource) and seen < 8:
        source, seen = getattr(source, "inner", None), seen + 1
    return source if isinstance(source, StdioMcpToolSource) else None


def configured_servers(config: Any) -> list[dict[str, Any]]:
    """Serveurs stdio de `config.yaml` (`mcp_stdio.servers`), marqués `origin: config`. Par DÉFAUT ils ne sont pas
    approuvés : celui qui écrit la config n'est pas forcément la personne devant l'écran, et lancer un programme pour
    son compte demande SON accord — une carte forte au premier lancement, mémorisée par empreinte (`Approvals`).
    `mcp_stdio.trust_config: true` (seulement `true`) : l'ancien comportement, config.yaml vaut approbation — pour un
    hôte dont l'opérateur et l'utilisateur sont la même personne. Une entrée invalide est sautée, motif au journal."""
    block = getattr(config, "mcp_stdio", None) or {}
    trusted = block.get("trust_config") is True
    out: list[dict[str, Any]] = []
    for raw in (block.get("servers") or [])[:MAX_SERVERS]:
        try:
            out.append({**check_stdio_entry(raw if isinstance(raw, dict) else {}), "approved": trusted,
                        "origin": "config"})
        except McpStdioError as e:
            log.warning("serveur MCP stdio de la config ignoré : %s", e)
    return out


__all__ = ["APPROVALS_FILE", "Approvals", "CATALOG_DIR", "FILE_NAME", "McpStdioError", "SessionEnv",
           "StdioMcpToolSource", "check_env", "check_stdio_entry", "configured_servers", "fingerprint", "identity_name",
           "load_stdio_servers", "raw_of", "same_declaration", "secret_names", "stdio_enabled", "stdio_refusal", "stdio_sources", "write_stdio_servers"]
