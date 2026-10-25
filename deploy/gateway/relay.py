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

Machine éteinte (tunnel fermé) : `<nom>--pages.nokker.app` sert la COPIE d'une page que l'utilisateur a demandé de
garder en miroir (abonnés, docs/LOKKAL.md §7.23), avec les mêmes preuves (jeton de pages, cookie, capacité) et les
mêmes en-têtes ; le relais vérifie la preuve PUIS demande la copie à nokker.ai avec sa clé (`NOKKER_RELAY_CORE_KEY`,
absente = rien servi depuis le miroir, 502 comme avant). Sans preuve : 404, ni la machine ni nokker.ai ne sont
interrogés (une page privée ne se sert jamais par son id seul).

Pages publiques (docs/LOKKAL.md §7.26) : `<nom>--pub.nokker.app/<id public>`, une AUTRE origine et un AUTRE id que la
page privée (jamais le jeton, la capacité ou le cookie d'une page privée). GET/HEAD seulement, aucun cookie lu ni posé,
rien du navigateur ne passe à la machine (ni cookie, ni jeton, ni IP) ; en-têtes stricts (CSP sans source externe,
`connect-src 'none'`, `form-action 'none'`, `frame-ancestors 'none'`, `nosniff`, `no-referrer`, `noindex` sauf choix
de l'utilisateur), un lien de signalement ajouté par le relais, plafond de débit par page et par IP. Machine en ligne :
servie par la machine si nokker.ai dit que le compte y a droit (`pages_public`) ; éteinte : la copie publique gardée en
miroir, demandée à nokker.ai avec la clé du relais. Modération (§7.26, point 6) : avant de servir une page publique
rendue par la machine, le relais demande au registre de nokker.ai (`POST /v1/gateway/public-page/check`, clé du relais,
avec le `page_id` que la machine dit dans `X-Nokker-Page-Id`) ; retirée par l'opérateur → 410, pour la copie du miroir
aussi. Sans clé du relais (registre illisible) ou nokker.ai injoignable : rien n'est servi (échec fermé).

Cookies : tous `__Host-` (Secure, Path=/, jamais d'attribut Domain : tant que nokker.app n'est pas sur la Public
Suffix List, un cookie de domaine serait lu par toutes les passerelles) ; `/_gw` exige l'origine exacte et
Sec-Fetch-Site = same-origin ; le cookie de pages ne vaut que pour une navigation de document.

État en mémoire : la table nom → tunnel, les compteurs de débit. Les jti de pages consommés : en mémoire (une
réplique, défaut) ou chez nokker.ai (`NOKKER_RELAY_JTI_STORE=core`, plusieurs répliques). Rien sur disque.
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
from html import escape
from typing import Any, Awaitable, Callable
from urllib.parse import quote

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
PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{24}$")   # id public d'une page (≠ page_id), tiré au hasard à la publication
PUBLIC_MAX = 6 << 20              # corps d'une page publique (5 Mio de page + marge)
PUBLIC_RATE_IP = (60, 60.0)       # par page et par IP : 60 vues par minute
PUBLIC_RATE_PAGE = (1200, 60.0)   # par page, toutes IP : 1200 vues par minute
# Pages publiques : leur propre JS en ligne seulement, aucune source externe, aucun appel réseau, aucun formulaire,
# jamais dans un cadre ; origine opaque (sandbox sans allow-same-origin). Rien de ce que le relais ajoute n'est un script.
PUBLIC_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
              "font-src data:; media-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'; "
              "frame-ancestors 'none'; sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox")
PUBLIC_PERMISSIONS = ("camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), bluetooth=(), "
                      "hid=(), clipboard-read=(), display-capture=(), publickey-credentials-get=()")


@dataclass
class Machine:
    machine_id: str
    name: str
    tenant: str
    inference: bool
    pages_public: bool = False          # le compte peut rendre des pages publiques (nokker.ai le dit, §7.26)


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
    core_key: str = ""                                  # clé du relais pour lire les pages copiées (miroir) ; "" = éteint
    report_url: str = "https://nokker.ai/report"        # lien de signalement ajouté aux pages publiques
    jti_store: str = "memory"                           # jti des jetons de pages : memory (une réplique) | core (partagé)

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        secret = env.get("NOKKER_RELAY_COOKIE_SECRET", "")
        if len(secret) < 32:
            raise SystemExit("NOKKER_RELAY_COOKIE_SECRET absent ou trop court (32 caractères au moins)")
        core_key = env.get("NOKKER_RELAY_CORE_KEY", "")
        if 0 < len(core_key) < 32:  # une clé tronquée dans le Secret se lirait sinon comme « miroir éteint »
            log.warning("NOKKER_RELAY_CORE_KEY fait %d caractères (32 au moins) : pages du miroir DÉSACTIVÉES",
                        len(core_key))
        core_key = core_key if len(core_key) >= 32 else ""
        jti_store = env.get("NOKKER_RELAY_JTI_STORE", "memory")
        if jti_store not in ("memory", "core"):
            raise SystemExit("NOKKER_RELAY_JTI_STORE : memory ou core")
        if jti_store == "core" and not core_key:
            raise SystemExit("NOKKER_RELAY_JTI_STORE=core exige NOKKER_RELAY_CORE_KEY (32 caractères au moins)")
        return cls(domain=env.get("NOKKER_GATEWAY_DOMAIN", cls.domain),
                   relay_host=env.get("NOKKER_RELAY_HOST", cls.relay_host),
                   core_url=env.get("NOKKER_CORE_URL", cls.core_url).rstrip("/"),
                   jwks_url=env.get("NOKKER_JWKS_URL", cls.jwks_url),
                   issuer=env.get("NOKKER_TOKEN_ISSUER", cls.issuer),
                   audiences=tuple(a for a in env.get("NOKKER_TOKEN_AUDIENCES", "nokker.app").split(",") if a),
                   cookie_secret=secret.encode(),
                   frame_ancestors=env.get("NOKKER_PAGES_FRAME_ANCESTORS", cls.frame_ancestors),
                   pages_accept_access_token=env.get("NOKKER_PAGES_ACCEPT_ACCESS_TOKEN", "1") == "1",
                   core_key=core_key, jti_store=jti_store,
                   report_url=env.get("NOKKER_PUBLIC_REPORT_URL", cls.report_url))


# --------------------------------------------------------------------------------------------- identités
MachineCheck = Callable[[str], Awaitable[Machine | None]]
TokenCheck = Callable[[str], Awaitable[dict | None]]
CorePost = Callable[..., Awaitable[tuple[int, Any]]]   # (chemin, corps, client_ip=…) → (statut, JSON) ; 0 = injoignable
Presence = Callable[[str, bool], Awaitable[None]]               # (jeton de machine, en ligne)
MirrorPage = Callable[[dict], Awaitable[tuple[int, Any]]]       # {host, tenant, machine?, page_id?} → (statut, JSON)
MirrorPublic = Callable[[dict], Awaitable[tuple[int, Any]]]     # {host, public_id} → (statut, JSON)
PublicCheck = Callable[[dict], Awaitable[tuple[int, Any]]]      # {host, public_id, page_id, size} → (statut, JSON)
PAGE_ID_RE = re.compile(r"^pg_[0-9a-f]{16}$")


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
                return Machine(str(d["machine_id"]), str(d["name"]), str(d["tenant"]), bool(d.get("inference")),
                               pages_public=d.get("pages_public") is True)
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

    async def relay_post(self, path: str, body: dict, timeout: float = 20) -> tuple[int, Any]:
        """Question du relais à nokker.ai avec SA clé (`X-Nokker-Relay-Key`) : copies du miroir, registre des jti ;
        (0, None) si injoignable."""
        try:
            async with self.http.post(f"{self.s.core_url}{path}", json=body,
                                      headers={"X-Nokker-Relay-Key": self.s.core_key},
                                      timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                try:
                    return r.status, await r.json(content_type=None)
                except ValueError:
                    return r.status, None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("nokker.ai injoignable (%s) : %s", path, type(e).__name__)
            return 0, None

    async def mirror_page(self, body: dict) -> tuple[int, Any]:
        """Copie d'une page gardée en miroir (machine éteinte), avec la clé du relais ; (0, None) si injoignable."""
        return await self.relay_post("/v1/gateway/mirror/page", body)

    async def mirror_public(self, body: dict) -> tuple[int, Any]:
        """Copie PUBLIQUE d'une page (machine éteinte) : `{host, public_id}` → `{page}` ; (0, None) si injoignable."""
        return await self.relay_post("/v1/gateway/mirror/public-page", body)

    async def public_check(self, body: dict) -> tuple[int, Any]:
        """Registre de modération : `{host, public_id, page_id, size}` → 200 servie | 410 retirée | 404 ; (0, None) si
        injoignable."""
        return await self.relay_post("/v1/gateway/public-page/check", body, timeout=5)

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


# --------------------------------------------------------------------------------------------- jti consommés
class MemoryUsedTokens:
    """Jti des jetons de pages déjà présentés, en mémoire du processus : le comportement d'une seule réplique."""

    def __init__(self) -> None:
        self.seen: dict[str, float] = {}           # jti → expiration (+ 60 s de marge)

    async def first_use(self, jti: str, exp: float) -> bool:
        now = time.time()
        if len(self.seen) > 1000:
            self.seen = {j: e for j, e in self.seen.items() if e > now}
        if jti in self.seen:
            return False
        self.seen[jti] = exp + 60
        return True


class CoreUsedTokens:
    """Registre partagé entre répliques : nokker.ai le tient (Postgres, `POST /v1/gateway/pages-jti`, clé du relais).
    `200 {first: true}` = premier usage ; `409` = rejoué ; toute autre réponse (nokker.ai injoignable compris) = refus :
    un jeton qu'on ne peut pas prouver neuf ne passe pas."""

    def __init__(self, post: Callable[[dict], Awaitable[tuple[int, Any]]]) -> None:
        self.post = post

    async def first_use(self, jti: str, exp: float) -> bool:
        st, data = await self.post({"jti": jti, "exp": int(exp)})
        if st == 200 and isinstance(data, dict) and data.get("first") is True:
            return True
        if st != 409:
            log.warning("registre des jetons de pages : réponse %s, jeton refusé", st or "aucune")
        return False


def set_host_cookie(r: web.StreamResponse, name: str, value: str, *, samesite: str, max_age: int) -> None:
    """Le seul chemin par lequel le relais pose un cookie : préfixe `__Host-` (le navigateur refuse alors tout attribut
    Domain et exige Secure + Path=/), HttpOnly. Jamais de cookie de domaine `nokker.app`."""
    if not name.startswith("__Host-"):
        raise ValueError(f"cookie sans préfixe __Host- refusé : {name}")
    r.set_cookie(name, value, secure=True, httponly=True, samesite=samesite, path="/", max_age=max_age)


def with_report(html: bytes, href: str) -> bytes:
    """Lien de signalement d'une page publique, ajouté par le relais (docs/BUBBLES.md §10.4) : un lien, aucun script,
    avant le dernier `</body>` (sinon à la fin)."""
    a = (f'<a data-nokker-report href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer" '
         'style="position:fixed;right:8px;bottom:8px;z-index:2147483647;padding:3px 8px;'
         'font:12px/1.4 system-ui,sans-serif;background:#fff;color:#444;border:1px solid #bbb;border-radius:4px;'
         'text-decoration:none">Signaler cette page</a>').encode()
    last = None
    for last in re.finditer(rb"</body\s*>", html, re.IGNORECASE):
        pass
    return html[:last.start()] + a + html[last.start():] if last else html + a


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
                 core_post: CorePost | None = None, presence: Presence | None = None,
                 mirror_page: MirrorPage | None = None, mirror_public: MirrorPublic | None = None,
                 used_tokens: Any = None, public_check: PublicCheck | None = None):
        self.s = s
        self.mirror_page, self.mirror_public = mirror_page, mirror_public
        self.public_check = public_check  # registre de modération de nokker.ai ; None = aucune page publique servie
        self.check_machine, self.check_token, self.core_post = check_machine, check_token, core_post
        self.presence = presence
        self._bg: set[asyncio.Task] = set()
        self.reg = Registry()
        # jti des jetons de pages consommés : mémoire (une réplique) ou registre partagé de nokker.ai
        self.used_tokens = used_tokens if used_tokens is not None else MemoryUsedTokens()
        self.gw_rate = RateLimit(*GW_RATE)
        self.pub_ip_rate = RateLimit(*PUBLIC_RATE_IP)
        self.pub_page_rate = RateLimit(*PUBLIC_RATE_PAGE)

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
        if label.endswith("--pub"):
            name = label[: -len("--pub")]
            return ("pub", name) if NAME_RE.match(name) else None
        return ("app", label) if NAME_RE.match(label) else None

    async def handle(self, req: web.Request) -> web.StreamResponse:
        where = self.parse_host(req.headers.get("Host", ""))
        if where is None:
            return web.Response(status=404, text="inconnu\n")
        kind, name = where
        if kind == "app" and (req.path == "/_gw" or req.path.startswith("/_gw/")):
            return await self.gw(req, name)      # avant la table des tunnels : marche machine hors ligne
        if kind == "pub":
            return await self.public(req, name)  # machine en ligne ou copie publique : décidé dedans
        if kind == "relay":
            if req.path == "/healthz":
                return web.Response(text="ok\n")
            if req.path == "/v1/tunnel":
                return await self.tunnel(req)
            return web.Response(status=404, text="inconnu\n")
        t = self.reg.by_name.get(name)
        if t is None:
            if kind == "pages" and self.mirror_page is not None:
                return await self.pages_offline(req, name)
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
                t.machine.pages_public = m.pages_public  # droit de publier retiré : ses pages publiques tombent

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

    async def pages_entry(self, claims: dict | None, m: Machine) -> bool:
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
        if not await self.used_tokens.first_use(jti, exp):
            log.info("jeton de pages rejoué ou non vérifiable (machine %s)", m.name)
            return False
        return True

    def tab_cookie(self, req: web.Request, name: str) -> str | None:
        """Locataire prouvé par le cookie d'onglet `__Host-nk_pg`, seulement pour une navigation de document : tant que
        nokker.app n'est pas sur la Public Suffix List, une autre passerelle est « même site » et le cookie (Lax)
        partirait avec ses sous-ressources et ses cadres. Un navigateur sans Sec-Fetch-Dest reste accepté."""
        if req.headers.get("Sec-Fetch-Dest", "document") != "document":
            return None
        return check_cookie(self.s.cookie_secret, req.cookies.get("__Host-nk_pg", ""), name)

    def _pages_enter(self, req: web.Request, m: Machine) -> web.StreamResponse:
        """Entrée par `/_auth` acceptée : capacité d'URL (iframe, `embed=1`) ou cookie d'onglet `__Host-nk_pg`."""
        to = req.query.get("to", "/")
        if not to.startswith("/") or to.startswith("//") or "\\" in to:
            to = "/"
        if req.query.get("embed") == "1":
            # iframe : capacité dans le chemin, sans cookie (tiers une fois sur la PSL, et depuis nokker.ai).
            cap = sign_cookie(cap_key(self.s.cookie_secret), m.tenant, m.name, int(time.time()) + CAP_TTL)
            r = web.HTTPFound(location=f"/_s/{cap}{to}")
        else:
            exp = int(time.time()) + 12 * 3600
            r = web.HTTPFound(location=to)
            set_host_cookie(r, "__Host-nk_pg", sign_cookie(self.s.cookie_secret, m.tenant, m.name, exp),
                            samesite="Lax", max_age=12 * 3600)
        r.headers["Cache-Control"] = "no-store"
        r.headers["Referrer-Policy"] = "no-referrer"
        return r

    async def pages_offline(self, req: web.Request, name: str) -> web.StreamResponse:
        """Machine éteinte : la copie gardée en miroir, jamais sans preuve (docs/LOKKAL.md §7.23). Mêmes preuves et mêmes
        en-têtes qu'en ligne ; nokker.ai vérifie que le nom est une machine active du locataire prouvé (et la machine du
        jeton), que le compte a le miroir, et rend la copie avec son bandeau. Tout refus : « machine hors ligne »."""
        offline = web.Response(status=502, text="machine hors ligne\n", headers={"Cache-Control": "no-store"})
        if req.method not in ("GET", "HEAD"):
            return web.Response(status=405, text="méthode refusée\n")
        host = f"{name}.{self.s.domain}"
        if req.path == "/_auth":
            claims = await self.check_token(req.query.get("token", ""))
            tenant = claims_tenant(claims) if claims else None
            if not tenant or claims.get("scope") != "pages" or not claims.get("machine"):
                return web.Response(status=401, text="lien expiré ou invalide\n", headers={"Cache-Control": "no-store"})
            st, data = await self.mirror_page({"host": host, "tenant": tenant, "machine": claims["machine"]})
            if st != 200 or not isinstance(data, dict) or not data.get("machine_id"):
                return offline
            m = Machine(str(data["machine_id"]), name, tenant, True)
            if not await self.pages_entry(claims, m):
                return web.Response(status=401, text="lien expiré ou invalide\n", headers={"Cache-Control": "no-store"})
            return self._pages_enter(req, m)
        if req.path.startswith("/_s/"):
            cap, _, page = req.path[len("/_s/"):].partition("/")
            tenant = check_cookie(cap_key(self.s.cookie_secret), cap, name)
            if tenant is None:
                return web.Response(status=401, body=EXPIRED_HTML, content_type="text/html", charset="utf-8",
                                    headers=self.pages_headers(name))
        else:
            tenant = self.tab_cookie(req, name)
            page = req.path.lstrip("/")
        if not PAGE_ID_RE.match(page):
            return web.Response(status=404, text="page inconnue\n")
        if tenant is None:
            return offline  # sans preuve : rien depuis le miroir (une page publique a son adresse, `--pub`)
        st, data = await self.mirror_page({"host": host, "tenant": tenant, "page_id": page})
        copy = data.get("page") if st == 200 and isinstance(data, dict) else None
        if not isinstance(copy, dict):
            return offline
        try:
            html = base64.b64decode(str(copy.get("html_b64") or ""), validate=True)
            at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(copy.get("mirrored_at") or 0)))
        except (ValueError, TypeError, OverflowError):
            return offline
        headers = {**self.pages_headers(name), "Content-Type": "text/html; charset=utf-8", "X-Nokker-Mirror-Copy": at}
        if req.query.get("dl") == "1":
            title = re.sub(r"[^A-Za-z0-9 ._()-]+", "", str(copy.get("title") or ""))[:80].strip() or page
            headers["Content-Disposition"] = f'attachment; filename="{title}.html"'
        return web.Response(status=200, body=html, headers=headers)

    async def pages(self, req: web.Request, t: Tunnel) -> web.StreamResponse:
        if req.method not in ("GET", "HEAD"):
            return web.Response(status=405, text="méthode refusée\n")
        host = f"{t.machine.name}--pages.{self.s.domain}"
        if req.path == "/_auth":
            # Lien signé par nokker.ai : ?token=<jeton de pages>&to=/<page>[&embed=1].
            if not await self.pages_entry(await self.check_token(req.query.get("token", "")), t.machine):
                return web.Response(status=401, text="lien expiré ou invalide\n", headers={"Cache-Control": "no-store"})
            return self._pages_enter(req, t.machine)
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
            tenant = self.tab_cookie(req, t.machine.name)
            page = req.path.lstrip("/")
        if not PAGE_RE.match(page) or any(seg in ("", ".", "..") for seg in page.split("/")):
            return web.Response(status=404, text="page inconnue\n")
        if tenant is None:
            # Sans preuve : rien. Une page publique a sa propre adresse (`<nom>--pub`, un autre id) ; une page privée
            # ne se sert jamais par son id seul, la machine n'est même pas interrogée.
            return web.Response(status=404, text="page inconnue\n", headers={"Cache-Control": "no-store"})
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
        # CSRF : origine exacte ET JSON (requête préalable CORS forcée hors même origine) ; SameSite=Strict en plus, qui
        # ne protège pas d'une passerelle voisine tant que nokker.app n'est pas sur la PSL (même site) : d'où l'origine
        # exacte, et Sec-Fetch-Site = same-origin quand le navigateur l'envoie.
        if req.headers.get("Origin") != f"https://{host}" or req.content_type != "application/json" \
                or req.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
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
            set_host_cookie(r, RT_COOKIE, str(data["refresh_token"]), samesite="Strict", max_age=RT_MAX_AGE)
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

    # ---- pages publiques (docs/LOKKAL.md §7.26)
    def public_headers(self, index: bool) -> dict:
        h = {"Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": PUBLIC_CSP,
             "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
             "X-Frame-Options": "DENY", "Cross-Origin-Resource-Policy": "same-origin",
             "Cross-Origin-Opener-Policy": "same-origin", "Permissions-Policy": PUBLIC_PERMISSIONS}
        if not index:
            h["X-Robots-Tag"] = "noindex, nofollow"     # défaut : pas dans les moteurs, sauf choix de l'utilisateur
        return h

    async def public(self, req: web.Request, name: str) -> web.StreamResponse:
        """`<nom>--pub.nokker.app/<id public>` : sans preuve par nature, donc sans rien du navigateur vers la machine
        (ni cookie, ni jeton, ni IP), aucun cookie posé, en-têtes stricts, lien de signalement, plafond de débit."""
        ns = {"Cache-Control": "no-store"}
        not_found = web.Response(status=404, text="page introuvable\n", headers=ns)
        if req.method not in ("GET", "HEAD"):
            return web.Response(status=405, text="méthode refusée\n", headers=ns)
        pid = req.path[1:]
        if not PUBLIC_ID_RE.match(pid):
            return not_found
        ip = req.headers.get("CF-Connecting-IP", req.remote or "")
        if not self.pub_ip_rate.ok(f"{ip}|{name}/{pid}") or not self.pub_page_rate.ok(f"{name}/{pid}"):
            return web.Response(status=429, text="trop de requêtes\n", headers={**ns, "Retry-After": "60"})
        host = f"{name}--pub.{self.s.domain}"
        t = self.reg.by_name.get(name)
        copy_at = None
        gone = web.Response(status=410, text="page retirée par la modération\n", headers=ns)
        if t is not None:
            if not t.machine.pages_public:
                return not_found                       # le compte n'a pas (ou plus) le droit : la machine n'est pas vue
            if self.public_check is None:
                return not_found                       # registre de modération illisible : échec fermé
            st, hdrs, html = await self.ask(t, f"/v1/pages/public/{pid}",
                                            [["Host", host], ["X-Nokker-Relay", "1"], ["X-Nokker-Public", "1"]])
            if st == 404:
                return not_found
            if st != 200 or html is None:
                return web.Response(status=502, text="page indisponible\n", headers=ns)
            page_id = hdrs.get("x-nokker-page-id")
            cst, _ = await self.public_check({"host": f"{name}.{self.s.domain}", "public_id": pid,
                                              "page_id": page_id if PAGE_ID_RE.match(page_id or "") else None,
                                              "size": len(html)})
            if cst == 410:
                return gone
            if cst == 404:
                return not_found
            if cst != 200:
                return web.Response(status=502, text="page indisponible\n", headers=ns)
            index = hdrs.get("x-nokker-index") == "1"
        else:
            if self.mirror_public is None:
                return web.Response(status=502, text="machine hors ligne\n", headers=ns)
            st, data = await self.mirror_public({"host": f"{name}.{self.s.domain}", "public_id": pid})
            if st == 410:
                return gone
            page = data.get("page") if st == 200 and isinstance(data, dict) else None
            if not isinstance(page, dict):
                return web.Response(status=502, text="machine hors ligne\n", headers=ns)
            try:
                html = base64.b64decode(str(page.get("html_b64") or ""), validate=True)
                copy_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(page.get("mirrored_at") or 0)))
            except (ValueError, TypeError, OverflowError):
                return web.Response(status=502, text="machine hors ligne\n", headers=ns)
            index = page.get("index") is True
        report = f"{self.s.report_url}?url={quote(f'https://{host}/{pid}', safe='')}"
        headers = self.public_headers(index)
        if copy_at:
            headers["X-Nokker-Mirror-Copy"] = copy_at
        return web.Response(status=200, body=with_report(html, report), headers=headers)

    async def ask(self, t: Tunnel, path: str, headers: list) -> tuple[int, dict, bytes | None]:
        """GET sans corps vers la machine, réponse lue en entier (≤ PUBLIC_MAX) : `(statut, en-têtes en minuscules,
        corps)`, ou `(0, {}, None)` (tunnel fermé, machine muette, corps trop gros)."""
        if t.closed.is_set():
            return 0, {}, None
        try:
            await asyncio.wait_for(t.slots.acquire(), 5)
        except asyncio.TimeoutError:
            return 0, {}, None
        sid = next(t.ids)
        st = t.streams[sid] = Stream()
        try:
            await t.send({"t": "req", "id": sid, "method": "GET", "path": path, "headers": headers, "body": "",
                          "more": False})
            f = await asyncio.wait_for(st.queue.get(), RESPONSE_TIMEOUT)
            if f.get("t") != "res":
                return 0, {}, None
            status, out = int(f.get("status", 502)), {str(k).lower(): v for k, v in f.get("headers", [])}
            buf, more = bytearray(unb64(f.get("body"))), f.get("more")
            while more and len(buf) <= PUBLIC_MAX:
                f = await asyncio.wait_for(st.queue.get(), RESPONSE_TIMEOUT)
                if f.get("t") != "body":
                    return 0, {}, None
                buf += unb64(f.get("data"))
                more = f.get("more")
            if len(buf) > PUBLIC_MAX:
                await self._cancel(t, sid)
                return 0, {}, None
            return status, out, bytes(buf)
        except asyncio.TimeoutError:
            await self._cancel(t, sid)
            return 0, {}, None
        except TunnelClosed:
            return 0, {}, None
        finally:
            t.streams.pop(sid, None)
            t.slots.release()

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
    used = CoreUsedTokens(lambda b: core.relay_post("/v1/gateway/pages-jti", b, timeout=5)) \
        if s.jti_store == "core" else None
    relay = Relay(s, core.machine, core.token, core.post, core.presence,
                  mirror_page=core.mirror_page if s.core_key else None,
                  mirror_public=core.mirror_public if s.core_key else None, used_tokens=used,
                  public_check=core.public_check if s.core_key else None)
    if not s.core_key:
        log.warning("NOKKER_RELAY_CORE_KEY absente : registre de modération illisible, aucune page publique servie")
    runner = web.AppRunner(make_app(relay), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", "8080"))).start()
    log.info("relais %s : *.%s", s.relay_host, s.domain)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
