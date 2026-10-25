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
    POST /v1/gateway/machine/load {charge} (Bearer jeton de machine) → {stored, at}  (charge de la machine, lot X5)
    POST /v1/gateway/token (session du compte) → {access_token, expires_in}  (navigateur → <nom>.nokker.app)
    GET  /v1/.well-known/jwks.json (aussi /.well-known/jwks.json en interne)
    POST /v1/inference/chat/completions (Bearer jeton de machine) → relayé au Router, compté au locataire
    POST /v1/mirror/turns {turns} | GET /v1/mirror/turns?since= (Bearer jeton de machine, abonnés) → miroir (L5)
    POST /v1/mirror/erase (Bearer jeton de machine) → {erased_turns} : efface les copies de cette machine
"""
from __future__ import annotations

import json
import logging
import re
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
MAX_LOAD_BYTES = 16 * 1024  # une charge fait quelques centaines d'octets (docs/LOKKAL.md §7.2)


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
    gateway_auth: Any = None  # nokker.cloud.gateway_auth.GatewayAuth : connexion par la passerelle (§7.3)
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


_RE_MACHINE = re.compile(r"^/v1/link/machines/(mc_[0-9a-f]{16})/(revoke|pages-token)$")
_RE_PAGE_ID = re.compile(r"^pg_[0-9a-f]{16}$")


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
    if path in ("/v1/gateway/authorize", "/v1/gateway/refresh", "/v1/gateway/revoke") and method == "POST" or (
            path == "/v1/gateway/token" and method == "POST" and c.gateway_auth is not None and _has_code(h)):
        _gateway_login(h, c, path)
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
    m = _RE_MACHINE.match(path)
    if path == "/v1/link/machines" or m:
        _account_machines(h, c, method, path, m)
        return True
    machine = c.links.machine_for_token(_bearer(h))
    if machine is None:
        h._json(401, {"error": {"message": "jeton de machine requis"}})
        return True
    if path == "/v1/gateway/pages-token" and method == "POST":
        # Le Lokkal demande lui-même le lien d'une de SES pages, pour une interface servie par la passerelle (qui n'a
        # ni l'id de la machine ni de session app.nokker.ai) : il a déjà vérifié le jeton d'accès du compte.
        _pages_link(h, c, machine, machine.account, machine.tenant, h._read_json_body())
        return True
    if path == "/v1/link/revoke" and method == "POST":
        c.links.revoke(_bearer(h))
        h._json(200, {"revoked": True})
        return True
    if path == "/v1/gateway/machine/load" and method == "POST":
        _machine_load(h, c, machine)
        return True
    if path == "/v1/gateway/machine" and method == "GET":
        seen = getattr(c.links.store, "seen", None)
        if seen is not None:
            try:
                seen(machine.id, time.time())  # « vue à » de l'écran « mes machines »
            except Exception:  # noqa: BLE001 — le contrôle du relais ne dépend pas de cette date
                pass
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


def _has_code(h: Any) -> bool:
    """`POST /v1/gateway/token` avec un code (échange du relais, §7.3) ou sans (ancien chemin : session du compte)."""
    body = h._read_json_body()
    h._cached_body = body
    return bool(body.get("code"))


def _gateway_login(h: Any, c: CloudLink, path: str) -> None:
    """Connexion à la passerelle (docs/LOKKAL.md §7.3, nokker/cloud/gateway_auth.py). `authorize` : session du compte
    (page app.nokker.ai/gateway/authorize) ; `token`/`refresh`/`revoke` : appelés par le relais, la preuve est dans le
    corps (code + vérificateur PKCE, ou jeton de renouvellement)."""
    from nokker.cloud.gateway_auth import GrantError

    ga = c.gateway_auth
    if ga is None:
        h._json(404, {"error": {"message": "not found"}})
        return
    body = getattr(h, "_cached_body", None) or h._read_json_body()
    host = str(body.get("host") or "")
    try:
        if path == "/v1/gateway/authorize":
            account = c.identity(h)
            tenant = c.tenant_of(account) if account else None
            if not account or not tenant:
                h._json(401, {"error": {"message": "session du compte requise"}})
                return
            if str(body.get("code_challenge_method") or "S256") != "S256":
                h._json(400, {"error": "invalid_request", "message": "code_challenge_method S256 seulement"})
                return
            h._json(200, ga.authorize(account, tenant, host, str(body.get("code_challenge") or "")))
        elif path == "/v1/gateway/token":
            h._json(200, ga.token(str(body.get("code") or ""), str(body.get("code_verifier") or ""), host))
        elif path == "/v1/gateway/refresh":
            h._json(200, ga.refresh(str(body.get("refresh_token") or ""), host))
        else:
            ga.revoke(str(body.get("refresh_token") or ""))
            h._json(200, {"revoked": True})
    except PermissionError:
        h._json(403, {"error": {"message": "cette machine n'est pas liée à ce compte"}})
    except GrantError:
        h._json(400, {"error": "invalid_grant"})


def _account_machines(h: Any, c: CloudLink, method: str, path: str, m: Any) -> None:
    """Écran « mes machines » (onboarding, docs/LOKKAL.md §7.2), session du compte :
    GET /v1/link/machines ; POST /v1/link/machines/{id}/revoke ; POST /v1/link/machines/{id}/pages-token
    {page_id, embed?} → {url, expires_at} (docs/BUBBLES.md §10.3). Une machine d'un autre compte : 404."""
    account = c.identity(h)
    tenant = c.tenant_of(account) if account else None
    if not account or not tenant:
        h._json(401, {"error": {"message": "session du compte requise"}})
        return
    if m is None:
        if method != "GET":
            h._json(404, {"error": {"message": "not found"}})
            return
        now = time.time()
        h._json(200, {"machines": [{"machine_id": x.id, "name": x.name, "host": x.host, "created_at": x.created_at,
                                    "last_seen_at": x.last_seen_at, "load": x.load, "load_at": x.load_at,
                                    "load_age_s": None if x.load_at is None else max(0, int(now - x.load_at))}
                                   for x in c.links.machines_with_load(account, tenant)]})
        return
    machine_id, action = m.group(1), m.group(2)
    if method != "POST":
        h._json(404, {"error": {"message": "not found"}})
        return
    if action == "revoke":
        if c.links.revoke_machine(account, machine_id):
            h._json(200, {"revoked": True})
        else:
            h._json(404, {"error": {"message": "not found"}})
        return
    machine = c.links.machine_of(account, machine_id)
    if machine is None:
        h._json(404, {"error": {"message": "not found"}})
        return
    _pages_link(h, c, machine, account, tenant, h._read_json_body())


def _machine_load(h: Any, c: CloudLink, machine: Any) -> None:
    """Charge envoyée par la machine liée (lot X5, docs/LOKKAL.md §7.2) : toutes les 60 s et à l'ouverture du tunnel.
    Une ligne courante par machine ; 20 envois par minute au plus par machine."""
    if not c.rate_ok(f"load:{machine.id}"):
        h._json(429, {"error": "rate", "retry_after_s": int(RATE_WINDOW_S)})
        return
    if int(h.headers.get("Content-Length") or 0) > MAX_LOAD_BYTES:
        h._json(413, {"error": {"message": "charge trop grande"}})
        return
    load = c.links.report_load(machine, h._read_json_body())
    if load is None:
        h._json(400, {"error": {"message": "charge illisible"}})
        return
    h._json(200, {"stored": True, "at": int(time.time())})


def _pages_link(h: Any, c: CloudLink, machine: Any, account: str, tenant: str, body: dict[str, Any]) -> None:
    page_id = str(body.get("page_id") or "")
    if not _RE_PAGE_ID.match(page_id):
        h._json(404, {"error": {"message": "not found"}})
        return
    from urllib.parse import quote

    token = c.signer.pages_token(account, tenant, machine.id)
    pages_host = machine.host.replace(".", "--pages.", 1)  # <nom>.nokker.app → <nom>--pages.nokker.app
    to = f"/{page_id}?dl=1" if body.get("download") else f"/{page_id}"
    url = f"https://{pages_host}/_auth?token={quote(token)}&to={quote(to, safe='/')}"
    # embed : lien de l'iframe (le relais redirige vers une capacité d'URL, sans cookie tiers) ; sinon, un onglet.
    h._json(200, {"url": url + ("&embed=1" if body.get("embed") else ""), "expires_at": int(time.time()) + 60})


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
