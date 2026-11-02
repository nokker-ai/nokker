"""Routes de l'agent de paiement (bibliothèque standard). Réseau interne seulement, sauf `/stripe/webhook`.

Appels du noyau (en-tête `Authorization: Bearer <PAYAGENT_KERNEL_TOKEN>`) :
    GET    /v1/methods/{us}                       → {method: {...} | null}
    PUT    /v1/methods/{us} {payment_method, proof}  → {method}   (preuve WebAuthn du compte, défi `method_challenge`)
    DELETE /v1/methods/{us}                       → {removed}     (révoque aussi les jetons vivants)
    POST   /v1/tokens {account, envelope}         → jeton        (crée UN SPT pour un mandat signé, idempotent)
    GET    /v1/tokens/{md}?account=us_…           → jeton
    POST   /v1/tokens/{md}/revoke {account}       → jeton
Stripe :
    POST   /stripe/webhook                        (signature `Stripe-Signature` vérifiée)
Erreurs : {"error": {"code", "message"}}.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from payagent.service import AgentError, PayAgent

logger = logging.getLogger("payagent.http")
MAX_BODY = 64 * 1024
_ACCOUNT = re.compile(r"^us_[A-Za-z0-9_-]{1,64}$")
_MANDATE = re.compile(r"^md_[0-9a-f]{16}$")


def route(agent: PayAgent, method: str, raw_path: str, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
    """Le cœur des routes, sans socket (testé directement)."""
    parts = urlsplit(raw_path)
    path, query = parts.path.rstrip("/"), parse_qs(parts.query)
    try:
        if path == "/healthz" and method == "GET":
            return 200, {"ok": True}
        if path == "/stripe/webhook" and method == "POST":
            return 200, agent.on_webhook(body, headers.get("Stripe-Signature") or "")
        auth = headers.get("Authorization") or ""
        if not agent.cfg.kernel_token or not hmac.compare_digest(auth, f"Bearer {agent.cfg.kernel_token}"):
            return 401, {"error": {"code": "unauthorized", "message": "jeton du noyau attendu"}}
        data = json.loads(body or b"{}") if body else {}
        if not isinstance(data, dict):
            raise AgentError(400, "invalid", "objet JSON attendu")
        m = re.fullmatch(r"/v1/methods/([^/]+)", path)
        if m:
            account = _account(m.group(1))
            if method == "GET":
                return 200, {"method": agent.method(account)}
            if method == "PUT":
                return 200, {"method": agent.register_method(account, data.get("payment_method"), data.get("proof"))}
            if method == "DELETE":
                return 200, {"removed": agent.remove_method(account)}
        if path == "/v1/tokens" and method == "POST":
            ref = data.get("kernel_ref")
            return 200, agent.issue(_account(data.get("account")), data.get("envelope"),
                                    ref if isinstance(ref, str) else "")
        m = re.fullmatch(r"/v1/tokens/([^/]+)(/revoke)?", path)
        if m and _MANDATE.match(m.group(1)):
            if method == "GET" and not m.group(2):
                return 200, agent.token(_account((query.get("account") or [""])[0]), m.group(1))
            if method == "POST" and m.group(2):
                return 200, agent.revoke(_account(data.get("account")), m.group(1))
        return 404, {"error": {"code": "not_found", "message": "route inconnue"}}
    except AgentError as e:
        return e.status, {"error": {"code": e.code, "message": e.message}}
    except ValueError:
        return 400, {"error": {"code": "invalid", "message": "JSON illisible"}}


def _account(value: Any) -> str:
    if not isinstance(value, str) or not _ACCOUNT.match(value):
        raise AgentError(400, "invalid", "compte us_… attendu")
    return value


def serve(agent: PayAgent) -> None:  # pragma: no cover - boucle réseau
    class Handler(BaseHTTPRequestHandler):
        server_version = "nokker-pay-agent"

        def _go(self) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                status, out = 413, {"error": {"code": "too_large", "message": "corps trop grand"}}
            else:
                status, out = route(agent, self.command, self.path, self.headers, self.rfile.read(n) if n else b"")
            raw = json.dumps(out, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PUT = do_DELETE = _go

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.info("%s %s", self.command, self.path.split("?", 1)[0])

    ThreadingHTTPServer((agent.cfg.host, agent.cfg.port), Handler).serve_forever()
