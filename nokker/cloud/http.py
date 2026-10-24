"""Routes de nokker.ai pour Nokker Lokkal (docs/LOKKAL.md §7.2 révisé, §7.5). Branché comme `nokker/onboarding/http.py` :

    from nokker.cloud import http as cloud_http
    cloud_http.configure(CloudLink(...))                  # une fois au démarrage (config `cloud_link`)
    if cloud_http.handle(self, "GET" | "POST", path): return

Routes (rien d'autre n'est exposé ; `handle` rend False tant que `configure` n'a pas été appelé) :
    POST /v1/link/device   {machine}            → codes du flux d'appareil (public, limité par IP)
    GET  /v1/link/pending?code=… (session du compte) → {machine_name, expires_in} | 404 (inconnu ou expiré)
    POST /v1/link/approve  {user_code, decision} (session du compte) → {state, machine_id, host}
    POST /v1/link/token    {device_code}        → {state, api_key, machine_token, account, relay_host, inference_base}
    POST /v1/link/revoke   (Bearer jeton de machine)
    GET  /v1/gateway/machine (Bearer jeton de machine) → {machine_id, name, tenant, inference}  (pour le relais)
    POST /v1/gateway/token (session du compte) → {access_token, expires_in}  (navigateur → <nom>.nokker.app)
    GET  /v1/.well-known/jwks.json (aussi /.well-known/jwks.json en interne)
    POST /v1/inference/chat/completions (Bearer jeton de machine) → relayé au Router, compté au locataire
    POST /v1/mirror/turns {turns} | GET /v1/mirror/turns?since= (Bearer jeton de machine, abonnés) → miroir (L5)
    POST /v1/mirror/erase (Bearer jeton de machine) → {erased_turns} : efface les copies de cette machine
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from nokker.cloud.link import LinkService, Signer

logger = logging.getLogger("nokker.cloud.http")
RATE_MAX, RATE_WINDOW_S = 20, 60.0


@dataclass
class CloudLink:
    links: LinkService
    signer: Signer
    identity: Callable[[Any], str | None]  # handler → compte vérifié (session d'appareil), ou None
    tenant_of: Callable[[str], str | None]  # compte → locataire
    router_base: str
    router_key: Callable[[], str]
    record_usage: Callable[[str, str, dict[str, Any], float], None] | None = None  # (locataire, machine, usage, s)
    store: Any = None  # Store du noyau : miroir web ⇄ Lokkal (nokker/cloud/mirror.py)
    mirror_allowed: Callable[[str], bool] | None = None  # compte → miroir permis (abonnés) ; None = miroir éteint
    _hits: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque), init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def rate_ok(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[ip]
            while q and now - q[0] > RATE_WINDOW_S:
                q.popleft()
            if len(q) >= RATE_MAX:
                return False
            q.append(now)
            return True


_cloud: CloudLink | None = None


def configure(cloud: CloudLink | None) -> None:
    global _cloud
    _cloud = cloud


def _bearer(h: Any) -> str:
    auth = h.headers.get("Authorization", "")
    return auth[7:].strip() if auth.startswith("Bearer ") else ""


def handle(h: Any, method: str, path: str) -> bool:
    c = _cloud
    if c is None:
        return False
    # Ingress public d'app.nokker.ai : seul ^/v1/ va au noyau (le reste au web) — d'où le chemin sous /v1 (brosim, 23.10).
    if path in ("/v1/.well-known/jwks.json", "/.well-known/jwks.json") and method == "GET":
        h._json(200, c.signer.jwks())
        return True
    if not path.startswith(("/v1/link/", "/v1/gateway/", "/v1/inference/", "/v1/mirror/")):
        return False
    ip = h.headers.get("CF-Connecting-IP") or h.client_address[0]
    if path == "/v1/link/device" and method == "POST":
        if not c.rate_ok(ip):
            h._json(429, {"error": "rate", "retry_after_s": int(RATE_WINDOW_S)})
            return True
        body = h._read_json_body()
        h._json(200, c.links.device(str(body.get("machine") or "machine")))
        return True
    if path == "/v1/link/token" and method == "POST":
        if not c.rate_ok(ip):
            h._json(429, {"error": "rate", "retry_after_s": int(RATE_WINDOW_S)})
            return True
        h._json(200, c.links.token(str(h._read_json_body().get("device_code") or "")))
        return True
    if path == "/v1/link/pending" and method == "GET":
        account = c.identity(h)
        if not account or not c.tenant_of(account):
            h._json(401, {"error": {"message": "session du compte requise"}})
            return True
        if not c.rate_ok(f"pending:{account}"):  # pas de devinette de codes depuis un compte
            h._json(429, {"error": "rate", "retry_after_s": int(RATE_WINDOW_S)})
            return True
        from urllib.parse import parse_qs, urlsplit

        code = (parse_qs(urlsplit(h.path).query).get("code") or [""])[0]
        found = c.links.pending(code)
        h._json(200, found) if found else h._json(404, {"error": {"message": "code inconnu ou expiré"}})
        return True
    if path in ("/v1/link/approve", "/v1/gateway/token") and method == "POST":
        account = c.identity(h)
        tenant = c.tenant_of(account) if account else None
        if not account or not tenant:
            h._json(401, {"error": {"message": "session du compte requise"}})
            return True
        if path == "/v1/gateway/token":
            h._json(200, {"access_token": c.signer.access_token(account, tenant), "expires_in": 900})
            return True
        body = h._read_json_body()
        try:
            h._json(200, c.links.approve(str(body.get("user_code") or ""), account, tenant,
                                         str(body.get("decision") or "approve")))
        except LookupError as e:
            h._json(404, {"error": {"message": str(e)}})
        return True
    machine = c.links.machine_for_token(_bearer(h))
    if machine is None:
        h._json(401, {"error": {"message": "jeton de machine requis"}})
        return True
    if path == "/v1/link/revoke" and method == "POST":
        c.links.revoke(_bearer(h))
        h._json(200, {"revoked": True})
        return True
    if path == "/v1/gateway/machine" and method == "GET":
        h._json(200, {"machine_id": machine.id, "name": machine.name, "tenant": machine.tenant, "host": machine.host,
                      "inference": True})  # l'inférence passe par nous dès que la machine est liée (§7.2 révisé)
        return True
    if path == "/v1/mirror/erase" and method == "POST":
        from nokker.cloud import mirror

        # Effacement des copies de CETTE machine : un droit, pas une option de l'abonnement (abonnement échu compris).
        if c.store is None:
            h._json(404, {"error": {"message": "miroir non configuré"}})
            return True
        h._json(200, {"erased_turns": mirror.erase(c.store, machine.tenant, machine.id)})
        return True
    if path == "/v1/mirror/turns":
        from nokker.cloud import mirror

        if c.store is None or c.mirror_allowed is None or not c.mirror_allowed(machine.account):
            h._json(403, {"error": {"message": "miroir réservé aux abonnés"}})
            return True
        if method == "POST":
            h._json(200, mirror.push(c.store, machine.tenant, machine.id, list(h._read_json_body().get("turns") or [])))
            return True
        if method == "GET":
            from urllib.parse import parse_qs, urlsplit

            since = (parse_qs(urlsplit(h.path).query).get("since") or [None])[0]
            try:
                h._json(200, mirror.pull(c.store, machine.tenant, since))
            except ValueError:
                h._json(400, {"error": {"message": "curseur illisible"}})
            return True
    if path == "/v1/inference/chat/completions" and method == "POST":
        _relay_inference(h, c, machine)
        return True
    h._json(404, {"error": {"message": "not found"}})
    return True


def _relay_inference(h: Any, c: CloudLink, machine: Any) -> None:
    """Relaie un appel compatible OpenAI au Router, avec la clé de nokker.ai ; l'usage est compté au locataire de la
    machine. Flux SSE relayé tel quel quand `stream` est demandé."""
    length = int(h.headers.get("Content-Length") or 0)
    raw = h.rfile.read(length) if length else b"{}"
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        h._json(400, {"error": {"message": "invalid JSON"}})
        return
    t0 = time.monotonic()
    url = c.router_base.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {c.router_key()}", "Content-Type": "application/json"}
    try:
        if body.get("stream"):
            with httpx.Client(timeout=httpx.Timeout(30.0, read=600.0)) as client, \
                    client.stream("POST", url, headers=headers, content=raw) as r:
                h.send_response(r.status_code)
                h.send_header("Content-Type", r.headers.get("content-type", "text/event-stream"))
                h.send_header("Transfer-Encoding", "chunked")
                h.end_headers()
                usage: dict[str, Any] = {}
                for chunk in r.iter_raw():
                    h._write_chunk(chunk)
                    if b'"usage"' in chunk:
                        usage = _last_usage(chunk) or usage
                h._write_chunk(b"")
        else:
            r = httpx.post(url, headers=headers, content=raw, timeout=600.0)
            data = r.content
            h.send_response(r.status_code)
            h.send_header("Content-Type", r.headers.get("content-type", "application/json"))
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            h.wfile.write(data)
            try:
                usage = json.loads(data).get("usage") or {}
            except (json.JSONDecodeError, AttributeError):
                usage = {}
    except httpx.HTTPError as e:
        logger.warning("relais d'inférence en échec : %s", e)
        h._json(502, {"error": {"message": "Router injoignable"}})
        return
    if c.record_usage is not None:
        try:
            c.record_usage(machine.tenant, machine.id, usage, time.monotonic() - t0)
        except Exception:  # noqa: BLE001 — le comptage ne casse pas la réponse déjà partie
            logger.exception("comptage de l'inférence relayée en échec")


def _last_usage(chunk: bytes) -> dict[str, Any] | None:
    for line in reversed(chunk.split(b"\n")):
        if line.startswith(b"data: ") and b'"usage"' in line:
            try:
                return json.loads(line[6:]).get("usage")
            except json.JSONDecodeError:
                return None
    return None


__all__ = ["CloudLink", "configure", "handle"]
