"""nokker/previews/ssrf.py — garde SSRF stricte pour le service d'aperçus (docs/BUBBLES.md §5).

Règles (consigne P1) :
  - schémas `http`/`https` **seulement** ;
  - résolution DNS puis **refus des IP privées/loopback/link-local/CGNAT (100.64.0.0/10,
    Tailscale !)/multicast/IPv6 ULA (fc00::/7)/réservées** ;
  - re-vérification à **chaque redirection** (le module ne suit rien lui-même : `fetch.py` boucle
    et rappelle `resolve_and_check` à chaque `Location`, ≤ 3 fois) ;
  - **connexion pinglée à l'IP déjà vérifiée** (pas de seconde résolution exploitable par DNS
    rebinding) — `PinnedBackend` force `connect_tcp` vers cette IP, le SNI / la vérification TLS
    continuent de porter sur le VRAI nom d'hôte (`httpcore.HTTPConnection._connect` lit le SNI
    séparément du `network_backend`, voir sa source : `server_hostname=self._origin.host`).

Note sur les formes décimales/octales/hexadécimales (`http://2130706433/` == 127.0.0.1) : rien de
spécial à coder — `socket.getaddrinfo` (glibc) les résout lui-même en IP avant toute requête DNS,
donc elles passent par la MÊME classification que n'importe quelle IP littérale.

Mode strict (`ssrf.strict` de la config, ÉTEINT par défaut, chantier p2/ssrf-audit) — éteint, rien ne change ici :
  - adresses IPv6 qui EMBARQUENT une IPv4 (NAT64 64:ff9b::/96, 6to4 2002::/16, Teredo 2001::/32) classées d'après
    l'IPv4 qu'elles portent, site-local IPv6 déprécié (fec0::/10) refusé ;
  - port illisible = refus (au lieu d'une `ValueError`), et liste de ports pour les appelants qui la demandent
    (`enforce_ports=True` : récupération web, serveurs MCP ajoutés, compétences par URL) ;
  - liste d'autorisation EXPLICITE `hôte:port` (Lokkal : un serveur MCP sur la machine, un service nommé) : l'hôte
    listé peut résoudre vers loopback, RFC1918, ULA ou CGNAT ; jamais vers link-local (métadonnées cloud),
    multicast, non spécifiée ou réservée. La connexion reste épinglée à l'IP vérifiée.
Le réseau du foyer ne passe pas par ici : il passe par home-bridge (API interne nommée, docs/LOKKAL-HOME-BRIDGE.md).

Sortie par mandataire (chantier p2/core-via-proxy, docs/LOKKAL-CORE-EGRESS.md) — automatique, sans drapeau : quand
`HTTPS_PROXY` (ou `https_proxy`, `http://` seulement) est posé et que l'hôte n'est pas couvert par `NO_PROXY`, aucune
résolution locale (Lokkal : core n'a plus de DNS ni de route) ; la garde reste NOMINALE côté noyau (schéma, ports du mode
strict, hôte qui est une IP littérale interdite, toutes ses écritures numériques) et la connexion est un tunnel
`CONNECT hôte:port` ouvert vers le mandataire, TLS et SNI sur le vrai nom. Le refus des IP RÉSOLUES privées est délégué
au mandataire : `egress` (deploy/lokkal/sandboxd/egress.py) résout lui-même, refuse toute adresse privée, loopback,
link-local, CGNAT ou ULA, puis se connecte à l'IP qu'il vient de vérifier — même garantie anti-rebinding que l'épinglage
local, prise un saut plus loin. Son 403 est retraduit dans le refus d'avant (`IP refusée (…) pour hôte : ip`). Sans
mandataire (ou hôte en `NO_PROXY`, ou entrée de la liste d'autorisation du mode strict) : chemin épinglé, inchangé.
Un mandataire qui ne refuserait PAS le privé affaiblirait la garde : ne poser `HTTPS_PROXY` que vers un tel mandataire.
"""
from __future__ import annotations

import base64
import ipaddress
import logging
import re
import socket
import ssl
import urllib.request
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import httpcore
import httpx

MAX_REDIRECTS = 3
TIMEOUT_S = 5.0
_DEFAULT_PORTS = {"http": 80, "https": 443}

# CGNAT (100.64.0.0/10, « shared address space », RFC 6598) — c'est l'espace Tailscale. Vérifié
# explicitement : ne pas supposer que `IPv4Address.is_private` le couvre sur toutes les versions
# de Python visées par ce dépôt.
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
# ULA IPv6 (fc00::/7, RFC 4193) — l'équivalent IPv6 de RFC1918, PAS couvert par `is_private`
# partout non plus (certaines versions ne listent que fec0::/10 déprécié).
_ULA_V6 = ipaddress.ip_network("fc00::/7")
# Mode strict : préfixes IPv6 qui portent une IPv4 (RFC 6052, RFC 3056, RFC 4380) et site-local déprécié (RFC 3879).
_NAT64_V6 = ipaddress.ip_network("64:ff9b::/96")
_SIXTOFOUR_V6 = ipaddress.ip_network("2002::/16")
_TEREDO_V6 = ipaddress.ip_network("2001::/32")
_SITE_LOCAL_V6 = ipaddress.ip_network("fec0::/10")
# Ports par défaut du mode strict (récupérations web, MCP ajoutés, compétences par URL).
DEFAULT_STRICT_PORTS = frozenset({80, 443, 8080, 8443})
# Raisons qu'une entrée de la liste d'autorisation peut lever : le réseau privé, jamais les métadonnées cloud.
_ALLOWABLE = frozenset({"loopback", "privée (RFC1918)", "privée", "ULA (fc00::/7)", "CGNAT (100.64.0.0/10, Tailscale)"})

log = logging.getLogger("nokker.ssrf")


class SsrfBlocked(RuntimeError):
    """URL refusée par la garde SSRF (schéma, IP interdite, DNS en échec, hôte absent, …)."""


IpAddr = ipaddress.IPv4Address | ipaddress.IPv6Address


def blocked_reason(ip: IpAddr) -> str | None:
    """Étiquette courte si *ip* est interdite, sinon `None`. Les adresses IPv4 encodées en IPv6
    (`::ffff:a.b.c.d`) sont démappées avant classification — sinon elles passeraient à travers les
    contrôles `is_private`/CGNAT qui ne regardent que la forme IPv4."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "non spécifiée"
    if ip.is_reserved:
        return "réservée"
    if isinstance(ip, ipaddress.IPv4Address):
        if ip in _CGNAT_V4:
            return "CGNAT (100.64.0.0/10, Tailscale)"
        if ip.is_private:
            return "privée (RFC1918)"
    else:
        if ip in _ULA_V6:
            return "ULA (fc00::/7)"
        if ip.is_private:
            return "privée"
    return None


def _embedded_v4(ip: IpAddr) -> tuple[str, ipaddress.IPv4Address] | None:
    """IPv4 portée par une IPv6 de traduction/tunnel (NAT64, 6to4, Teredo), sinon `None`."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return None
    if ip in _NAT64_V6:
        return "NAT64", ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _SIXTOFOUR_V6:
        return "6to4", ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
    if ip in _TEREDO_V6:
        return "Teredo", ipaddress.IPv4Address(~int(ip) & 0xFFFFFFFF)
    return None


def strict_blocked_reason(ip: IpAddr) -> str | None:
    """`blocked_reason` + ce que le mode strict ajoute (IPv4 embarquée, site-local IPv6). L'IPv4 embarquée est classée
    D'ABORD (message identique quelle que soit la version de Python, dont la table `is_private`/`is_reserved` des
    préfixes de traduction varie) ; le mode strict ne relâche jamais un refus de `blocked_reason`."""
    embedded = _embedded_v4(ip)
    if embedded is not None:
        kind, v4 = embedded
        inner = blocked_reason(v4)
        if inner:
            return f"{kind} vers {v4}, {inner}"
    reason = blocked_reason(ip)
    if reason:
        return reason
    if isinstance(ip, ipaddress.IPv6Address) and ip in _SITE_LOCAL_V6:
        return "site-local (fec0::/10)"
    return None


def _host_key(host: str) -> str:
    return host.strip().strip("[]").rstrip(".").lower()


@dataclass(frozen=True)
class SsrfPolicy:
    """Politique réseau des clients sortants. `strict=False` (défaut) : comportement historique, octet pour octet.
    `allow` : couples (hôte, port) explicitement autorisés vers le réseau privé ; `ports` : ports permis aux appelants
    qui les vérifient (vide = tous)."""

    strict: bool = False
    allow: frozenset[tuple[str, int]] = frozenset()
    ports: frozenset[int] = DEFAULT_STRICT_PORTS

    @classmethod
    def from_config(cls, raw: object) -> SsrfPolicy:
        """Bloc `ssrf:` de la config : `{strict: bool, allow: ["hôte:port", …], ports: [int, …]}`. Une entrée illisible
        est ignorée avec un avis (jamais élargie)."""
        if not isinstance(raw, dict):
            return cls()
        allow: set[tuple[str, int]] = set()
        for item in raw.get("allow") or []:
            entry = parse_allow_entry(item)
            if entry is None:
                log.warning("ssrf.allow : entrée ignorée (hôte:port attendu) : %r", item)
            else:
                allow.add(entry)
        ports_raw = raw.get("ports")
        ports = DEFAULT_STRICT_PORTS
        if ports_raw is not None:
            ports = frozenset(p for p in ports_raw if isinstance(p, int) and not isinstance(p, bool) and 0 < p < 65536)
        return cls(strict=raw.get("strict") is True, allow=frozenset(allow), ports=ports)


def parse_allow_entry(item: object) -> tuple[str, int] | None:
    """`hôte:port` ou `[ipv6]:port` -> (hôte normalisé, port). Port obligatoire : une autorisation vise UN service."""
    if not isinstance(item, str) or not item.strip():
        return None
    try:
        parts = urlsplit("//" + item.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host or port is None or parts.path or parts.username or parts.password:
        return None
    return _host_key(host), port


_POLICY = SsrfPolicy()


def configure(policy: SsrfPolicy | dict | None) -> SsrfPolicy:
    """Pose la politique du processus (appelé au démarrage du noyau avec le bloc `ssrf:` de la config)."""
    global _POLICY
    _POLICY = policy if isinstance(policy, SsrfPolicy) else SsrfPolicy.from_config(policy)
    return _POLICY


def current_policy() -> SsrfPolicy:
    return _POLICY


@dataclass(frozen=True)
class ResolvedHop:
    """Une URL résolue et classée sûre : *ip* est l'adresse à laquelle on va réellement se
    connecter (pinglée), *hostname* reste le nom pour le SNI/Host/cert."""

    url: str
    scheme: str
    hostname: str
    port: int
    ip: str
    # Mandataire de sortie (`http://hôte:port`) quand la connexion passe par lui : *ip* vaut alors l'IP littérale de
    # l'URL, ou "" (pas de résolution locale). `None` : connexion directe épinglée à *ip* (comportement historique).
    proxy: str | None = None


def resolve_and_check(url: str, *, resolver=None, policy: SsrfPolicy | None = None,
                      enforce_ports: bool = False) -> ResolvedHop:
    """Résout *url* et lève `SsrfBlocked` si le schéma, l'hôte ou UNE SEULE des IP renvoyées par le
    résolveur est interdite — on n'ignore pas les IP interdites pour en garder une autre : un
    résolveur qui renvoie une IP publique ET une IP privée pour le même nom est lui-même un signal
    (DNS rebinding préparé), donc on refuse l'URL entière plutôt que de « choisir la bonne ».

    *policy* (défaut : celle du processus, `configure`) : hors mode strict, *enforce_ports* est sans effet et le
    comportement est l'historique, inchangé."""
    resolver = resolver or socket.getaddrinfo  # lu à l'appel (None = le résolveur du système)
    pol = policy if policy is not None else _POLICY
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise SsrfBlocked(f"schéma refusé : {scheme or '(absent)'}")
    hostname = parts.hostname or ""
    if not hostname:
        raise SsrfBlocked("URL sans hôte")
    if pol.strict:
        try:
            explicit_port = parts.port
        except ValueError:
            raise SsrfBlocked("port invalide dans l'URL") from None
        port = explicit_port or _DEFAULT_PORTS[scheme]
    else:
        port = parts.port or _DEFAULT_PORTS[scheme]
    allowed = pol.strict and (_host_key(hostname), port) in pol.allow
    if pol.strict and enforce_ports and pol.ports and port not in pol.ports and not allowed:
        raise SsrfBlocked(f"port {port} refusé (autorisés : {', '.join(map(str, sorted(pol.ports)))})")
    proxy = None if allowed else egress_proxy(scheme, hostname)
    if proxy is not None:
        return _nominal_hop(url, scheme, hostname, port, proxy, pol)
    try:
        infos = resolver(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SsrfBlocked(f"résolution DNS échouée pour {hostname} : {exc}") from exc
    if not infos:
        raise SsrfBlocked(f"résolution DNS vide pour {hostname}")
    classify = strict_blocked_reason if pol.strict else blocked_reason
    chosen_ip: str | None = None
    for _family, _socktype, _proto, _canon, sockaddr in infos:
        raw_ip = str(sockaddr[0]).split("%", 1)[0]  # retire une zone IPv6 (`fe80::1%eth0`)
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise SsrfBlocked(f"adresse renvoyée par le résolveur invalide : {raw_ip}") from exc
        reason = classify(ip)
        if reason and not (allowed and reason in _ALLOWABLE):
            raise SsrfBlocked(f"IP refusée ({reason}) pour {hostname} : {raw_ip}")
        if chosen_ip is None:
            chosen_ip = raw_ip
    assert chosen_ip is not None
    return ResolvedHop(url=url, scheme=scheme, hostname=hostname, port=port, ip=chosen_ip)


# ─────────────────────────────── sortie par mandataire (HTTPS_PROXY) ───────────────────────────────

_PROXY_HEAD_MAX = 8192
_PROXY_BODY_MAX = 4096
_HOSTNAME_OK = re.compile(r"^[a-z0-9_.-]+$")
_EGRESS_DENIED_IP = re.compile(r"→\s*(\S+)\s*$")


def egress_proxy(scheme: str, host: str) -> str | None:
    """Mandataire de sortie à emprunter pour *host*, ou `None` (connexion directe épinglée, comportement historique).
    Lu à chaque appel dans l'environnement (`HTTPS_PROXY`/`https_proxy`, `HTTP_PROXY` pour une URL `http` à défaut) ;
    `NO_PROXY` respecté (sémantique de `urllib`) ; seul un mandataire `http://` est pris en charge (tunnel CONNECT)."""
    env = urllib.request.getproxies_environment()
    proxy = env.get("https") if scheme == "https" else (env.get(scheme) or env.get("https"))
    if not proxy:
        return None
    try:
        parts = urlsplit(proxy)
        usable = parts.scheme.lower() == "http" and bool(parts.hostname)
        if usable:
            parts.port  # noqa: B018 — un port illisible lève ici, pas au moment de se connecter
    except ValueError:
        usable = False
    if not usable:
        log.warning("mandataire de sortie ignoré (http://hôte:port attendu)")
        return None
    if urllib.request.proxy_bypass_environment(_host_key(host), env):
        return None
    return proxy


def _literal_ip(host: str) -> IpAddr | None:
    """IP écrite dans l'URL, toutes formes que la libc accepte (`2130706433`, `0x7f.1`, `127.1`), sans résolution DNS."""
    h = _host_key(host).split("%", 1)[0]
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(h))
    except (OSError, ValueError):
        return None


def _nominal_hop(url: str, scheme: str, hostname: str, port: int, proxy: str, pol: SsrfPolicy) -> ResolvedHop:
    """Garde nominale du mode mandataire : aucune résolution locale. Une IP littérale interdite est refusée ici avec le
    message du chemin direct ; un nom part tel quel au mandataire, qui le résout et refuse le privé."""
    ip = _literal_ip(hostname)
    if ip is None:
        if not _HOSTNAME_OK.match(_idna(hostname)):
            raise SsrfBlocked(f"résolution DNS échouée pour {hostname} : nom invalide")
        return ResolvedHop(url=url, scheme=scheme, hostname=hostname, port=port, ip="", proxy=proxy)
    reason = (strict_blocked_reason if pol.strict else blocked_reason)(ip)
    if reason:
        raise SsrfBlocked(f"IP refusée ({reason}) pour {hostname} : {ip}")
    return ResolvedHop(url=url, scheme=scheme, hostname=hostname, port=port, ip=str(ip), proxy=proxy)


class ProxyRefused(httpcore.ProxyError):
    """Le mandataire a refusé la destination (403 : adresse interne ; 502 « nom introuvable »). Le message est celui du
    refus équivalent du chemin direct ; `proxy_refusal` le retrouve sous l'exception httpx qui l'enveloppe."""


def proxy_refusal(exc: BaseException | None) -> str | None:
    """Message du refus du mandataire porté par *exc* ou par ses causes, sinon `None`."""
    for _ in range(8):
        if exc is None:
            return None
        if isinstance(exc, ProxyRefused):
            return str(exc)
        exc = exc.__cause__ or exc.__context__
    return None


def _connect_request(hop: ResolvedHop) -> bytes:
    host = _idna(hop.hostname)
    authority = f"[{host}]:{hop.port}" if ":" in host else f"{host}:{hop.port}"
    lines = [f"CONNECT {authority} HTTP/1.1", f"Host: {authority}"]
    parts = urlsplit(hop.proxy or "")
    if parts.username is not None:
        cred = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
        lines.append("Proxy-Authorization: Basic " + base64.b64encode(cred).decode("ascii"))
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def _parse_head(head: bytes) -> tuple[int, int]:
    """(statut, Content-Length) de la réponse du mandataire ; `ProxyError` si elle est illisible."""
    lines = head.decode("latin-1").split("\r\n")
    try:
        status = int(lines[0].split(" ", 2)[1])
    except (IndexError, ValueError):
        raise httpcore.ProxyError("réponse du proxy de sortie illisible") from None
    length = 0
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length" and value.strip().isdigit():
            length = int(value.strip())
    return status, min(length, _PROXY_BODY_MAX)


def _proxy_failure(status: int, body: bytes, hop: ResolvedHop) -> tuple[str, str]:
    """(nature, message) d'une réponse non 2xx du mandataire. Un 403 d'`egress` (« destination interne refusée : h → ip »)
    redonne le refus du chemin direct, mot pour mot ; un 502 « nom introuvable », l'échec de résolution."""
    text = body.decode("utf-8", errors="replace").strip()
    if status == 403:
        m = _EGRESS_DENIED_IP.search(text)
        ip = _literal_ip(m.group(1)) if m else None
        reason = (strict_blocked_reason if _POLICY.strict else blocked_reason)(ip) if ip is not None else None
        if reason:
            return "refused", f"IP refusée ({reason}) pour {hop.hostname} : {ip}"
        return "refused", "destination refusée par le proxy de sortie (HTTP 403)"
    if status == 502 and text.startswith("nom introuvable"):
        return "refused", f"résolution DNS échouée pour {hop.hostname} : nom introuvable"
    if status == 504:
        return "timeout", "proxy de sortie : HTTP 504"
    return "error", f"proxy de sortie : HTTP {status}"


def _raise_failure(kind: str, message: str, errors) -> None:
    if kind == "refused":
        raise ProxyRefused(message) if errors is httpcore else errors.ProxyError(message)
    raise (errors.ConnectTimeout if kind == "timeout" else errors.ConnectError)(message)


def _recv_exact_head(recv) -> bytes:
    buf = b""
    while not buf.endswith(b"\r\n\r\n"):
        chunk = recv(1) if len(buf) < _PROXY_HEAD_MAX else b""
        if not chunk:
            raise httpcore.ProxyError("réponse du proxy de sortie illisible")
        buf += chunk
    return buf


def open_tunnel(hop: ResolvedHop, *, timeout_s: float = TIMEOUT_S) -> socket.socket:
    """Socket TCP tunnelé jusqu'à `hop.hostname:hop.port` par `CONNECT` sur `hop.proxy` (aucune résolution du nom de
    destination ici). Lève `ProxyRefused` (refus de politique), `httpcore.ConnectTimeout` ou `httpcore.ConnectError`.
    Le TLS (SNI, vérification du certificat sur `hop.hostname`) reste à la charge de l'appelant, comme en direct."""
    parts = urlsplit(hop.proxy or "")
    try:
        sock = socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout_s)
    except TimeoutError as exc:
        raise httpcore.ConnectTimeout(str(exc) or "timed out") from exc
    except OSError as exc:
        raise httpcore.ConnectError(str(exc)) from exc
    try:
        sock.sendall(_connect_request(hop))
        status, length = _parse_head(_recv_exact_head(sock.recv))
        if 200 <= status < 300:
            return sock
        body = b""
        while len(body) < length:
            chunk = sock.recv(length - len(body))
            if not chunk:
                break
            body += chunk
    except TimeoutError as exc:
        sock.close()
        raise httpcore.ConnectTimeout(str(exc) or "timed out") from exc
    except OSError as exc:
        sock.close()
        raise httpcore.ConnectError(str(exc)) from exc
    except BaseException:
        sock.close()
        raise
    sock.close()
    _raise_failure(*_proxy_failure(status, body, hop), httpcore)
    raise AssertionError("inatteignable")


class ProxyConnectBackend(httpcore.SyncBackend):
    """Pendant de `PinnedBackend` en mode mandataire : `connect_tcp` ignore l'hôte qu'on lui passe et ouvre TOUJOURS le
    tunnel vers l'hôte et le port vérifiés du hop. httpcore pose ensuite TLS dessus, SNI sur le vrai nom."""

    def __init__(self, hop: ResolvedHop) -> None:
        super().__init__()
        self._hop = hop

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        from httpcore._backends.sync import SyncStream

        return SyncStream(open_tunnel(self._hop, timeout_s=timeout or TIMEOUT_S))


class PinnedBackend(httpcore.SyncBackend):
    """`connect_tcp` ignore le nom d'hôte qu'on lui passe et connecte TOUJOURS à l'IP déjà
    vérifiée — fermé : aucune résolution n'a lieu entre `resolve_and_check` et la connexion
    réelle. Le SNI/la vérification du certificat ne passent pas par ici (httpcore les lit
    séparément depuis `Origin.host`, donc ils restent sur le vrai nom de domaine)."""

    def __init__(self, pinned_ip: str) -> None:
        super().__init__()
        self._pinned_ip = pinned_ip

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        return super().connect_tcp(self._pinned_ip, port, timeout=timeout, local_address=local_address,
                                   socket_options=socket_options)


class _PinnedTransport(httpx.HTTPTransport):
    """`httpx.HTTPTransport` construit son pool `httpcore` en interne et ne permet pas d'y glisser
    un `network_backend` personnalisé sur son propre constructeur (vérifié sur la version
    installée, 0.28.1 : `HTTPTransport.__init__` n'a pas ce paramètre, seul le `httpcore.
    ConnectionPool` qu'il enveloppe l'accepte) — on construit donc ce pool nous-mêmes, PAS
    `super().__init__()`. `handle_request`/`close` (hérités, inchangés) ne lisent que `self._pool`,
    donc rien d'autre à initialiser (vérifié dans `httpx._transports.default.HTTPTransport`)."""

    def __init__(self, pinned_ip: str, backend: httpcore.NetworkBackend | None = None) -> None:
        self._pool = httpcore.ConnectionPool(ssl_context=ssl.create_default_context(),
                                             network_backend=backend or PinnedBackend(pinned_ip))


def pinned_client(hop: ResolvedHop, *, timeout_s: float = TIMEOUT_S) -> httpx.Client:
    """Client httpx à usage unique pour UN hop déjà vérifié. `follow_redirects=False` toujours :
    une redirection est une NOUVELLE URL, re-vérifiée par l'appelant (`fetch.py`) avant d'y toucher,
    jamais suivie automatiquement par httpx. Hop en mode mandataire : même client, tunnel CONNECT au lieu de l'IP."""
    backend = ProxyConnectBackend(hop) if hop.proxy else None
    return httpx.Client(transport=_PinnedTransport(hop.ip, backend), timeout=timeout_s, follow_redirects=False)


def _idna(host: str) -> str:
    host = _host_key(host)
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def pinned_async_client_v2(hop: ResolvedHop, *, headers: dict[str, str] | None = None,
                           timeout_s: float = TIMEOUT_S):
    """Client `httpx2.AsyncClient` (celui que le SDK `mcp` attend) épinglé à l'IP vérifiée de *hop* : chaque
    connexion TCP part vers `hop.ip`, et SEULEMENT pour l'hôte et le port vérifiés (un autre hôte = refus, jamais une
    nouvelle résolution). Aucune redirection suivie, aucun mandataire lu dans l'environnement par httpx. TLS sur le vrai
    nom. Hop en mode mandataire (`hop.proxy`) : la connexion est un tunnel CONNECT vers l'hôte vérifié."""
    import httpcore2
    import httpx2
    from httpcore2._backends.auto import AutoBackend

    want_host, want_port = _idna(hop.hostname), hop.port

    class _PinnedAsyncBackend(httpcore2.AsyncNetworkBackend):
        def __init__(self) -> None:
            self._inner = AutoBackend()

        async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            if _idna(str(host)) != want_host or port != want_port:
                raise httpcore2.ConnectError(f"connexion refusée : {host}:{port} n'est pas l'hôte vérifié")
            if hop.proxy:
                return await self._tunnel(timeout)
            return await self._inner.connect_tcp(hop.ip, port, timeout=timeout, local_address=local_address,
                                                 socket_options=socket_options)

        async def _tunnel(self, timeout):
            """Même tunnel que `open_tunnel`, en asynchrone (mode mandataire)."""
            parts = urlsplit(hop.proxy or "")
            stream = await self._inner.connect_tcp(parts.hostname, parts.port or 80, timeout=timeout)
            try:
                await stream.write(_connect_request(hop), timeout=timeout)
                buf = b""
                while not buf.endswith(b"\r\n\r\n"):
                    chunk = await stream.read(1, timeout=timeout) if len(buf) < _PROXY_HEAD_MAX else b""
                    if not chunk:
                        raise httpcore2.ProxyError("réponse du proxy de sortie illisible")
                    buf += chunk
                try:
                    status, length = _parse_head(buf)
                except httpcore.ProxyError as exc:
                    raise httpcore2.ProxyError(str(exc)) from None
                if 200 <= status < 300:
                    return stream
                body = b""
                while len(body) < length:
                    chunk = await stream.read(length - len(body), timeout=timeout)
                    if not chunk:
                        break
                    body += chunk
            except BaseException:
                await stream.aclose()
                raise
            await stream.aclose()
            _raise_failure(*_proxy_failure(status, body, hop), httpcore2)
            raise AssertionError("inatteignable")

        async def connect_unix_socket(self, path, timeout=None, socket_options=None):
            raise httpcore2.ConnectError("socket Unix refusé")

        async def sleep(self, seconds: float) -> None:
            await self._inner.sleep(seconds)

    class _PinnedAsyncTransport(httpx2.AsyncHTTPTransport):
        def __init__(self) -> None:  # même raison que `_PinnedTransport` : le pool est construit ici
            self._pool = httpcore2.AsyncConnectionPool(ssl_context=ssl.create_default_context(),
                                                       network_backend=_PinnedAsyncBackend())

    return httpx2.AsyncClient(headers=headers or {}, timeout=timeout_s, transport=_PinnedAsyncTransport(),
                              follow_redirects=False, trust_env=False)


__all__ = [
    "DEFAULT_STRICT_PORTS",
    "MAX_REDIRECTS",
    "TIMEOUT_S",
    "PinnedBackend",
    "ProxyConnectBackend",
    "ProxyRefused",
    "ResolvedHop",
    "SsrfBlocked",
    "SsrfPolicy",
    "blocked_reason",
    "configure",
    "current_policy",
    "egress_proxy",
    "open_tunnel",
    "parse_allow_entry",
    "pinned_async_client_v2",
    "pinned_client",
    "proxy_refusal",
    "resolve_and_check",
    "strict_blocked_reason",
]
