"""Relais de la passerelle `nokker.app` (docs/GATEWAY.md, docs/LOKKAL.md §7.5).

Un Lokkal ouvre une connexion SORTANTE `wss://relay.nokker.app/v1/tunnel` (jeton de machine) ; le relais publie
`<nom>.nokker.app` et `<nom>--pages.nokker.app` et fait passer les requêtes du navigateur dans ce tunnel, en trames
JSON (`req` / `res` / `body` / `cancel` / `ping`).

Le relais filtre AVANT la machine : sur `/v1/*`, seul un jeton d'accès ou de délégation de nokker.ai, signé, du MÊME
compte que la machine, passe ; hors `/v1`, GET/HEAD seulement (fichiers de l'interface) ; jamais de jeton local ;
cookies retirés dans les deux sens. Pages : origine séparée, CSP `sandbox` + `frame-ancestors`, entrée par un jeton de
pages à usage unique (`/_auth`), puis cookie `__Host-nk_pg` (onglet) ou capacité d'URL `/_s/<cap>/` (iframe, `embed=1` :
pas de cookie tiers, qui ne passe plus une fois nokker.app sur la PSL ni depuis nokker.ai).

Connexion du navigateur (LOKKAL §7.3) : `/_gw/token|refresh|logout` sur `<nom>.nokker.app`, traités par le relais
SANS tunnel ; le renouvellement vit dans `__Host-nk_rt` (posé par le relais seul) et ne passe jamais au Lokkal.

État en mémoire (une réplique en v1) : la table nom → tunnel, les jti de pages consommés, le compteur de débit de
`/_gw`. Rien sur disque.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import itertools
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import aiohttp
import jwt
from aiohttp import web

log = logging.getLogger("relay")

SUBPROTOCOL = "nokker-tunnel.v1"
MAX_FRAME = 1 << 20
MAX_BODY = 50 << 20
CHUNK = 256 << 10
MAX_IN_FLIGHT = 64
RESPONSE_TIMEOUT = 60
DELEGATION_RESPONSE_TIMEOUT = 900   # délégation (X21) : chat/completions synchrone d'un agent, jusqu'à 15 min
STREAM_IDLE = 3600
RECHECK_EVERY = 600
PAGE_RE = re.compile(r"^[A-Za-z0-9._~-]+(/[A-Za-z0-9._~-]+)*$")
NAME_RE = re.compile(r"^(?!-)(?!.*--)[a-z0-9-]{3,30}(?<!-)$")
HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "te", "trailer",
       "proxy-authorization", "proxy-authenticate", "host", "content-length"}
STRIP_REQ = HOP | {"cookie", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip",
                   "x-nokker-relay", "x-nokker-account", "x-nokker-local", "forwarded", "cf-connecting-ip",
                   "cf-ray", "cf-visitor", "cf-ipcountry", "cdn-loop", "cf-access-jwt-assertion"}
STRIP_RES = HOP | {"set-cookie"}
PAGES_CSP = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"
PAGES_TOKEN_MAX_TTL = 60          # jeton de pages : exp − iat (BUBBLES §10.3)
CAP_TTL = 3600                    # capacité d'URL des iframes
EXPIRED_HTML = ('<!doctype html><meta charset="utf-8"><title>Lien expiré</title>'
                '<p style="font:14px system-ui;color:#666;padding:1em">Lien expiré : rouvrez la page.</p>'
                '<script>try{parent.postMessage({type:"nokker:pages-expired"},"*")}catch(e){}</script>')
RT_COOKIE = "__Host-nk_rt"        # renouvellement de la connexion par la passerelle (§7.3)
RT_MAX_AGE = 30 * 86400
GW_BODY_MAX = 8 << 10
GW_RATE = (20, 60.0)              # /_gw : 20 requêtes par minute et par IP


@dataclass
class Machine:
    machine_id: str
    name: str
    tenant: str
    inference: bool


@dataclass
class Settings:
    domain: str = "nokker.app"
    relay_host: str = "relay.nokker.app"
    core_url: str = "https://app.nokker.ai"            # noyau de nokker.ai : /v1/gateway/machine
    jwks_url: str = "https://app.nokker.ai/.well-known/jwks.json"
    issuer: str = "https://app.nokker.ai"
    audiences: tuple[str, ...] = ("nokker.app",)       # jetons d'accès et de délégation (LOKKAL §7.5)
    cookie_secret: bytes = b""                          # cookies des pages (HMAC), propre au relais
    frame_ancestors: str = "https://nokker.ai https://app.nokker.ai"  # + https://<nom>.nokker.app
    pages_accept_access_token: bool = True              # /_auth avec un jeton d'accès : toléré jusqu'au train du 2.11

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        secret = env.get("NOKKER_RELAY_COOKIE_SECRET", "")
        if len(secret) < 32:
            raise SystemExit("NOKKER_RELAY_COOKIE_SECRET absent ou trop court (32 caractères au moins)")
        return cls(domain=env.get("NOKKER_GATEWAY_DOMAIN", cls.domain),
                   relay_host=env.get("NOKKER_RELAY_HOST", cls.relay_host),
                   core_url=env.get("NOKKER_CORE_URL", cls.core_url).rstrip("/"),
                   jwks_url=env.get("NOKKER_JWKS_URL", cls.jwks_url),
                   issuer=env.get("NOKKER_TOKEN_ISSUER", cls.issuer),
                   audiences=tuple(a for a in env.get("NOKKER_TOKEN_AUDIENCES", "nokker.app").split(",") if a),
                   cookie_secret=secret.encode(),
                   frame_ancestors=env.get("NOKKER_PAGES_FRAME_ANCESTORS", cls.frame_ancestors),
                   pages_accept_access_token=env.get("NOKKER_PAGES_ACCEPT_ACCESS_TOKEN", "1") == "1")


# --------------------------------------------------------------------------------------------- identités
MachineCheck = Callable[[str], Awaitable[Machine | None]]
TokenCheck = Callable[[str], Awaitable[dict | None]]
CorePost = Callable[..., Awaitable[tuple[int, Any]]]   # (chemin, corps, client_ip=…) → (statut, JSON) ; 0 = injoignable
Presence = Callable[[str, bool], Awaitable[None]]               # (jeton de machine, en ligne)


class CoreClient:
    """Les deux questions que le relais pose à nokker.ai : quelle machine porte ce jeton ; ce jeton d'accès est-il signé."""

    def __init__(self, s: Settings, session: aiohttp.ClientSession):
        self.s, self.http = s, session
        self._jwks = jwt.PyJWKClient(s.jwks_url, cache_keys=True, lifespan=900) if s.jwks_url else None

    async def machine(self, token: str) -> Machine | None:
        try:
            async with self.http.get(f"{self.s.core_url}/v1/gateway/machine",
                                     headers={"Authorization": f"Bearer {token}"},
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                d = await r.json()
                return Machine(str(d["machine_id"]), str(d["name"]), str(d["tenant"]), bool(d.get("inference")))
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as e:
            log.warning("vérification de machine impossible : %s", e)
            return None

    async def presence(self, token: str, online: bool) -> None:
        """Présence du tunnel (X15) : nokker.ai marque la machine en ligne / hors ligne sans attendre le prochain
        passage. Au mieux : un échec n'affecte pas le tunnel."""
        try:
            async with self.http.post(f"{self.s.core_url}/v1/gateway/presence", json={"online": online},
                                      headers={"Authorization": f"Bearer {token}"},
                                      timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status >= 300:
                    log.debug("présence refusée : %s", r.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.debug("présence impossible : %s", type(e).__name__)

    async def post(self, path: str, body: dict, client_ip: str = "") -> tuple[int, Any]:
        """Appel du relais lui-même à nokker.ai (connexion par la passerelle, §7.3). `client_ip` : l'IP du navigateur
        (CF-Connecting-IP reçu de Cloudflare), transmise pour que les limites de débit du noyau soient par IP et pas
        celles de l'adresse du relais ; le noyau n'est joignable que par cloudflared et le relais (NetworkPolicy)."""
        headers = {"CF-Connecting-IP": client_ip} if client_ip else {}
        try:
            async with self.http.post(f"{self.s.core_url}{path}", json=body, headers=headers,
                                      timeout=aiohttp.ClientTimeout(total=10)) as r:
                try:
                    data = await r.json(content_type=None)
                except ValueError:
                    data = None
                return r.status, data
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("noyau injoignable (%s) : %s", path, type(e).__name__)
            return 0, None

    async def token(self, tok: str) -> dict | None:
        if not self._jwks:
            return None
        try:
            key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, tok)
            return jwt.decode(tok, key.key, algorithms=["ES256", "EdDSA", "RS256"], issuer=self.s.issuer,
                              audience=list(self.s.audiences), options={"require": ["exp", "iss", "aud"]},
                              leeway=30)
        except Exception as e:  # noqa: BLE001 — jeton illisible, mal signé, expiré : même réponse
            log.info("jeton refusé : %s", type(e).__name__)
            return None


def claims_tenant(c: dict) -> str | None:
    return c.get("tenant") or c.get("ten") or c.get("sub")


# --------------------------------------------------------------------------------------------- tunnel
class TunnelClosed(Exception):
    pass


@dataclass
class Stream:
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)


class Tunnel:
    def __init__(self, machine: Machine, ws: web.WebSocketResponse, token: str):
        self.machine, self.ws, self.token = machine, ws, token
        self.streams: dict[int, Stream] = {}
        self.ids = itertools.count(1)
        self.slots = asyncio.Semaphore(MAX_IN_FLIGHT)
        self.closed = asyncio.Event()

    async def send(self, frame: dict) -> None:
        if self.ws.closed:
            raise TunnelClosed()
        await self.ws.send_str(json.dumps(frame, separators=(",", ":")))

    def deliver(self, frame: dict) -> None:
        st = self.streams.get(frame.get("id"))
        if st:
            st.queue.put_nowait(frame)

    def fail_all(self) -> None:
        self.closed.set()
        for st in self.streams.values():
            st.queue.put_nowait({"t": "closed"})


class Registry:
    def __init__(self):
        self.by_name: dict[str, Tunnel] = {}

    async def put(self, t: Tunnel) -> None:
        old = self.by_name.get(t.machine.name)
        self.by_name[t.machine.name] = t
        if old and not old.ws.closed:
            await old.ws.close(code=4001, message=b"remplace par une connexion plus recente")
            old.fail_all()

    def drop(self, t: Tunnel) -> bool:
        """Retire le tunnel ; True s'il était le tunnel courant (pas un tunnel déjà remplacé)."""
        current = self.by_name.get(t.machine.name) is t
        if current:
            del self.by_name[t.machine.name]
        t.fail_all()
        return current


# --------------------------------------------------------------------------------------------- application
def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def unb64(s: str | None) -> bytes:
    return base64.b64decode(s) if s else b""


def bearer(req: web.Request) -> str | None:
    h = req.headers.get("Authorization", "")
    return h[7:].strip() if h[:7].lower() == "bearer " else None


def looks_local(tok: str) -> bool:
    """Le jeton local (docs/LOKKAL.md §7.4.5) n'est pas un JWT : tout jeton qui n'a pas la forme a.b.c est refusé."""
    return tok.count(".") != 2


def sign_cookie(secret: bytes, tenant: str, name: str, exp: int) -> str:
    msg = f"{tenant}|{name}|{exp}"
    mac = hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{msg}|{mac}".encode()).decode().rstrip("=")


def check_cookie(secret: bytes, value: str, name: str) -> str | None:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        tenant, n, exp, mac = raw.rsplit("|", 3)
    except (ValueError, UnicodeDecodeError):
        return None
    good = hmac.new(secret, f"{tenant}|{n}|{exp}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, mac) or n != name or int(exp) < time.time():
        return None
    return tenant


def cap_key(secret: bytes) -> bytes:
    """Clé des capacités d'URL, séparée de celle des cookies : une capacité n'est pas un cookie, et l'inverse."""
    return hmac.new(secret, b"nokker-pages-cap", hashlib.sha256).digest()


class RateLimit:
    def __init__(self, n: int, per: float):
        self.n, self.per, self.hits = n, per, {}

    def ok(self, key: str) -> bool:
        now = time.monotonic()
        if len(self.hits) > 10000:
            self.hits = {k: v for k, v in self.hits.items() if v and v[-1] > now - self.per}
        h = [t for t in self.hits.get(key, []) if t > now - self.per]
        if len(h) >= self.n:
            self.hits[key] = h
            return False
        h.append(now)
        self.hits[key] = h
        return True


class Relay:
    def __init__(self, s: Settings, check_machine: MachineCheck, check_token: TokenCheck,
                 core_post: CorePost | None = None, presence: Presence | None = None):
        self.s = s
        self.check_machine, self.check_token, self.core_post = check_machine, check_token, core_post
        self.presence = presence
        self._bg: set[asyncio.Task] = set()
        self.reg = Registry()
        self.used_jti: dict[str, float] = {}       # jti des jetons de pages consommés → expiration
        self.gw_rate = RateLimit(*GW_RATE)

    # ---- routage par Host
    def parse_host(self, host: str) -> tuple[str, str] | None:
        host = host.split(":", 1)[0].lower()
        if host == self.s.relay_host:
            return ("relay", "")
        suffix = "." + self.s.domain
        if not host.endswith(suffix):
            return None
        label = host[: -len(suffix)]
        if "." in label:
            return None
        if label.endswith("--pages"):
            name = label[: -len("--pages")]
            return ("pages", name) if NAME_RE.match(name) else None
        return ("app", label) if NAME_RE.match(label) else None

    async def handle(self, req: web.Request) -> web.StreamResponse:
        where = self.parse_host(req.headers.get("Host", ""))
        if where is None:
            return web.Response(status=404, text="inconnu\n")
        kind, name = where
        if kind == "app" and (req.path == "/_gw" or req.path.startswith("/_gw/")):
            return await self.gw(req, name)      # avant la table des tunnels : marche machine hors ligne
        if kind == "relay":
            if req.path == "/healthz":
                return web.Response(text="ok\n")
            if req.path == "/v1/tunnel":
                return await self.tunnel(req)
            return web.Response(status=404, text="inconnu\n")
        t = self.reg.by_name.get(name)
        if t is None:
            return web.Response(status=502, text="machine hors ligne\n", headers={"Cache-Control": "no-store"})
        if kind == "pages":
            return await self.pages(req, t)
        return await self.app(req, t)

    # ---- tunnel entrant
    async def tunnel(self, req: web.Request) -> web.StreamResponse:
        tok = bearer(req)
        if not tok:
            return web.Response(status=401, text="jeton de machine requis\n")
        m = await self.check_machine(tok)
        if m is None:
            return web.Response(status=401, text="jeton de machine refusé\n")
        if not m.inference:
            return web.Response(status=403, text="passerelle réservée aux comptes avec inférence Nokker\n")
        if not NAME_RE.match(m.name):
            return web.Response(status=403, text="nom de machine invalide\n")
        ws = web.WebSocketResponse(protocols=(SUBPROTOCOL,), heartbeat=None, max_msg_size=MAX_FRAME,
                                   autoping=True)
        await ws.prepare(req)
        if ws.ws_protocol != SUBPROTOCOL:
            await ws.close(code=4000, message=b"sous-protocole nokker-tunnel.v1 attendu")
            return ws
        t = Tunnel(m, ws, tok)
        await self.reg.put(t)
        log.info("tunnel ouvert : %s (%s)", m.name, m.machine_id)
        self._signal(tok, True)
        keep = asyncio.create_task(self._keepalive(t))
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    f = json.loads(msg.data)
                except ValueError:
                    continue
                if f.get("t") == "ping":
                    await t.send({"t": "pong"})
                elif f.get("t") in ("res", "body", "cancel"):
                    t.deliver(f)
        finally:
            keep.cancel()
            if self.reg.drop(t):                 # remplacé par une connexion plus récente : toujours en ligne
                self._signal(tok, False)
            log.info("tunnel fermé : %s", m.name)
        return ws

    def _signal(self, token: str, online: bool) -> None:
        if self.presence is None:
            return
        task = asyncio.create_task(self.presence(token, online))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _keepalive(self, t: Tunnel) -> None:
        """Ping toutes les 25 s ; revérifie le compte toutes les 10 min (inférence, révocation, nom)."""
        last = time.monotonic()
        while not t.ws.closed:
            await asyncio.sleep(25)
            try:
                await t.send({"t": "ping"})
            except (TunnelClosed, ConnectionError):
                return
            if time.monotonic() - last >= RECHECK_EVERY:
                last = time.monotonic()
                m = await self.check_machine(t.token)
                if m is None or not m.inference or m.name != t.machine.name:
                    log.info("tunnel %s fermé : compte ou jeton plus valable", t.machine.name)
                    await t.ws.close(code=4003, message=b"machine ou compte plus autorise")
                    return

    # ---- requêtes du navigateur
    async def app(self, req: web.Request, t: Tunnel) -> web.StreamResponse:
        tenant = None
        if req.path.startswith("/v1/") or req.path == "/v1":
            tok = bearer(req)
            if not tok or looks_local(tok) or req.headers.get("X-Nokker-Local"):
                return web.json_response({"error": {"message": "unauthorized"}}, status=401)
            claims = await self.check_token(tok)
            tenant = claims_tenant(claims) if claims else None
            if tenant != t.machine.tenant or (claims.get("machine") not in (None, t.machine.machine_id)):
                return web.json_response({"error": {"message": "unauthorized"}}, status=401)
            if claims.get("scope") == "lokkal:session" or claims.get("delegation"):
                return await self.forward(req, t, req.path_qs, tenant, extra={},
                                          response_timeout=DELEGATION_RESPONSE_TIMEOUT)
        elif req.method not in ("GET", "HEAD"):
            return web.Response(status=405, text="méthode refusée\n")
        return await self.forward(req, t, req.path_qs, tenant, extra={})

    def pages_headers(self, name: str) -> dict:
        fa = f"https://{name}.{self.s.domain} {self.s.frame_ancestors}".strip()
        return {"Content-Security-Policy": f"{PAGES_CSP}; frame-ancestors {fa}", "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
                "Cross-Origin-Resource-Policy": "same-origin"}

    def pages_entry(self, claims: dict | None, m: Machine) -> bool:
        """Jeton présenté à `/_auth`. Jeton de pages (scope `pages`) : cette machine, ≤ 60 s, usage unique par jti.
        Jeton d'accès (sans scope) : toléré tant que `pages_accept_access_token` (jusqu'au 2.11)."""
        if not claims or claims_tenant(claims) != m.tenant:
            return False
        scope = claims.get("scope")
        if scope is None:
            return self.s.pages_accept_access_token and claims.get("machine") in (None, m.machine_id)
        if scope != "pages" or claims.get("machine") != m.machine_id:
            return False
        try:
            iat, exp, jti = int(claims["iat"]), int(claims["exp"]), str(claims["jti"])
        except (KeyError, TypeError, ValueError):
            return False
        if not jti or exp - iat > PAGES_TOKEN_MAX_TTL:
            return False
        now = time.time()
        if len(self.used_jti) > 1000:
            self.used_jti = {j: e for j, e in self.used_jti.items() if e > now}
        if jti in self.used_jti:
            log.info("jeton de pages rejoué (machine %s)", m.name)
            return False
        self.used_jti[jti] = exp + 60
        return True

    async def pages(self, req: web.Request, t: Tunnel) -> web.StreamResponse:
        if req.method not in ("GET", "HEAD"):
            return web.Response(status=405, text="méthode refusée\n")
        host = f"{t.machine.name}--pages.{self.s.domain}"
        if req.path == "/_auth":
            # Lien signé par nokker.ai : ?token=<jeton de pages>&to=/<page>[&embed=1].
            if not self.pages_entry(await self.check_token(req.query.get("token", "")), t.machine):
                return web.Response(status=401, text="lien expiré ou invalide\n", headers={"Cache-Control": "no-store"})
            to = req.query.get("to", "/")
            if not to.startswith("/") or to.startswith("//") or "\\" in to:
                to = "/"
            if req.query.get("embed") == "1":
                # iframe : capacité dans le chemin, sans cookie (tiers une fois sur la PSL, et depuis nokker.ai).
                cap = sign_cookie(cap_key(self.s.cookie_secret), t.machine.tenant, t.machine.name,
                                  int(time.time()) + CAP_TTL)
                r = web.HTTPFound(location=f"/_s/{cap}{to}")
            else:
                exp = int(time.time()) + 12 * 3600
                r = web.HTTPFound(location=to)
                r.set_cookie("__Host-nk_pg", sign_cookie(self.s.cookie_secret, t.machine.tenant, t.machine.name, exp),
                             secure=True, httponly=True, samesite="Lax", path="/", max_age=12 * 3600)
            r.headers["Cache-Control"] = "no-store"
            r.headers["Referrer-Policy"] = "no-referrer"
            return r
        prefix = None
        if req.path.startswith("/_s/"):
            cap, _, rest = req.path[len("/_s/"):].partition("/")
            tenant = check_cookie(cap_key(self.s.cookie_secret), cap, t.machine.name)
            if tenant is None:
                # L'iframe est d'une autre origine : le client ne voit pas ce 401. La page le lui dit (postMessage,
                # sans secret), il redemande un lien et remonte l'iframe.
                return web.Response(status=401, body=EXPIRED_HTML, content_type="text/html", charset="utf-8",
                                    headers=self.pages_headers(t.machine.name))
            page, prefix = rest, f"/_s/{cap}"
        else:
            tenant = check_cookie(self.s.cookie_secret, req.cookies.get("__Host-nk_pg", ""), t.machine.name)
            page = req.path.lstrip("/")
        if not PAGE_RE.match(page) or any(seg in ("", ".", "..") for seg in page.split("/")):
            return web.Response(status=404, text="page inconnue\n")
        # Sans preuve, on demande quand même au noyau : il sert les pages PUBLIQUES et refuse les autres.
        path = "/v1/pages/" + page + (("?" + req.query_string) if req.query_string else "")
        return await self.forward(req, t, path, tenant, extra=self.pages_headers(t.machine.name), pages_host=host,
                                  location_prefix=prefix)

    # ---- connexion du navigateur par la passerelle (LOKKAL §7.3), sans tunnel
    async def gw(self, req: web.Request, name: str) -> web.StreamResponse:
        host = f"{name}.{self.s.domain}"
        ns = {"Cache-Control": "no-store"}
        action = req.path[len("/_gw/"):] if req.path.startswith("/_gw/") else ""
        if action not in ("token", "refresh", "logout"):
            return web.Response(status=404, text="inconnu\n", headers=ns)
        if req.method != "POST":
            return web.Response(status=405, text="méthode refusée\n", headers=ns)
        # CSRF : origine exacte ET JSON (requête préalable CORS forcée hors même origine) ; SameSite=Strict en plus.
        if req.headers.get("Origin") != f"https://{host}" or req.content_type != "application/json":
            return web.json_response({"error": "forbidden"}, status=403, headers=ns)
        if req.content_length is not None and req.content_length > GW_BODY_MAX:
            return web.json_response({"error": "too_large"}, status=413, headers=ns)
        if self.core_post is None:
            return web.json_response({"error": "unavailable"}, status=503, headers=ns)
        ip = req.headers.get("CF-Connecting-IP", req.remote or "")
        if action != "logout" and not self.gw_rate.ok(ip):
            return web.json_response({"error": "rate_limited"}, status=429, headers={**ns, "Retry-After": "60"})
        rt = req.cookies.get(RT_COOKIE, "")
        if action == "logout":
            if rt:
                await self.core_post("/v1/gateway/revoke", {"refresh_token": rt}, client_ip=ip)
            r = web.Response(status=204, headers=ns)
            self._clear_rt(r)
            return r
        if action == "token":
            try:
                body = await req.json()
                code, verifier = body["code"], body["code_verifier"]
                if not (isinstance(code, str) and isinstance(verifier, str) and code and verifier):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                return web.json_response({"error": "invalid_request"}, status=400, headers=ns)
            st, data = await self.core_post("/v1/gateway/token",
                                            {"code": code, "code_verifier": verifier, "host": host}, client_ip=ip)
            return self._grant(st, data, clear_on_refusal=False)
        if not rt:
            return web.json_response({"error": "invalid_grant"}, status=401, headers=ns)
        st, data = await self.core_post("/v1/gateway/refresh", {"refresh_token": rt, "host": host}, client_ip=ip)
        return self._grant(st, data, clear_on_refusal=True)

    def _grant(self, st: int, data: Any, clear_on_refusal: bool) -> web.Response:
        ns = {"Cache-Control": "no-store"}
        if st == 200 and isinstance(data, dict) and data.get("access_token") and data.get("refresh_token"):
            r = web.json_response({"access_token": data["access_token"], "expires_in": data.get("expires_in", 900)},
                                  headers=ns)
            r.set_cookie(RT_COOKIE, str(data["refresh_token"]), secure=True, httponly=True, samesite="Strict",
                         path="/", max_age=RT_MAX_AGE)
            return r
        if st in (400, 401):
            # Refus net de nokker.ai (invalid_grant) : seul cas où le renouvellement est jeté.
            r = web.json_response({"error": "invalid_grant"}, status=401, headers=ns)
            if clear_on_refusal:
                self._clear_rt(r)
            return r
        log.warning("connexion par la passerelle : nokker.ai a répondu %s", st or "rien")
        return web.json_response({"error": "upstream"}, status=502, headers=ns)   # cookie gardé

    @staticmethod
    def _clear_rt(r: web.StreamResponse) -> None:
        r.del_cookie(RT_COOKIE, path="/", secure=True, httponly=True, samesite="Strict")

    async def forward(self, req: web.Request, t: Tunnel, path: str, tenant: str | None, extra: dict,
                      pages_host: str | None = None, location_prefix: str | None = None,
                      response_timeout: float | None = None) -> web.StreamResponse:
        if t.closed.is_set():
            return web.Response(status=502, text="machine hors ligne\n")
        if req.content_length and req.content_length > MAX_BODY:
            return web.Response(status=413, text="corps trop gros\n")
        headers = [[k, v] for k, v in req.headers.items() if k.lower() not in STRIP_REQ]
        headers += [["Host", f"{t.machine.name}.{self.s.domain}" if pages_host is None else pages_host],
                    ["X-Nokker-Relay", "1"],
                    ["X-Forwarded-For", req.headers.get("CF-Connecting-IP", req.remote or "")],
                    ["X-Forwarded-Proto", "https"]]
        if tenant:
            headers.append(["X-Nokker-Account", tenant])
        if pages_host is not None:
            headers.append(["X-Nokker-Pages", "1"])
        try:
            await asyncio.wait_for(t.slots.acquire(), 5)
        except asyncio.TimeoutError:
            return web.Response(status=503, text="machine occupée\n", headers={"Retry-After": "2"})
        sid = next(t.ids)
        st = t.streams[sid] = Stream()
        resp: web.StreamResponse | None = None
        try:
            # Corps en flux, trame par trame : jamais tout le corps en mémoire (4 images de 5 Mo ≈ 27 Mo par message, pod
            # à 512 Mi). Une trame d'avance pour savoir si c'est la dernière (`more`).
            started, size, prev = False, 0, None

            async def emit(chunk: bytes, more: bool) -> None:
                nonlocal started
                if not started:
                    started = True
                    await t.send({"t": "req", "id": sid, "method": req.method, "path": path, "headers": headers,
                                  "body": b64(chunk), "more": more})
                else:
                    await t.send({"t": "body", "id": sid, "data": b64(chunk), "more": more})
            async for data in req.content.iter_chunked(CHUNK):
                size += len(data)
                if size > MAX_BODY:                 # sans Content-Length (chunked) : coupé en route
                    if started:
                        await self._cancel(t, sid)
                    return web.Response(status=413, text="corps trop gros\n")
                if prev is not None:
                    await emit(prev, True)
                prev = data
            await emit(prev or b"", False)
            f = await asyncio.wait_for(st.queue.get(), response_timeout or RESPONSE_TIMEOUT)
            if f.get("t") != "res":
                return web.Response(status=502, text="machine injoignable\n")
            out = {k: v for k, v in f.get("headers", []) if k.lower() not in STRIP_RES}
            if location_prefix:
                for k, v in list(out.items()):      # redirection relative du noyau : reste sous la capacité
                    if k.lower() == "location" and v.startswith("/") and not v.startswith("//"):
                        out[k] = location_prefix + v
            out.update(extra)
            status = int(f.get("status", 502))
            if not f.get("more"):
                return web.Response(status=status, body=unb64(f.get("body")), headers=out)
            resp = web.StreamResponse(status=status, headers=out)
            await resp.prepare(req)
            if f.get("body"):
                await resp.write(unb64(f["body"]))
            while True:
                f = await asyncio.wait_for(st.queue.get(), STREAM_IDLE)
                if f.get("t") == "body":
                    if f.get("data"):
                        await resp.write(unb64(f["data"]))
                    if not f.get("more"):
                        break
                else:
                    break
            await resp.write_eof()
            return resp
        except asyncio.TimeoutError:
            await self._cancel(t, sid)
            if resp is not None:
                return resp
            return web.Response(status=504, text="la machine ne répond pas\n")
        except (ConnectionResetError, asyncio.CancelledError):
            await self._cancel(t, sid)
            raise
        except TunnelClosed:
            return resp or web.Response(status=502, text="machine hors ligne\n")
        finally:
            t.streams.pop(sid, None)
            t.slots.release()

    async def _cancel(self, t: Tunnel, sid: int) -> None:
        try:
            await t.send({"t": "cancel", "id": sid})
        except (TunnelClosed, ConnectionError):
            pass


def make_app(relay: Relay) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY)
    app.router.add_route("*", "/{tail:.*}", relay.handle)
    return app


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    s = Settings.from_env()
    http = aiohttp.ClientSession()
    core = CoreClient(s, http)
    relay = Relay(s, core.machine, core.token, core.post, core.presence)
    runner = web.AppRunner(make_app(relay), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", "8080"))).start()
    log.info("relais %s : *.%s", s.relay_host, s.domain)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
