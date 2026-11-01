"""`python -m nokker mcp serve` : Nokker exposé comme SERVEUR MCP (stdio), pour un client MCP tiers (éditeur, agent)
— docs/MCP-SERVER.md.

Le serveur ne contient AUCUNE logique du noyau : chaque outil est une requête à l'API `/v1` d'un noyau, avec le jeton
du compte (`Authorization: Bearer`). Mêmes portes, même locataire, mêmes refus que l'API, par construction : un fil
d'un autre compte répond 403/404 ici comme là-bas, une action à effet demandée dans un message pose sa carte comme
depuis le web. Rien n'est ajouté à ce que voit le modèle de Nokker.

Jeton : variable d'environnement (`NOKKER_TOKEN` par défaut, `--token-env` pour une autre) ou fichier (`--token-file`,
lisible par l'utilisateur seulement) ; jamais en argument de commande (visible dans la liste des processus).
"""
from __future__ import annotations

import argparse
import logging
import os
import stat
import sys
import uuid
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

logger = logging.getLogger("nokker.mcp_server")

DEFAULT_URL = "http://127.0.0.1:8742"
DEFAULT_TOKEN_ENV = "NOKKER_TOKEN"
SEND_TIMEOUT_S = 600.0  # un tour réel peut durer plusieurs minutes (outils, cartes)
READ_TIMEOUT_S = 30.0
MAX_MESSAGE = 20_000

INSTRUCTIONS = ("Nokker : envoyer un message à un fil et lire la réponse, lister et lire ses fils, chercher dans la "
                "mémoire du compte. Une action à effet demandée dans un message attend la décision de l'utilisateur "
                "sur sa carte d'approbation, comme depuis l'application.")


def _error(resp: httpx.Response) -> ToolError:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    err = body.get("error") if isinstance(body, dict) else None
    msg = err.get("message") if isinstance(err, dict) else (err if isinstance(err, str) else "")
    if resp.status_code == 401:
        return ToolError("jeton refusé par Nokker (401)")
    if resp.status_code in (403, 404):
        return ToolError(f"fil ou ressource inconnu ou interdit ({resp.status_code})")
    return ToolError(f"Nokker a répondu {resp.status_code}" + (f" : {msg}" if msg else ""))


class NokkerApi:
    """Client de l'API `/v1` d'un noyau, au nom d'UN compte (son jeton)."""

    def __init__(self, base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not token:
            raise ValueError("jeton du compte absent")
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._transport = transport

    async def request(self, method: str, path: str, *, json: Any = None, headers: dict[str, str] | None = None,
                      timeout: float = READ_TIMEOUT_S) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, headers=self._headers, timeout=timeout,
                                     transport=self._transport, follow_redirects=False) as client:
            try:
                resp = await client.request(method, path, json=json, headers=headers)
            except httpx.HTTPError as e:
                raise ToolError(f"Nokker injoignable : {type(e).__name__}") from None
        if resp.status_code >= 400:
            raise _error(resp)
        try:
            return resp.json()
        except ValueError:
            raise ToolError("réponse illisible de Nokker") from None


def _thread_view(t: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "title", "owner", "members", "working", "updated_at", "created_at", "last_turn_at")
    return {k: t[k] for k in keep if k in t}


def build_server(api: NokkerApi) -> MCPServer:
    """Le serveur MCP et ses quatre outils, au nom du compte de `api`."""
    server = MCPServer("nokker", instructions=INSTRUCTIONS)

    @server.tool(name="nokker_threads_list", description="Liste les fils de conversation du compte.")
    async def threads_list() -> dict[str, Any]:
        data = await api.request("GET", "/v1/threads")
        return {"threads": [_thread_view(t) for t in data if isinstance(t, dict)]}

    @server.tool(name="nokker_thread_read",
                 description="Lit les derniers messages d'un fil du compte (rôle, contenu, index du tour).")
    async def thread_read(thread_id: str, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        data = await api.request("GET", f"/v1/threads/{quote(thread_id, safe='')}?limit={limit}")
        msgs = [{k: m.get(k) for k in ("index", "role", "content") if k in m}
                for m in (data.get("messages") or []) if isinstance(m, dict)]
        return {"thread": _thread_view(data), "messages": msgs}

    @server.tool(name="nokker_thread_send",
                 description="Envoie un message à un fil (nouveau fil si thread_id est absent) et rend la réponse de "
                             "Nokker. Une action à effet attend la décision de l'utilisateur sur sa carte.")
    async def thread_send(message: str, thread_id: str | None = None) -> dict[str, Any]:
        if not message.strip():
            raise ToolError("message vide")
        if len(message) > MAX_MESSAGE:
            raise ToolError(f"message au-delà de {MAX_MESSAGE} caractères")
        tid = thread_id or f"mcp-{uuid.uuid4().hex[:12]}"
        data = await api.request("POST", "/v1/chat/completions", headers={"X-Session-Id": tid},
                                 json={"model": "nokker", "stream": False,
                                       "messages": [{"role": "user", "content": message}]},
                                 timeout=SEND_TIMEOUT_S)
        try:
            reply = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ToolError("réponse sans message") from None
        return {"thread_id": tid, "reply": reply}

    @server.tool(name="nokker_memory_search", description="Cherche dans la mémoire longue du compte.")
    async def memory_search(query: str, k: int = 5) -> dict[str, Any]:
        if not query.strip():
            raise ToolError("requête vide")
        return await api.request("POST", "/v1/memory/search", json={"query": query, "k": max(1, min(int(k), 20))})

    return server


def read_token(token_env: str, token_file: str | None) -> str:
    if token_file:
        st = os.stat(token_file)
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SystemExit(f"{token_file} : lisible par d'autres que l'utilisateur (chmod 600)")
        with open(token_file, encoding="utf-8") as f:
            return f.read().strip()
    return os.environ.get(token_env, "").strip()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nokker mcp", description="Nokker exposé comme serveur MCP")
    sub = p.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="serveur MCP sur stdio (outils : fils, messages, mémoire du compte)")
    serve.add_argument("--url", default=os.environ.get("NOKKER_URL", DEFAULT_URL), help="base de l'API du noyau")
    serve.add_argument("--token-env", default=DEFAULT_TOKEN_ENV, help="variable qui porte le jeton du compte")
    serve.add_argument("--token-file", default=None, help="fichier (0600) qui porte le jeton du compte")
    args = p.parse_args(argv)
    # stdout = le protocole : tout journal sur stderr.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(name)s %(levelname)s %(message)s")
    token = read_token(args.token_env, args.token_file)
    if not token:
        print(f"jeton du compte absent : {args.token_env} ou --token-file", file=sys.stderr)
        return 2
    build_server(NokkerApi(args.url, token)).run("stdio")
    return 0


__all__ = ["NokkerApi", "build_server", "main", "read_token"]
