"""Client MCP à la révision 2026-07-28 et MCP Apps (docs/MCP-NEXT.md), derrière `mcp_modern` / `mcp_apps` (éteints).

Ce module ne parle qu'au SDK `mcp` (pin 2.0.0, qui porte déjà la révision 2026-07-28) ; `nokker/tools/mcp.py` s'en sert
quand un des deux drapeaux est posé, et garde son chemin d'avant, octet pour octet, quand les deux sont éteints.

- **Négociation** (spec 2026-07-28, basic/lifecycle « Backward Compatibility ») : `Client(mode="auto")` sonde
  `server/discover` à la version la plus récente, réessaie une fois à une version commune sur `-32022`, et retombe sur
  la poignée de main `initialize` (serveur 2025-11-25 ou plus ancien) sur toute autre erreur. Drapeau `mcp_modern`
  éteint mais `mcp_apps` posé : `mode="legacy"` (poignée de main seule, comme avant).
- **MRTR** (SEP-2322) : `Client.call_tool` relance l'appel avec `requestState` et les réponses tant que le serveur rend
  `input_required` (10 tours au plus). Le noyau ne déclare NI élicitation NI échantillonnage : un serveur qui en demande
  reçoit un refus, l'appel finit en erreur (rien n'est inventé à la place de l'utilisateur, aucun modèle payant appelé
  pour un serveur tiers).
- **Tasks** (extension `io.modelcontextprotocol/tasks`, schéma ext-tasks 2026-07-28) : un `tools/call` peut rendre
  `resultType: "task"` ; on interroge `tasks/get` au rythme `pollIntervalMs` jusqu'à un état terminal, borné par
  `task_wait_s`. Au-delà, l'appel rend une note courte (la tâche continue chez le serveur).
- **MCP Apps** (extension `io.modelcontextprotocol/ui`, spec ext-apps 2026-01-26) : `app_payload` construit, à partir
  d'un outil qui déclare `_meta.ui.resourceUri` (ou d'un résultat qui embarque une ressource `ui://`), ce que le flux
  d'événements transmet au client — jamais au modèle.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Literal

import anyio
import mcp_types as types
from mcp.client import ClaimContext, ClientExtension, ResultClaim
from pydantic import Field

logger = logging.getLogger("nokker.tools.mcp_next")

TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
UI_EXTENSION = "io.modelcontextprotocol/ui"
APP_MIME_TYPE = "text/html;profile=mcp-app"

# Bornes d'une tâche longue : on n'attend pas plus que `task_wait_s` (sous le délai de `_SyncMcp.run`) ; l'intervalle
# suggéré par le serveur est borné pour ne pas marteler ni s'endormir.
DEFAULT_TASK_WAIT_S = 20.0
POLL_MIN_S, POLL_MAX_S, POLL_DEFAULT_S = 0.05, 5.0, 0.5

# Taille max du document d'une app transmis au client (au-delà : pas d'app, la note courte non plus).
APP_HTML_MAX_BYTES = 512 * 1024

# ─── Textes vus par le MODÈLE (tous nouveaux, gel du format : à recopier tels quels dans le rapport) ───
# Remplace, dans le résultat d'outil, ce que serait le document d'une app : jamais son contenu.
APP_SUMMARY = "[Interface interactive de l'outil {tool} affichée à l'utilisateur dans le fil.]"
TASK_PENDING = ("[Tâche longue du serveur MCP toujours en cours (état : {status}) : résultat pas encore disponible "
                "dans ce tour.]")
TASK_FAILED = "Tâche longue du serveur MCP en échec : {message}"
TASK_CANCELLED = "Tâche longue du serveur MCP annulée."
TASK_INPUT = "La tâche longue du serveur MCP attend une saisie que le noyau ne fournit pas : tâche annulée."

# Messages qu'une app peut envoyer à l'hôte (postMessage, spec ext-apps §« View → Host »). Tout autre message est refusé
# par le client (réponse JSON-RPC -32601). `tools/call` passe par le noyau (POST /v1/apps/{app_id}/tools/call) ;
# `ui/message` et `ui/update-model-context` sont EXCLUS : ils écriraient dans la conversation ou la fenêtre du modèle.
ALLOWED_APP_MESSAGES = (
    "ui/initialize",
    "ui/notifications/initialized",
    "ui/notifications/size-changed",
    "ui/request-display-mode",
    "ui/open-link",
    "tools/call",
    "notifications/message",
    "ping",
)
# Bac à sable de l'iframe (décision v1 : le web l'affiche en `srcdoc` avec `sandbox="allow-scripts"` SEUL, origine
# opaque). `allow-same-origin` n'est envoyé que si un domaine dédié est posé (`mcp_apps_origin`, distinct de l'origine
# de l'app, contrôlé au démarrage : `check_apps_origin`) : le proxy de la spec ext-apps vit alors sur cette origine.
APP_SANDBOX = "allow-scripts"
APP_SANDBOX_DEDICATED = "allow-scripts allow-same-origin"


def _origin(url: str) -> tuple[str, str, int] | None:
    from urllib.parse import urlsplit

    try:
        u = urlsplit(str(url or "").strip())
        port = u.port or (443 if u.scheme == "https" else 80 if u.scheme == "http" else 0)
    except ValueError:
        return None
    if not u.scheme or not u.hostname:
        return None
    return u.scheme.lower(), u.hostname.lower().rstrip("."), port


def check_apps_origin(apps_origin: str, app_origins: list[str]) -> str:
    """`mcp_apps_origin` validé au démarrage (ValueError sinon) : vide = aucun domaine dédié ; sinon une origine
    `https://hôte[:port]` dont l'hôte n'est NI celui de l'app (`app_url`, `webauthn_origin`, `front_url`…) NI un de
    ses sous-domaines ou parents : l'app ne doit jamais partager son origine avec un document de serveur MCP."""
    value = str(apps_origin or "").strip().rstrip("/")
    if not value:
        return ""
    o = _origin(value)
    if o is None or o[0] != "https" or value.count("/") != 2:
        raise ValueError(f"mcp_apps_origin : origine https://hôte[:port] attendue, pas {value!r}")
    for other in app_origins:
        a = _origin(other)
        if a is None:
            continue
        if o == a or o[1] == a[1]:
            raise ValueError(f"mcp_apps_origin : {value} est l'origine de l'app ({other}) ; un domaine dédié est exigé")
        if o[1].endswith("." + a[1]) or a[1].endswith("." + o[1]):
            raise ValueError(f"mcp_apps_origin : {value} partage le domaine de l'app ({other}) ; un domaine dédié, "
                             "distinct, est exigé")
    return value

# CSP par défaut quand la ressource n'en déclare pas (spec ext-apps, « MUST »).
DEFAULT_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data:; media-src 'self' data:; connect-src 'none'; object-src 'none'")
# Une source de CSP déclarée par un serveur tiers : https://hôte[:port] ou https://*.hôte, rien d'autre (pas de
# guillemets, de point-virgule, d'espace ni de mot-clé : sinon le serveur réécrirait la politique).
_CSP_SOURCE = re.compile(r"^https://(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:\d{1,5})?$")


# ─────────────────────────────── Tasks ───────────────────────────────


class CreateTaskResult(types.Result):
    """`CreateTaskResult` (ext-tasks 2026-07-28) : `Result & Task` à plat, `resultType: "task"`."""

    result_type: Literal["task"] = "task"
    task_id: str
    status: str
    status_message: str | None = None
    ttl_ms: int | None = None
    poll_interval_ms: int | None = None


class _TaskIdParams(types.RequestParams):
    task_id: str


class _UpdateTaskParams(_TaskIdParams):
    input_responses: dict[str, Any] = Field(default_factory=dict)


class _GetTaskRequest(types.Request[_TaskIdParams, Literal["tasks/get"]]):
    method: Literal["tasks/get"] = "tasks/get"
    name_param = "taskId"


class _CancelTaskRequest(types.Request[_TaskIdParams, Literal["tasks/cancel"]]):
    method: Literal["tasks/cancel"] = "tasks/cancel"
    name_param = "taskId"


class _DetailedTask(types.Result):
    """Réponse de `tasks/get` : la variante de `DetailedTask` de l'état courant."""

    result_type: str | None = None
    task_id: str = ""
    status: str = "working"
    status_message: str | None = None
    poll_interval_ms: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    input_requests: dict[str, Any] | None = None


def _poll_s(ms: int | None) -> float:
    if ms is None:
        return POLL_DEFAULT_S
    return min(POLL_MAX_S, max(POLL_MIN_S, ms / 1000.0))


def _text_result(text: str, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)


class TasksExtension(ClientExtension):
    """Côté client de l'extension Tasks : annonce la capacité et résout un `resultType: "task"` par `tasks/get`."""

    identifier = TASKS_EXTENSION

    def __init__(self, wait_s: float = DEFAULT_TASK_WAIT_S, clock: Any = time.monotonic) -> None:
        self.wait_s = wait_s
        self.clock = clock

    def claims(self):
        return (ResultClaim(result_type="task", model=CreateTaskResult, resolve=self._resolve),)

    async def _resolve(self, created: CreateTaskResult, ctx: ClaimContext) -> types.CallToolResult:
        session = ctx.session
        deadline = self.clock() + self.wait_s
        task_id, status, poll = created.task_id, created.status, _poll_s(created.poll_interval_ms)
        while True:
            if status not in ("completed", "failed", "cancelled", "input_required"):
                if self.clock() + poll > deadline:
                    return _text_result(TASK_PENDING.format(status=status), False)
                await anyio.sleep(poll)
            task = await session.send_request(_GetTaskRequest(params=_TaskIdParams(task_id=task_id)), _DetailedTask)
            status, poll = task.status, _poll_s(task.poll_interval_ms)
            if status == "completed":
                return types.CallToolResult.model_validate(task.result or {"content": []})
            if status == "failed":
                message = str((task.error or {}).get("message") or task.status_message or "erreur inconnue")
                return _text_result(TASK_FAILED.format(message=message[:300]), True)
            if status == "cancelled":
                return _text_result(TASK_CANCELLED, True)
            if status == "input_required":
                # Ni élicitation ni échantillonnage déclarés : on n'invente pas de réponse, on libère la tâche.
                try:
                    await session.send_request(_CancelTaskRequest(params=_TaskIdParams(task_id=task_id)),
                                               types.EmptyResult)
                except Exception:
                    logger.debug("tasks/cancel en échec", exc_info=True)
                return _text_result(TASK_INPUT, True)


# ─────────────────────────────── MCP Apps ───────────────────────────────


def tool_ui(tool: Any) -> dict[str, Any] | None:
    """`_meta.ui` d'un outil (`resourceUri`, `visibility`) ; accepte l'ancienne clé `_meta["ui/resourceUri"]`."""
    meta = getattr(tool, "meta", None) or {}
    ui = meta.get("ui") if isinstance(meta.get("ui"), dict) else {}
    uri = ui.get("resourceUri") or meta.get("ui/resourceUri")
    if not isinstance(uri, str) or not uri.startswith("ui://"):
        return None
    vis = ui.get("visibility")
    visibility = [v for v in vis if v in ("model", "app")] if isinstance(vis, list) else ["model", "app"]
    return {"resource_uri": uri, "visibility": visibility or ["model", "app"]}


def csp_sources(raw: Any) -> list[str]:
    """Sources CSP déclarées par le serveur, filtrées (voir `_CSP_SOURCE`) ; les autres sont ignorées."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, str) and _CSP_SOURCE.match(item.strip().lower()):
            out.append(item.strip().lower())
    return out


def build_csp(ui_meta: dict[str, Any] | None) -> tuple[str, dict[str, list[str]]]:
    """CSP que le client DOIT appliquer au document de l'app, et les domaines retenus. Sans `_meta.ui.csp` : la CSP
    par défaut de la spec. Avec : construction de la spec (connect/resource/frame/baseUri), domaines filtrés."""
    raw = (ui_meta or {}).get("csp")
    if not isinstance(raw, dict):
        return DEFAULT_CSP, {}
    domains = {k: csp_sources(raw.get(k)) for k in ("connectDomains", "resourceDomains", "frameDomains",
                                                    "baseUriDomains")}
    res = " ".join(["'self'", *domains["resourceDomains"]])
    parts = [
        "default-src 'none'",
        f"script-src {res} 'unsafe-inline'",
        f"style-src {res} 'unsafe-inline'",
        f"img-src {res} data:",
        f"font-src {res}",
        f"media-src {res} data:",
        "connect-src " + (" ".join(["'self'", *domains["connectDomains"]])),
        "frame-src " + (" ".join(domains["frameDomains"]) if domains["frameDomains"] else "'none'"),
        "base-uri " + (" ".join(domains["baseUriDomains"]) if domains["baseUriDomains"] else "'self'"),
        "object-src 'none'",
    ]
    kept = {k: v for k, v in domains.items() if v}
    return "; ".join(parts), kept


def embedded_ui(result: Any) -> Any | None:
    """Ressource `ui://` embarquée dans un `CallToolResult` (forme MCP-UI), ou None."""
    for block in getattr(result, "content", None) or []:
        res = getattr(block, "resource", None)
        if isinstance(block, types.EmbeddedResource) and str(getattr(res, "uri", "")).startswith("ui://"):
            return res
    return None


def app_payload(*, server: str, tool: str, resource: Any, visibility: list[str], arguments: dict[str, Any],
                apps_origin: str = "",
                result: Any) -> dict[str, Any] | None:
    """Ce que le noyau transmet au client pour une app (contrat : docs/MCP-NEXT.md §3). None si la ressource n'est
    pas une app servable : MIME autre que `text/html;profile=mcp-app`, pas de texte, ou document trop gros."""
    mime = (getattr(resource, "mime_type", None) or "").replace(" ", "").lower()
    html = getattr(resource, "text", None)
    if mime != APP_MIME_TYPE or not isinstance(html, str) or not html:
        return None
    if len(html.encode("utf-8")) > APP_HTML_MAX_BYTES:
        logger.warning("mcp app %s/%s : document de %d octets, au-delà de %d, non transmis", server, tool,
                       len(html.encode("utf-8")), APP_HTML_MAX_BYTES)
        return None
    meta = getattr(resource, "meta", None) or {}
    ui_meta = meta.get("ui") if isinstance(meta.get("ui"), dict) else {}
    csp, domains = build_csp(ui_meta)
    perms = ui_meta.get("permissions") if isinstance(ui_meta.get("permissions"), dict) else {}
    requested = sorted(k for k in ("camera", "microphone", "geolocation", "clipboardWrite") if k in perms)
    tool_result = result.model_dump(by_alias=True, mode="json", exclude_none=True) if result is not None else {}
    # Le document est déjà dans `html` : pas une seconde fois dans le résultat transmis à l'app.
    tool_result["content"] = [b for b in tool_result.get("content") or []
                              if not (b.get("type") == "resource"
                                      and str((b.get("resource") or {}).get("uri", "")).startswith("ui://"))]
    return {
        "server": server,
        "tool": tool,
        "resource_uri": str(getattr(resource, "uri", "")),
        "mime_type": APP_MIME_TYPE,
        "html": html,
        "csp": csp,
        "csp_domains": domains,
        "sandbox": APP_SANDBOX_DEDICATED if apps_origin else APP_SANDBOX,
        "sandbox_origin": apps_origin or None,
        "permissions_requested": requested,
        "prefers_border": ui_meta.get("prefersBorder") if isinstance(ui_meta.get("prefersBorder"), bool) else None,
        "visibility": visibility,
        "allowed_messages": list(ALLOWED_APP_MESSAGES),
        "tool_input": arguments,
        "tool_result": tool_result,
    }


__all__ = [
    "ALLOWED_APP_MESSAGES",
    "APP_MIME_TYPE",
    "APP_SANDBOX",
    "APP_SANDBOX_DEDICATED",
    "APP_SUMMARY",
    "DEFAULT_CSP",
    "TASKS_EXTENSION",
    "UI_EXTENSION",
    "TasksExtension",
    "app_payload",
    "check_apps_origin",
    "build_csp",
    "embedded_ui",
    "tool_ui",
]
