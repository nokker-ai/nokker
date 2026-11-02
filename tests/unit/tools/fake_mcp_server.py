"""Faux serveur MCP streamable-HTTP pour les tests du client du noyau (docs/MCP-NEXT.md), aux DEUX révisions :

- `era="legacy"` : révision 2025-11-25 seule — poignée de main `initialize`, `Mcp-Session-Id`, et 400 (erreur JSON-RPC
  -32600) sur toute requête sans session, `server/discover` compris (c'est ce que voit un client 2026-07-28) ;
- `era="modern"` : révision 2026-07-28 seule — pas de session : chaque requête porte sa version et ses capacités dans
  `_meta`, contrôlées (version inconnue → -32022 avec `supported`) ; `initialize` refusé en 400 ; `server/discover`,
  MRTR (`input_required` + `requestState`), extension Tasks (`resultType: "task"`, `tasks/get`, `tasks/cancel`),
  `ttlMs`/`cacheScope` sur les listes.

Les deux servent une app MCP (`clock` → `ui://clock/app.html`, outil `clock_refresh` réservé à l'app) et une ressource
`ui://` embarquée (`inline_widget`, forme MCP-UI). Réponses JSON simples (pas de flux SSE), GET en 405. Chaque requête
reçue est notée dans `requests` (méthode, en-têtes, params) pour les assertions."""
from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self

MODERN = "2026-07-28"
LEGACY = "2025-11-25"
APP_MIME = "text/html;profile=mcp-app"
CLOCK_HTML = "<!doctype html><html><body><p id=t>12:00</p><script>/* app */</script></body></html>"
INLINE_HTML = "<!doctype html><html><body><button>ok</button></body></html>"

TOOLS = [
    {"name": "echo", "description": "Renvoie le texte.", "inputSchema": {"type": "object",
                                                                       "properties": {"text": {"type": "string"}}}},
    {"name": "clock", "description": "Heure courante.", "inputSchema": {"type": "object", "properties": {}},
     "_meta": {"ui": {"resourceUri": "ui://clock/app.html", "visibility": ["model", "app"]}}},
    {"name": "clock_refresh", "description": "Rafraîchit l'horloge.", "inputSchema": {"type": "object",
                                                                                     "properties": {}},
     "_meta": {"ui": {"resourceUri": "ui://clock/app.html", "visibility": ["app"]}}},
    {"name": "inline_widget", "description": "Widget embarqué.", "inputSchema": {"type": "object", "properties": {}}},
]
MODERN_TOOLS = [
    {"name": "slow_sum", "description": "Somme en plusieurs allers-retours.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "ask_name", "description": "Demande un nom à l'utilisateur.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "long_job", "description": "Travail long.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "stuck_job", "description": "Travail qui ne finit pas.",
     "inputSchema": {"type": "object", "properties": {}}},
]
CLOCK_RESOURCE_META = {"ui": {"csp": {"connectDomains": ["https://api.example.com", "'unsafe-eval'"],
                                      "resourceDomains": ["https://cdn.example.com", "https://x; script-src *"]},
                              "permissions": {"camera": {}}, "prefersBorder": True}}


def _text(t: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": t}]}


class FakeMcpServer:
    def __init__(self, era: str) -> None:
        assert era in ("legacy", "modern")
        self.era = era
        self.requests: list[dict[str, Any]] = []
        self.sessions: set[str] = set()
        self.task_polls: dict[str, int] = {}
        self.cancelled: list[str] = []
        self.list_calls = 0
        self.ttl_ms = 120_000
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/mcp"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def methods(self) -> list[str]:
        return [r["method"] for r in self.requests]

    # ─────────────────────────── logique MCP ───────────────────────────

    def _tools(self) -> list[dict[str, Any]]:
        return TOOLS + (MODERN_TOOLS if self.era == "modern" else [])

    def _complete(self, result: dict[str, Any]) -> dict[str, Any]:
        if self.era == "modern":
            return {"resultType": "complete", **result}
        return result

    def _call_tool(self, params: dict[str, Any], caps: dict[str, Any]) -> dict[str, Any]:
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "echo":
            return self._complete(_text(f"echo: {args.get('text', '')}"))
        if name == "clock":
            return self._complete({**_text("Il est 12:00."), "structuredContent": {"time": "12:00"}})
        if name == "clock_refresh":
            return self._complete(_text("Il est 12:01."))
        if name == "inline_widget":
            return self._complete({"content": [
                {"type": "text", "text": "Widget prêt."},
                {"type": "resource", "resource": {"uri": "ui://inline/app.html", "mimeType": APP_MIME,
                                                  "text": INLINE_HTML}}]})
        if name == "slow_sum":  # MRTR état seul : deux tours avant le résultat
            state = params.get("requestState")
            if state is None:
                return {"resultType": "input_required", "requestState": "r1"}
            if state == "r1":
                return {"resultType": "input_required", "requestState": "r2"}
            return self._complete(_text("somme : 3"))
        if name == "ask_name":
            if params.get("inputResponses"):
                return self._complete(_text("bonjour"))
            return {"resultType": "input_required", "inputRequests": {"q": {
                "method": "elicitation/create",
                "params": {"mode": "form", "message": "Ton nom ?",
                           "requestedSchema": {"type": "object", "properties": {"n": {"type": "string"}}}}}}}
        if name in ("long_job", "stuck_job"):
            if "io.modelcontextprotocol/tasks" not in ((caps or {}).get("extensions") or {}):
                return self._complete(_text("fait sans tâche"))
            tid = f"{name}-{uuid.uuid4().hex[:6]}"
            self.task_polls[tid] = 0
            return {"resultType": "task", "taskId": tid, "status": "working", "createdAt": "2026-11-01T00:00:00Z",
                    "lastUpdatedAt": "2026-11-01T00:00:00Z", "ttlMs": 60000, "pollIntervalMs": 50}
        raise KeyError(name)

    def _task_get(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = params["taskId"]
        self.task_polls[tid] += 1
        base = {"resultType": "complete", "taskId": tid, "createdAt": "2026-11-01T00:00:00Z",
                "lastUpdatedAt": "2026-11-01T00:00:01Z", "ttlMs": 60000, "pollIntervalMs": 50}
        if tid.startswith("long_job") and self.task_polls[tid] >= 2:
            return {**base, "status": "completed", "result": _text("travail fini")}
        return {**base, "status": "working", "statusMessage": "en cours"}

    def _read_resource(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params["uri"]
        if uri != "ui://clock/app.html":
            raise KeyError(uri)
        out = {"contents": [{"uri": uri, "mimeType": APP_MIME, "text": CLOCK_HTML, "_meta": CLOCK_RESOURCE_META}]}
        if self.era == "modern":
            out.update(ttlMs=0, cacheScope="private")
        return self._complete(out)

    def _dispatch(self, method: str, params: dict[str, Any], caps: dict[str, Any]) -> dict[str, Any]:
        if method == "tools/list":
            self.list_calls += 1
            out: dict[str, Any] = {"tools": self._tools()}
            if self.era == "modern":
                out.update(ttlMs=self.ttl_ms, cacheScope="private")
            return self._complete(out)
        if method == "tools/call":
            return self._call_tool(params, caps)
        if method == "resources/read":
            return self._read_resource(params)
        if method == "tasks/get" and self.era == "modern":
            return self._task_get(params)
        if method == "tasks/cancel" and self.era == "modern":
            self.cancelled.append(params["taskId"])
            return {"resultType": "complete"}
        raise KeyError(method)

    # ─────────────────────────── HTTP ───────────────────────────

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, code: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._send(405)

            def do_DELETE(self) -> None:
                self._send(200 if srv.era == "legacy" else 405)

            def do_POST(self) -> None:
                msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
                srv.requests.append({"method": method, "params": params,
                                     "headers": {k.lower(): v for k, v in self.headers.items()}})

                def err(code: int, message: str, data: Any = None, status: int = 200) -> None:
                    e: dict[str, Any] = {"code": code, "message": message}
                    if data is not None:
                        e["data"] = data
                    self._send(status, {"jsonrpc": "2.0", "id": rid, "error": e})

                if rid is None:  # notification
                    self._send(202)
                    return
                if srv.era == "legacy":
                    if method == "initialize":
                        sid = uuid.uuid4().hex
                        srv.sessions.add(sid)
                        self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {
                            "protocolVersion": LEGACY, "serverInfo": {"name": "fake-legacy", "version": "1"},
                            "capabilities": {"tools": {"listChanged": True}, "resources": {},
                                             "extensions": {"io.modelcontextprotocol/ui": {}}}}},
                                   {"Mcp-Session-Id": sid})
                        return
                    if self.headers.get("Mcp-Session-Id") not in srv.sessions:
                        err(-32600, "Bad Request: No valid session ID provided", status=400)
                        return
                    caps = {}
                else:
                    if method == "initialize":
                        err(-32601, f"initialize n'existe plus : versions servies {MODERN}", status=400)
                        return
                    meta = params.get("_meta") or {}
                    version = meta.get("io.modelcontextprotocol/protocolVersion")
                    if version != MODERN or self.headers.get("MCP-Protocol-Version") != MODERN:
                        err(-32022, "Unsupported protocol version", {"supported": [MODERN], "requested": version})
                        return
                    caps = meta.get("io.modelcontextprotocol/clientCapabilities") or {}
                    if method == "server/discover":
                        self._send(200, {"jsonrpc": "2.0", "id": rid, "result": {
                            "resultType": "complete", "supportedVersions": [MODERN], "ttlMs": 0,
                            "cacheScope": "private",
                            "capabilities": {"tools": {}, "resources": {},
                                             "extensions": {"io.modelcontextprotocol/tasks": {},
                                                            "io.modelcontextprotocol/ui": {}}}}})
                        return
                try:
                    result = srv._dispatch(method, params, caps)
                except KeyError as e:
                    err(-32601, f"inconnu : {e}")
                    return
                self._send(200, {"jsonrpc": "2.0", "id": rid, "result": result})

        return H


__all__ = ["CLOCK_HTML", "INLINE_HTML", "LEGACY", "MODERN", "FakeMcpServer"]
