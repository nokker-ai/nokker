"""Routes de l'agent de paiement (bibliothèque standard). Réseau interne seulement, sauf `/stripe/webhook`.

Appels du noyau (en-tête `Authorization: Bearer <PAYAGENT_KERNEL_TOKEN>`) :
    GET    /v1/methods/{us}                       → {method: {...} | null}
    PUT    /v1/methods/{us} {payment_method, proof}  → {method}   (preuve WebAuthn du compte, défi `method_challenge`)
    DELETE /v1/methods/{us}                       → {removed}     (révoque aussi les jetons vivants)
    GET    /v1/links/{us}                         → {link: {state, required_by, profile_ref} | null}       [wise]
    POST   /v1/links/{us} {person}                → {link}   (ouvre le profil Wise, lance la revue KYC)       [wise]
    POST   /v1/links/{us}/url {redirect_url}      → {url, expires_at}  (lien KYC hébergé par Wise, 5 min)     [wise]
    GET    /v1/links/{us}/balance                 → {balances: [{currency, amount_minor}]}                    [wise]
    GET    /v1/cards/{us}                         → {card: {brand, last4, exp_month, exp_year, cvv_kept} | null}  [own_card]
    PUT    /v1/cards/{us} {number, exp_month, exp_year, holder, keep_cvv?, cvv?}  → {card}   (coffre local)  [own_card]
    POST   /v1/cards/{us}/cvv-mode {keep, cvv?}   → {cvv_kept}                                             [own_card]
    POST   /v1/tokens/{md}/cvv {account, value}   → {accepted}  (CVV de CE paiement, en mémoire)            [own_card]
    POST   /v1/tokens/{md}/otp {account, value}   → {accepted}  (code de la banque, tapé par le composant)  [own_card]
    POST   /v1/tokens {account, envelope}         → jeton        (crée UN SPT pour un mandat signé, idempotent)
    GET    /v1/tokens/{md}?account=us_…           → jeton
    POST   /v1/tokens/{md}/revoke {account}       → jeton ou carte (carte : bloquée chez l'émetteur)
    GET    /v1/tokens/{md}/3ds?account=us_…       → défi 3-D Secure en attente (détails + défi à signer)  [wise]
    POST   /v1/tokens/{md}/3ds {account, challenge_id, proof | decline}  → carte                        [wise]
Émetteurs :
    POST   /stripe/webhook                        (signature `Stripe-Signature` vérifiée)
    POST   /wise/webhook                          (signature `X-Signature-SHA256` vérifiée ; réponse < 5 s)
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
# `local-owner` : le propriétaire de la machine Lokkal (own_card, docs/PAY-AGENT.md §11), sans compte us_….
_ACCOUNT = re.compile(r"^(us_[A-Za-z0-9_-]{1,64}|local-owner)$")
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
        if path == "/wise/webhook" and method == "POST":
            return 200, agent.on_issuer_webhook(body, headers)
        auth = headers.get("Authorization") or ""
        if not agent.cfg.kernel_token or not hmac.compare_digest(auth, f"Bearer {agent.cfg.kernel_token}"):
            return 401, {"error": {"code": "unauthorized", "message": "jeton du noyau attendu"}}
        data = json.loads(body or b"{}") if body else {}
        if not isinstance(data, dict):
            raise AgentError(400, "invalid", "objet JSON attendu")
        m = re.fullmatch(r"/v1/links/([^/]+)(/url|/balance)?", path)
        if m:
            account, tail = _account(m.group(1)), m.group(2)
            if method == "GET" and not tail:
                return 200, {"link": agent.link_view(account)}
            if method == "POST" and not tail:
                return 200, {"link": agent.link_start(account, data.get("person"))}
            if method == "POST" and tail == "/url":
                return 200, agent.link_url(account, data.get("redirect_url"))
            if method == "GET" and tail == "/balance":
                return 200, {"balances": agent.balance(account)}
        m = re.fullmatch(r"/v1/cards/([^/]+)(/cvv-mode)?", path)
        if m:
            account, tail = _account(m.group(1)), m.group(2)
            if method == "GET" and not tail:
                return 200, {"card": agent.card_meta(account)}
            if method == "PUT" and not tail:
                return 200, {"card": agent.register_card(account, data)}
            if method == "POST" and tail:
                return 200, agent.set_cvv_mode(account, data.get("keep") is True, data.get("cvv"))
        m = re.fullmatch(r"/v1/tokens/([^/]+)/(cvv|otp)", path)
        if m and _MANDATE.match(m.group(1)) and method == "POST":
            return 200, agent.provide(_account(data.get("account")), m.group(1), m.group(2), data.get("value"))
        m = re.fullmatch(r"/v1/methods/([^/]+)", path)
        if m:
            account = _account(m.group(1))
            if method == "GET":
                return 200, {"method": agent.method(account)}
            if method == "PUT":
                holder = data.get("holder_name")
                return 200, {"method": agent.register_method(account, data.get("payment_method"), data.get("proof"),
                                                             holder if isinstance(holder, str) else None)}
            if method == "DELETE":
                return 200, {"removed": agent.remove_method(account)}
        if path == "/v1/tokens" and method == "POST":
            ref = data.get("kernel_ref")
            return 200, agent.issue(_account(data.get("account")), data.get("envelope"),
                                    ref if isinstance(ref, str) else "")
        m = re.fullmatch(r"/v1/tokens/([^/]+)/3ds", path)
        if m and _MANDATE.match(m.group(1)):
            if method == "GET":
                return 200, agent.threeds(_account((query.get("account") or [""])[0]), m.group(1))
            if method == "POST":
                cid = data.get("challenge_id")
                return 200, agent.answer_threeds(_account(data.get("account")), m.group(1),
                                                 cid if isinstance(cid, str) else "", data.get("proof"),
                                                 data.get("decline") is True)
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
