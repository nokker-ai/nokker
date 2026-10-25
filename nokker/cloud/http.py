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
    GET  /v1/gateway/connectors (Bearer jeton de machine) → connecteurs du compte et leurs outils (lot X20)
    POST /v1/gateway/connectors/{id}/call {tool, arguments, approval?} (Bearer jeton de machine) → résultat caviardé,
         compté et audité (nokker/cloud/relay.py, docs/LOKKAL.md §7.12)
Approbations fortes par la passerelle (lot X10, docs/LOKKAL.md §7.3, nokker/cloud/approvals.py) :
    POST /v1/gateway/approvals {approval_id, title, reason, strength, expires_at, challenge, advice?} (jeton de machine)
    GET  /v1/gateway/approvals/decisions?wait=25 (jeton de machine, attente longue) → {decisions: [...]}
    POST /v1/gateway/approvals/{approval_id}/result {status, outcome?, error?} (jeton de machine)
    GET  /v1/gateway/devices (jeton de machine) → {devices: [{device_id, public_key_spki, status, can_decide, name}]}
    GET  /v1/link/machines/{machine_id}/approvals[/{approval_id}] (session du compte)
    POST /v1/link/machines/{machine_id}/approvals/{approval_id}/decision {decision, proof} (session) → 202 forwarded
    GET  /v1/mirror/projects, GET|POST /v1/mirror/projects/{id}/notes (Bearer jeton de machine) → équipe (X17,
         drapeau `team`, nokker/team/http.py ; 404 drapeau éteint)
Délégation web → Lokkal par besoin (lot X21, docs/LOKKAL.md §7.16, nokker/cloud/handoff.py) :
    POST /v1/gateway/machine/resources {repos, hosts, connectors, mcp} (jeton de machine) → {stored, at} (noms seuls)
    POST /v1/gateway/handoffs/{handoff_id}/result {turn_index, reply, pending_approvals} (jeton de machine)
    GET  /v1/handoffs?thread_id= | GET /v1/handoffs/{handoff_id} (session du compte) → demandes du compte
    POST /v1/handoffs/{handoff_id}/accept | /refuse (session du compte) → la demande (l'acceptation lance la session)
    POST /v1/mirror/notes {notes, forget} (jeton de machine, abonnés) → {accepted, refused, forgotten} (notes `both`)
Deck multi-machines et agents placés (lot X15, docs/LOKKAL.md §7.19, nokker/cloud/crew.py) :
    GET  /v1/link/deck (session du compte) → toutes les machines du compte, leurs sessions, leurs agents, leurs cartes
    POST /v1/gateway/machine/deck {sessions, agents, pending_approvals} (jeton de machine) → {stored, at}
    POST /v1/gateway/presence {online} (jeton de machine, appelé par le relais) → {stored, online}
    POST /v1/gateway/agent-runs/{run_id}/result {status, thread_id?, summary?, cost?} (jeton de machine) → {stored, status}
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
# Connexion par la passerelle (§7.3) : `token|refresh|revoke` sont appelés par le RELAIS, qui transmet l'IP réelle du
# navigateur (`CF-Connecting-IP`, relais 60ff099) : 30 par minute et par IP suffisent à un navigateur (un
# renouvellement toutes les 15 min), réglable (`cloud_link.gateway_rate_max`) ; un même jeton de renouvellement ne sert
# qu'une fois par rotation, 10 essais par minute suffisent largement.
GATEWAY_RATE_MAX = 30
GATEWAY_TOKEN_RATE_MAX = 10
# Flux d'appareil (§7.2) : la machine sonde `/v1/link/token` toutes les 5 s (`interval`) ; 30 par minute et par
# `device_code` laissent deux onglets de l'écran de lien ouvert. `/v1/link/token` par IP : un bureau peut lier plusieurs
# machines à la fois derrière la même adresse.
DEVICE_POLL_RATE_MAX = 30
LINK_TOKEN_IP_RATE_MAX = 60
# Code d'appareil saisi (`user_code`, 8 caractères parmi 28, ~3,8·10^11) : au plus 10 codes faux par compte ET par IP en
# 15 min, puis 429 (même pour le bon code) jusqu'à ce que le plus ancien essai sorte de la fenêtre ; et chaque code meurt
# après `LinkService.max_guesses_per_code` codes faux, tous comptes confondus, depuis sa création.
LINK_CODE_FAILS_MAX, LINK_CODE_FAILS_WINDOW_S = 10, 900.0
MAX_LOAD_BYTES = 16 * 1024  # une charge fait quelques centaines d'octets (docs/LOKKAL.md §7.2)
MAX_RELAY_BYTES = 16 * 1024  # carte, preuve ou résultat d'approbation relayée : quelques Kio au plus
MAX_RESOURCES_BYTES = 64 * 1024  # inventaire : 4 natures × 200 noms de 64 car. au plus
MAX_RESULT_BYTES = 64 * 1024  # résultat d'un tour délégué (la réponse est bornée à 8 000 car. à l'arrivée)
MAX_NOTES_BYTES = 1024 * 1024  # 200 notes de 4 000 car. au plus
MAX_INFERENCE_BYTES = 8 * 1024 * 1024  # un contexte long tient en quelques Mio ; au-delà : 413


@dataclass
class CloudLink:
    links: LinkService
    signer: Signer
    identity: Callable[[Any], str | None]  # handler → compte vérifié (session d'appareil), ou None
    tenant_of: Callable[[str], str | None]  # compte → locataire
    router_base: str
    router_key: Callable[[], str]
    # (locataire, machine, usage, durée s, modèle), appelé dans tenant_scope(locataire)
    record_usage: Callable[..., None] | None = None
    store: Any = None  # Store du noyau : miroir web ⇄ Lokkal (nokker/cloud/mirror.py)
    mirror_allowed: Callable[[str], bool] | None = None  # compte → miroir permis (abonnés) ; None = miroir éteint
    gateway_auth: Any = None  # nokker.cloud.gateway_auth.GatewayAuth : connexion par la passerelle (§7.3)
    relay: Any = None  # nokker.cloud.relay.ConnectorRelay : connecteurs relayés vers Lokkal (lot X20) ; None = éteints
    # Approbations fortes par la passerelle (lot X10) : boîte aux lettres (nokker.cloud.approvals.RelayService), appareils
    # du compte (compte → [Device], le backend d'approbations), notification Telegram d'une carte déposée.
    approvals_relay: Any = None
    devices_of: Callable[[str], list[Any]] | None = None
    notify_approval: Callable[[Any, Any], None] | None = None
    # Délégation web → Lokkal (lot X21) : demandes de relais ; None = routes /v1/handoffs absentes (404).
    handoffs: Any = None
    mirror_notes: Any = None  # notes en miroir (MemoryMirrorNotes / PgMirrorNotes, migration 025)
    # Deck multi-machines et agents placés (lot X15) : nokker.cloud.crew.CloudCrew ; None = routes absentes (404).
    crew: Any = None
    agents: Any = None  # AgentsHub de nokker.ai (agents du deck), posé par serve
    # Inférence relayée (revue du train du 2.11, décision 1) : plafonds du compte (`TenantLimits` du noyau, qui suit
    # l'abonnement quand la facturation le lui dit), modèles permis (ceux de la config du noyau sur le point d'accès
    # relayé), débit par machine et appels par jour et par compte. `models` vide = aucun modèle imposé (tests anciens).
    limits: Any = None
    models: frozenset[str] = frozenset()
    default_model: str = ""
    inference_rate_max: int = 60  # appels par minute et par machine (une session de code enchaîne vite)
    inference_calls_per_day: int = 2000  # appels par jour de quota et par compte, suites de tour comprises
    gateway_rate_max: int = GATEWAY_RATE_MAX  # `token|refresh|revoke` par minute et par IP du navigateur (§7.3)
    _hits: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque), init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _sweep_at: float = field(default=0.0, init=False, repr=False)

    def rate_wait(self, key: str, limit: int = RATE_MAX, window_s: float = RATE_WINDOW_S, *,
                  record: bool = True) -> int:
        """0 = permis (et compté si `record`) ; sinon le nombre de secondes avant qu'un essai repasse (`retry_after_s`).
        Fenêtre glissante, en mémoire du processus (un seul réplica, comme la présence)."""
        now = time.monotonic()
        with self._lock:
            if now - self._sweep_at > 600 and len(self._hits) > 10_000:  # clés (IP, jetons) mortes : mémoire bornée
                self._sweep_at = now
                for k in [k for k, q in self._hits.items() if not q or now - q[-1] > LINK_CODE_FAILS_WINDOW_S]:
                    del self._hits[k]
            q = self._hits[key]
            while q and now - q[0] > window_s:
                q.popleft()
            if len(q) >= limit:
                return max(1, int(window_s - (now - q[0])) + 1)
            if record:
                q.append(now)
            return 0

    def rate_ok(self, ip: str, limit: int = RATE_MAX) -> bool:
        return self.rate_wait(ip, limit) == 0


_cloud: CloudLink | None = None


_RE_MACHINE = re.compile(r"^/v1/link/machines/(mc_[0-9a-f]{16})/(revoke|pages-token)$")
_RE_PAGE_ID = re.compile(r"^pg_[0-9a-f]{16}$")
_RE_RELAY_CALL = re.compile(r"^/v1/gateway/connectors/([a-z_]{1,32})/call$")
RELAY_RATE_MAX = 120  # appels relayés par minute et machine : une session lit volontiers une boîte en rafale
_RE_ACCOUNT_RELAY = re.compile(r"^/v1/link/machines/(mc_[0-9a-f]{16})/approvals(?:/([A-Za-z0-9_-]{1,64})(/decision)?)?$")
_RE_MACHINE_RESULT = re.compile(r"^/v1/gateway/approvals/([A-Za-z0-9_-]{1,64})/result$")
_RE_HANDOFF = re.compile(r"^/v1/handoffs(?:/(ho_[0-9a-f]{16})(?:/(accept|refuse))?)?$")
_RE_HANDOFF_RESULT = re.compile(r"^/v1/gateway/handoffs/(ho_[0-9a-f]{16})/result$")
_RE_AGENT_RUN_RESULT = re.compile(r"^/v1/gateway/agent-runs/(ar_[0-9a-f]{16})/result$")
MAX_DECK_BYTES = 64 * 1024  # deck d'une machine : 100 sessions et 200 agents, noms et états


# État du lien vu de `/healthz` (`gateway`) : `on` (clé lue, routes servies), `off` (pas de bloc `cloud_link`),
# `no_key` (bloc posé, clé absente : inerte), `invalid_key` (clé illisible : inerte, ERROR au démarrage).
GATEWAY_STATES = frozenset({"on", "off", "no_key", "invalid_key"})
_gateway_state = "off"


def configure(cloud: CloudLink | None, state: str | None = None) -> None:
    global _cloud, _gateway_state
    _cloud = cloud
    _gateway_state = state if state in GATEWAY_STATES else ("on" if cloud is not None else "off")


def gateway_state() -> str:
    return _gateway_state


def _limited(h: Any, c: CloudLink, *checks: tuple[str, int] | tuple[str, int, float]) -> bool:
    """Contrôles (clé, limite[, fenêtre]) dans l'ordre ; au premier dépassé : `429 {error: rate, retry_after_s}` et True."""
    for key, limit, *window in checks:
        wait = c.rate_wait(key, limit, *window)
        if wait:
            h._json(429, {"error": "rate", "retry_after_s": wait})
            return True
    return False


def _code_locked(h: Any, c: CloudLink, account: str, ip: str) -> bool:
    """Trop de codes d'appareil faux depuis ce compte ou cette IP (devinette) : 429, même pour le bon code."""
    for key in (f"code-fail:acct:{account}", f"code-fail:ip:{ip}"):
        wait = c.rate_wait(key, LINK_CODE_FAILS_MAX, LINK_CODE_FAILS_WINDOW_S, record=False)
        if wait:
            h._json(429, {"error": "rate", "retry_after_s": wait})
            return True
    return False


def _code_failed(c: CloudLink, account: str, ip: str) -> None:
    for key in (f"code-fail:acct:{account}", f"code-fail:ip:{ip}"):
        c.rate_wait(key, 10 ** 9, LINK_CODE_FAILS_WINDOW_S)


def _sha(s: str) -> str:
    import hashlib

    return hashlib.sha256(s.encode()).hexdigest()[:32]


def _bearer(h: Any) -> str:
    auth = h.headers.get("Authorization", "")
    return auth[7:].strip() if auth.startswith("Bearer ") else ""


def handle(h: Any, method: str, path: str) -> bool:
    c = _cloud
    if c is None:
        return False
    # Ingress public d'app.nokker.ai : seul ^/v1/ va au noyau (le reste au web) — d'où le chemin sous /v1 (23.10).
    if path in ("/v1/.well-known/jwks.json", "/.well-known/jwks.json") and method == "GET":
        h._json(200, c.signer.jwks())
        return True
    if path == "/v1/handoffs" or path.startswith("/v1/handoffs/"):
        _account_handoffs(h, c, method, path)
        return True
    if not path.startswith(("/v1/link/", "/v1/gateway/", "/v1/inference/", "/v1/mirror/")):
        return False
    ip = h.headers.get("CF-Connecting-IP") or h.client_address[0]
    if path == "/v1/link/device" and method == "POST":
        if _limited(h, c, (f"device:{ip}", RATE_MAX)):
            return True
        body = h._read_json_body()
        h._json(200, c.links.device(str(body.get("machine") or "machine")))
        return True
    if path == "/v1/link/token" and method == "POST":
        if _limited(h, c, (f"link-token:{ip}", LINK_TOKEN_IP_RATE_MAX)):
            return True
        device_code = str(h._read_json_body().get("device_code") or "")
        # Un même code sondé plus vite que l'`interval` annoncé (5 s), même depuis plusieurs IP.
        if _limited(h, c, (f"link-poll:{_sha(device_code)}", DEVICE_POLL_RATE_MAX)):
            return True
        h._json(200, c.links.token(device_code))
        return True
    if path == "/v1/link/pending" and method == "GET":
        account = c.identity(h)
        if not account or not c.tenant_of(account):
            h._json(401, {"error": {"message": "session du compte requise"}})
            return True
        # pas de devinette de codes depuis un compte ni depuis une IP
        if _limited(h, c, (f"pending:{account}", RATE_MAX), (f"link-code:{ip}", 2 * RATE_MAX)) \
                or _code_locked(h, c, account, ip):
            return True
        from urllib.parse import parse_qs, urlsplit

        code = (parse_qs(urlsplit(h.path).query).get("code") or [""])[0]
        found = c.links.pending(code)
        if not found:
            _code_failed(c, account, ip)
        h._json(200, found) if found else h._json(404, {"error": {"message": "code inconnu ou expiré"}})
        return True
    if path in ("/v1/gateway/token", "/v1/gateway/refresh", "/v1/gateway/revoke") and method == "POST" \
            and _limited(h, c, (f"gw:{ip}", c.gateway_rate_max)):
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
            if _limited(h, c, (f"gw-session:{account}", RATE_MAX)):
                return True
            h._json(200, {"access_token": c.signer.access_token(account, tenant), "expires_in": 900})
            return True
        if _limited(h, c, (f"approve:{account}", RATE_MAX), (f"link-code:{ip}", 2 * RATE_MAX)) \
                or _code_locked(h, c, account, ip):
            return True
        body = h._read_json_body()
        try:
            h._json(200, c.links.approve(str(body.get("user_code") or ""), account, tenant,
                                         str(body.get("decision") or "approve")))
        except LookupError as e:
            _code_failed(c, account, ip)
            h._json(404, {"error": {"message": str(e)}})
        return True
    ra = _RE_ACCOUNT_RELAY.match(path)
    if ra:
        _account_relay(h, c, method, ra)
        return True
    if path == "/v1/link/deck":
        _account_deck(h, c, method)
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
    if path == "/v1/gateway/machine/resources" and method == "POST":
        _machine_resources(h, c, machine)
        return True
    if path == "/v1/gateway/machine/deck" and method == "POST":
        _machine_deck(h, c, machine)
        return True
    if path == "/v1/gateway/presence" and method == "POST":
        _machine_presence(h, c, machine)
        return True
    if _RE_AGENT_RUN_RESULT.match(path) and method == "POST":
        _machine_agent_run_result(h, c, machine, _RE_AGENT_RUN_RESULT.match(path).group(1))
        return True
    if _RE_HANDOFF_RESULT.match(path) and method == "POST":
        _machine_handoff_result(h, c, machine, _RE_HANDOFF_RESULT.match(path).group(1))
        return True
    if path == "/v1/gateway/connectors" or _RE_RELAY_CALL.match(path):
        _relay_connectors(h, c, machine, method, path)
        return True
    if path in ("/v1/gateway/approvals", "/v1/gateway/approvals/decisions", "/v1/gateway/devices") \
            or _RE_MACHINE_RESULT.match(path):
        _machine_relay(h, c, machine, method, path)
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
        notes = c.mirror_notes.erase(machine.tenant, machine.id) if c.mirror_notes is not None else 0
        h._json(200, {"erased_turns": mirror.erase(c.store, machine.tenant, machine.id), "erased_notes": notes})
        return True
    if path == "/v1/mirror/notes" and method == "POST":
        from nokker.cloud import mirror

        if c.mirror_notes is None or c.mirror_allowed is None or not c.mirror_allowed(machine.account):
            h._json(403, {"error": {"message": "miroir réservé aux abonnés"}})
            return True
        if int(h.headers.get("Content-Length") or 0) > MAX_NOTES_BYTES:
            h._json(413, {"error": {"message": "lot de notes trop grand"}})
            return True
        h._json(200, mirror.push_notes(c.mirror_notes, machine.tenant, machine.id, h._read_json_body()))
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
    if path.startswith("/v1/mirror/projects"):  # équipe (X17, drapeau `team`) : projets du compte de la machine
        from nokker.team import http as team_http

        if team_http.handle_machine(h, method, path, machine):
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
    if path in ("/v1/gateway/refresh", "/v1/gateway/revoke") and _limited(
            h, c, (f"gw-rt:{_sha(str(body.get('refresh_token') or ''))}", GATEWAY_TOKEN_RATE_MAX)):
        return  # un même jeton de renouvellement rejoué en boucle (une rotation n'en consomme qu'un)
    try:
        if path == "/v1/gateway/authorize":
            account = c.identity(h)
            tenant = c.tenant_of(account) if account else None
            if not account or not tenant:
                h._json(401, {"error": {"message": "session du compte requise"}})
                return
            if _limited(h, c, (f"gw-authorize:{account}", RATE_MAX)):
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
                                    "load_age_s": None if x.load_at is None else max(0, int(now - x.load_at)),
                                    "resources": x.resources, "resources_at": x.resources_at}
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
    _machine_back(c, machine)


def _machine_presence(h: Any, c: CloudLink, machine: Any) -> None:
    """Présence dite par le relais (X15, docs/LOKKAL.md §7.19) : `{"online": bool}` à l'ouverture du tunnel et à sa
    fermeture sans remplaçant, avec le jeton de la machine. Au mieux : la fraîcheur de la charge reste l'autorité."""
    if not c.rate_ok(f"presence:{machine.id}"):
        h._json(429, {"error": "rate", "retry_after_s": int(RATE_WINDOW_S)})
        return
    body = h._read_json_body()
    online = body.get("online") if isinstance(body, dict) else None
    if not isinstance(online, bool):
        h._json(400, {"error": {"message": "online (booléen) requis"}})
        return
    c.links.presence(machine, online)
    h._json(200, {"stored": True, "online": online})
    if online:
        _machine_back(c, machine)


def _machine_back(c: CloudLink, machine: Any) -> None:
    """La machine donne signe de vie : ce qui l'attendait repart (relais X21, passages d'agents X15)."""
    if c.handoffs is not None:  # la machine est de retour : ses demandes acceptées qui l'attendaient partent (X21)
        try:
            c.handoffs.machine_online(machine)
        except Exception:  # noqa: BLE001 — la charge est gardée, la reprise retentera au prochain envoi
            logger.exception("reprise des relais de %s en échec", machine.id)
    if c.crew is not None:  # et les passages d'agents qui attendaient une machine (X15)
        try:
            c.crew.machine_online(machine)
        except Exception:  # noqa: BLE001
            logger.exception("reprise des passages d'agents de %s en échec", machine.id)


def _account_deck(h: Any, c: CloudLink, method: str) -> None:
    """`GET /v1/link/deck` (lot X15, session du compte) : la vue agrégée de toutes les machines du compte — charge,
    sessions actives, agents, cartes en attente — plus les agents `web` et `auto`. Noms et états seulement."""
    if c.crew is None or method != "GET":
        h._json(404, {"error": {"message": "not found"}})
        return
    account = c.identity(h)
    tenant = c.tenant_of(account) if account else None
    if not account or not tenant:
        h._json(401, {"error": {"message": "session du compte requise"}})
        return
    h._json(200, c.crew.deck(account, tenant, c.agents))


def _machine_deck(h: Any, c: CloudLink, machine: Any) -> None:
    """Deck publié par la machine (X15) : nettoyé par `clean_deck` (noms et états bornés), une ligne par machine."""
    if c.crew is None:
        h._json(404, {"error": {"message": "not found"}})
        return
    if not c.rate_ok(f"deck:{machine.id}"):
        h._json(429, {"error": "rate", "retry_after_s": int(RATE_WINDOW_S)})
        return
    if int(h.headers.get("Content-Length") or 0) > MAX_DECK_BYTES:
        h._json(413, {"error": {"message": "deck trop grand"}})
        return
    if c.crew.report_deck(machine, h._read_json_body()) is None:
        h._json(400, {"error": {"message": "deck illisible"}})
        return
    h._json(200, {"stored": True, "at": int(time.time())})


def _machine_agent_run_result(h: Any, c: CloudLink, machine: Any, run_id: str) -> None:
    """État d'un passage d'agent envoyé à CETTE machine (X15) ; un passage d'une autre machine : 404."""
    if c.crew is None:
        h._json(404, {"error": {"message": "not found"}})
        return
    if not c.rate_ok(f"agent-run:{machine.id}", RELAY_RATE_MAX):
        h._json(429, {"error": "rate", "retry_after_s": int(RATE_WINDOW_S)})
        return
    if int(h.headers.get("Content-Length") or 0) > MAX_RESULT_BYTES:
        h._json(413, {"error": {"message": "résultat trop grand"}})
        return
    h._json(*c.crew.result(machine, run_id, h._read_json_body()))


def _machine_resources(h: Any, c: CloudLink, machine: Any) -> None:
    """Inventaire des ressources `local` de la machine (lot X21) : noms seulement, nettoyés par `clean_resources`
    (un chemin, un contenu sont jetés) ; une ligne courante par machine, sous RLS (migration 025)."""
    if not c.rate_ok(f"resources:{machine.id}"):
        h._json(429, {"error": "rate", "retry_after_s": int(RATE_WINDOW_S)})
        return
    if int(h.headers.get("Content-Length") or 0) > MAX_RESOURCES_BYTES:
        h._json(413, {"error": {"message": "inventaire trop grand"}})
        return
    if c.links.report_resources(machine, h._read_json_body()) is None:
        h._json(400, {"error": {"message": "inventaire illisible"}})
        return
    h._json(200, {"stored": True, "at": int(time.time())})


def _machine_handoff_result(h: Any, c: CloudLink, machine: Any, handoff_id: str) -> None:
    """Résultat d'un tour d'une session déléguée, envoyé par la machine (après une carte décidée au téléphone, ou en
    même temps que la réponse synchrone : rangé une seule fois par tour). Demande d'une autre machine : 404."""
    if c.handoffs is None:
        h._json(404, {"error": {"message": "not found"}})
        return
    if not c.rate_ok(f"handoff:{machine.id}", RELAY_RATE_MAX):
        h._json(429, {"error": "rate", "retry_after_s": int(RATE_WINDOW_S)})
        return
    if int(h.headers.get("Content-Length") or 0) > MAX_RESULT_BYTES:
        h._json(413, {"error": {"message": "résultat trop grand"}})
        return
    body = h._read_json_body()
    body = body if isinstance(body, dict) else {}
    try:
        turn_index = int(body.get("turn_index"))
        pending = int(body.get("pending_approvals") or 0)
    except (TypeError, ValueError):
        h._json(400, {"error": {"message": "turn_index entier requis"}})
        return
    known = c.handoffs.store.get(machine.tenant, handoff_id)
    if known is None or known.machine_id != machine.id:
        h._json(404, {"error": {"message": "not found"}})
        return
    done = c.handoffs.result(machine.tenant, handoff_id, machine_id=machine.id, reply=str(body.get("reply") or ""),
                             turn_index=turn_index, pending_approvals=pending)
    # `stored: false` : tour déjà rangé (réponse synchrone arrivée avant) ou demande close ; rien à refaire.
    h._json(200, {"stored": done is not None, "state": (done or known).state})


def _account_handoffs(h: Any, c: CloudLink, method: str, path: str) -> None:
    """Demandes de relais du compte (lot X21) : liste, lecture, acceptation ou refus. Une demande d'un autre compte
    n'existe pas (404). Accepter est le SEUL geste qui lance une session sur une machine."""
    from nokker.cloud.handoff import HandoffError, view
    from nokker.tenancy.context import scoped_thread_id

    m = _RE_HANDOFF.match(path)
    if c.handoffs is None or m is None:
        h._json(404, {"error": {"message": "not found"}})
        return
    account = c.identity(h)
    tenant = c.tenant_of(account) if account else None
    if not account or not tenant:
        h._json(401, {"error": {"message": "session du compte requise"}})
        return
    handoff_id, action = m.group(1), m.group(2)
    try:
        if method == "GET" and handoff_id is None:
            from urllib.parse import parse_qs, urlsplit

            thread = (parse_qs(urlsplit(h.path).query).get("thread_id") or [None])[0]
            web_thread = scoped_thread_id(tenant, thread) if thread else None
            h._json(200, {"handoffs": [view(x) for x in c.handoffs.list(tenant, account, web_thread)]})
            return
        if method == "GET" and action is None:
            h._json(200, view(c.handoffs.get(tenant, account, handoff_id)))
            return
        if method == "POST" and action is not None:
            if not c.rate_ok(f"handoff-decide:{account}"):
                h._json(429, {"error": "rate", "retry_after_s": int(RATE_WINDOW_S)})
                return
            h._json(200, view(c.handoffs.decide(tenant, account, handoff_id, action)))
            return
        h._json(404, {"error": {"message": "not found"}})
    except HandoffError as e:
        h._json(e.status, {"error": {"message": str(e)}})


def _relay_connectors(h: Any, c: CloudLink, machine: Any, method: str, path: str) -> None:
    """Connecteurs relayés (lot X20, nokker/cloud/relay.py) : la machine liée liste les outils des connecteurs de son
    compte et les appelle ; l'identifiant reste ici. Relais éteint : 404 (comme une route absente)."""
    from nokker.cloud.relay import MAX_CALL_BYTES

    if c.relay is None:
        h._json(404, {"error": {"message": "not found"}})
        return
    if path == "/v1/gateway/connectors":
        if method != "GET":
            h._json(404, {"error": {"message": "not found"}})
            return
        h._json(200, c.relay.listing(machine))
        return
    if method != "POST":
        h._json(404, {"error": {"message": "not found"}})
        return
    if not c.rate_ok(f"relay:{machine.id}", RELAY_RATE_MAX):
        h._json(429, {"error": "rate", "retry_after_s": int(RATE_WINDOW_S)})
        return
    if int(h.headers.get("Content-Length") or 0) > MAX_CALL_BYTES:
        h._json(413, {"error": {"code": "too_large", "message": "appel trop grand"}})
        return
    status, out = c.relay.call(machine, _RE_RELAY_CALL.match(path).group(1), h._read_json_body())
    h._json(status, out)
def _too_big(h: Any, limit: int = MAX_RELAY_BYTES) -> bool:
    if int(h.headers.get("Content-Length") or 0) > limit:
        h._json(413, {"error": {"message": "corps trop grand"}})
        return True
    return False


def _machine_relay(h: Any, c: CloudLink, machine: Any, method: str, path: str) -> None:
    """Côté machine de la boîte aux lettres (lot X10) : la machine dépose ses cartes fortes, relève les preuves, rend
    son verdict ; elle lit aussi les clés publiques des appareils de son compte pour vérifier ces preuves elle-même. Le
    jeton de machine ne décide jamais : aucune de ces routes ne pose `approved` sans que la machine l'ait vérifié."""
    from nokker.cloud.approvals import RelayError

    if c.approvals_relay is None:
        h._json(404, {"error": {"message": "approbations relayées non configurées"}})
        return
    try:
        if path == "/v1/gateway/devices" and method == "GET":
            from nokker.approvals.base import LOGIN_ONLY_VIA

            devices = c.devices_of(machine.account) if c.devices_of is not None else []
            h._json(200, {"devices": [
                {"device_id": d.id, "public_key_spki": d.public_key_spki, "status": d.status, "name": d.name,
                 # appareil de simple connexion (code e-mail, Google) : ouvre une session, ne décide jamais
                 "can_decide": d.status == "approved" and not (d.approved_via or "").startswith(LOGIN_ONLY_VIA)}
                for d in devices if d.user == machine.account]})
            return
        if path == "/v1/gateway/approvals/decisions" and method == "GET":
            if not c.rate_ok(f"relay-poll:{machine.id}"):
                h._json(429, {"error": "rate", "retry_after_s": int(RATE_WINDOW_S)})
                return
            from urllib.parse import parse_qs, urlsplit

            try:
                wait = float((parse_qs(urlsplit(h.path).query).get("wait") or ["25"])[0])
            except ValueError:
                wait = 25.0
            h._json(200, {"decisions": [
                {"approval_id": a.approval_id, "decision": a.decision, "proof": a.proof, "challenge": a.challenge,
                 "decided_at": a.decided_at} for a in c.approvals_relay.decisions(machine, wait)]})
            return
        if method != "POST":
            h._json(404, {"error": {"message": "not found"}})
            return
        from nokker.cloud.approvals import MAX_ARGS_BYTES

        # un dépôt peut porter les arguments d'une carte relayée (X20c) : un corps de mail, 256 Kio au plus
        if _too_big(h, MAX_RELAY_BYTES + (MAX_ARGS_BYTES if path == "/v1/gateway/approvals" else 0)):
            return
        if not c.rate_ok(f"relay:{machine.id}"):
            h._json(429, {"error": "rate", "retry_after_s": int(RATE_WINDOW_S)})
            return
        body = h._read_json_body()
        body = body if isinstance(body, dict) else {}
        m = _RE_MACHINE_RESULT.match(path)
        if m:
            from nokker.cloud.approvals import view

            h._json(200, view(c.approvals_relay.result(machine, m.group(1), body), time.time()))
            return
        if path != "/v1/gateway/approvals":
            h._json(404, {"error": {"message": "not found"}})
            return
        card, new = c.approvals_relay.deposit(machine, body)
        if new and c.notify_approval is not None:
            notify = c.notify_approval

            def _send() -> None:
                try:
                    notify(machine, card)
                except Exception:  # noqa: BLE001 — une notification ratée ne fait pas échouer le dépôt
                    logger.exception("notification de la carte relayée %s en échec", card.approval_id)

            threading.Thread(target=_send, name="nokker-relay-notify", daemon=True).start()
        h._json(201 if new else 200, {"approval_id": card.approval_id, "status": card.status,
                                      "url": f"{c.links.base_url.rstrip('/')}/approve/{machine.id}/{card.approval_id}"})
    except RelayError as e:
        h._json(e.status, {"error": {"message": str(e)}})


def _account_relay(h: Any, c: CloudLink, method: str, m: Any) -> None:
    """Côté compte (page https://app.nokker.ai/approve/<machine_id>/<approval_id>) : lire les cartes d'une de SES
    machines, transmettre une décision signée par un appareil. nokker.ai garde la preuve, ne la croit pas (202
    `forwarded`) : c'est la machine qui la vérifie et rend le statut final. Machine d'un autre compte : 404."""
    from nokker.cloud.approvals import RelayError

    account = c.identity(h)
    tenant = c.tenant_of(account) if account else None
    if not account or not tenant:
        h._json(401, {"error": {"message": "session du compte requise"}})
        return
    machine_id, approval_id, decide = m.group(1), m.group(2), bool(m.group(3))
    if c.approvals_relay is None or c.links.machine_of(account, machine_id) is None:
        h._json(404, {"error": {"message": "not found"}})
        return
    try:
        if method == "GET" and not decide:
            if approval_id is None:
                h._json(200, {"approvals": c.approvals_relay.list(tenant, machine_id)})
                return
            found = c.approvals_relay.get(tenant, machine_id, approval_id)
            h._json(200, found) if found else h._json(404, {"error": {"message": "carte inconnue"}})
            return
        if method == "POST" and decide:
            if _too_big(h):
                return
            if not c.rate_ok(f"relay-decide:{account}"):
                h._json(429, {"error": "rate", "retry_after_s": int(RATE_WINDOW_S)})
                return
            body = h._read_json_body()
            c.approvals_relay.decide(tenant, machine_id, approval_id, body if isinstance(body, dict) else {})
            h._json(202, {"status": "forwarded"})
            return
        h._json(404, {"error": {"message": "not found"}})
    except RelayError as e:
        h._json(e.status, {"error": {"message": str(e)}})


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
    machine. Flux SSE relayé tel quel quand `stream` est demandé.

    Gardes (revue du train du 2.11, décision 1), dans cet ordre, avant tout appel au Router : taille (413), débit par
    machine (429), modèle de la config du noyau (400, absent = celui de la config), plafonds du compte (403
    `quota_exceeded`) : appels par jour, et un appel qui OUVRE un tour (dernier message `user`) compte comme un tour du
    web (`TenantLimits.check_turn`, mêmes plafonds, ceux de l'abonnement s'il y en a un)."""
    from nokker.tenancy.context import tenant_scope

    length = int(h.headers.get("Content-Length") or 0)
    if length > MAX_INFERENCE_BYTES:
        h._json(413, {"error": {"message": "requête trop grande"}})
        return
    if not c.rate_ok(f"inference:{machine.id}", c.inference_rate_max):
        h._json(429, {"error": {"code": "rate", "message": "trop d'appels pour cette machine"},
                      "retry_after_s": int(RATE_WINDOW_S)})
        return
    raw = h.rfile.read(length) if length else b"{}"
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        h._json(400, {"error": {"message": "invalid JSON"}})
        return
    if not isinstance(body, dict):
        h._json(400, {"error": {"message": "objet JSON attendu"}})
        return
    model = str(body.get("model") or c.default_model or "")
    if c.models and model not in c.models:
        h._json(400, {"error": {"code": "model_not_allowed",
                                "message": f"modèle non proposé par nokker.ai : {model or '(aucun)'} ; "
                                           f"modèles : {', '.join(sorted(c.models))}"}})
        return
    if model and body.get("model") != model:
        body["model"] = model
        raw = json.dumps(body).encode()
    with tenant_scope(machine.tenant):
        refused = _inference_quota(c, machine, body)
    if refused is not None:
        h._json(403, {"error": {"code": "quota_exceeded", "message": refused}})
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
            # Dans le locataire de la machine : le ledger Postgres (RLS) lève NoTenant sans lui.
            with tenant_scope(machine.tenant):
                c.record_usage(machine.tenant, machine.id, usage, time.monotonic() - t0, model)
        except Exception:  # noqa: BLE001 — la réponse est déjà partie : on ne la casse pas, on le dit fort
            logger.error("comptage de l'inférence relayée en échec (machine %s, locataire %s) : appel NON compté",
                         machine.id, machine.tenant, exc_info=True)


def _opens_turn(body: dict[str, Any]) -> bool:
    """Un appel ouvre un tour quand son dernier message (hors système) vient de l'utilisateur ; la suite d'un tour
    finit par un résultat d'outil ou une réponse de l'assistant."""
    msgs = [m for m in (body.get("messages") or []) if isinstance(m, dict) and m.get("role") != "system"]
    return not msgs or msgs[-1].get("role") == "user"


def _inference_quota(c: CloudLink, machine: Any, body: dict[str, Any]) -> str | None:
    """None : l'appel passe (et il est compté). Sinon le message du refus. Appelé dans `tenant_scope(machine.tenant)`.
    Le locataire interne n'a pas de plafond (comme ses tours)."""
    from nokker.tenancy.context import INTERNAL_TENANT
    from nokker.tenancy.limits import day_window

    lim = c.limits
    if lim is None or machine.tenant == INTERNAL_TENANT:
        return None
    day = day_window(lim.clock())[0].date()
    calls_key = f"{machine.account}#inference"  # compteur à part, même table : jamais mêlé aux tours du compte
    if not lim.counter.take(machine.tenant, calls_key, day, "turns", c.inference_calls_per_day):
        return (f"Limite du jour atteinte pour l'inférence de Nokker Lokkal : {c.inference_calls_per_day} appels par "
                "jour et par compte. Elle repart avec le compteur du jour.")
    if _opens_turn(body):
        verdict = lim.check_turn(machine.account)
        if verdict is not None:
            lim.counter.give_back(machine.tenant, calls_key, day, "turns")
            logger.info("inférence relayée refusée (%s) : %s", verdict.rule, verdict.reason)
            return lim.reply(verdict)
    return None


def _last_usage(chunk: bytes) -> dict[str, Any] | None:
    for line in reversed(chunk.split(b"\n")):
        if line.startswith(b"data: ") and b'"usage"' in line:
            try:
                return json.loads(line[6:]).get("usage")
            except json.JSONDecodeError:
                return None
    return None


__all__ = ["DEVICE_POLL_RATE_MAX", "GATEWAY_RATE_MAX", "GATEWAY_STATES", "GATEWAY_TOKEN_RATE_MAX", "LINK_CODE_FAILS_MAX",
           "CloudLink", "configure", "gateway_state", "handle"]
