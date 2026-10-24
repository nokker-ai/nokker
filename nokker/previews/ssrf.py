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
"""
from __future__ import annotations

import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

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


@dataclass(frozen=True)
class ResolvedHop:
    """Une URL résolue et classée sûre : *ip* est l'adresse à laquelle on va réellement se
    connecter (pinglée), *hostname* reste le nom pour le SNI/Host/cert."""

    url: str
    scheme: str
    hostname: str
    port: int
    ip: str


def resolve_and_check(url: str, *, resolver=socket.getaddrinfo) -> ResolvedHop:
    """Résout *url* et lève `SsrfBlocked` si le schéma, l'hôte ou UNE SEULE des IP renvoyées par le
    résolveur est interdite — on n'ignore pas les IP interdites pour en garder une autre : un
    résolveur qui renvoie une IP publique ET une IP privée pour le même nom est lui-même un signal
    (DNS rebinding préparé), donc on refuse l'URL entière plutôt que de « choisir la bonne »."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise SsrfBlocked(f"schéma refusé : {scheme or '(absent)'}")
    hostname = parts.hostname or ""
    if not hostname:
        raise SsrfBlocked("URL sans hôte")
    port = parts.port or _DEFAULT_PORTS[scheme]
    try:
        infos = resolver(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SsrfBlocked(f"résolution DNS échouée pour {hostname} : {exc}") from exc
    if not infos:
        raise SsrfBlocked(f"résolution DNS vide pour {hostname}")
    chosen_ip: str | None = None
    for _family, _socktype, _proto, _canon, sockaddr in infos:
        raw_ip = str(sockaddr[0]).split("%", 1)[0]  # retire une zone IPv6 (`fe80::1%eth0`)
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise SsrfBlocked(f"adresse renvoyée par le résolveur invalide : {raw_ip}") from exc
        reason = blocked_reason(ip)
        if reason:
            raise SsrfBlocked(f"IP refusée ({reason}) pour {hostname} : {raw_ip}")
        if chosen_ip is None:
            chosen_ip = raw_ip
    assert chosen_ip is not None
    return ResolvedHop(url=url, scheme=scheme, hostname=hostname, port=port, ip=chosen_ip)


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

    def __init__(self, pinned_ip: str) -> None:
        self._pool = httpcore.ConnectionPool(ssl_context=ssl.create_default_context(),
                                             network_backend=PinnedBackend(pinned_ip))


def pinned_client(hop: ResolvedHop, *, timeout_s: float = TIMEOUT_S) -> httpx.Client:
    """Client httpx à usage unique pour UN hop déjà vérifié. `follow_redirects=False` toujours :
    une redirection est une NOUVELLE URL, re-vérifiée par l'appelant (`fetch.py`) avant d'y toucher,
    jamais suivie automatiquement par httpx."""
    return httpx.Client(transport=_PinnedTransport(hop.ip), timeout=timeout_s, follow_redirects=False)


__all__ = [
    "MAX_REDIRECTS",
    "TIMEOUT_S",
    "PinnedBackend",
    "ResolvedHop",
    "SsrfBlocked",
    "blocked_reason",
    "pinned_client",
    "resolve_and_check",
]
