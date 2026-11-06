"""Garde réseau du BYOK cloud (docs/CLOUD-BYOK.md §4) : un point d'accès choisi par un CLIENT, appelé depuis le noyau
multi-locataire de nokker.ai. Réutilise la garde SSRF du noyau (`nokker/previews/ssrf.py`), en mode strict et SANS la
liste d'autorisation de l'opérateur :

- `https` seulement, sans identifiants dans l'URL, sans requête ni fragment ;
- toutes les adresses du nom doivent être publiques (refus : privé RFC1918, loopback, link-local dont 169.254.169.254,
  CGNAT/Tailscale, ULA, multicast, réservée, IPv4 portée par NAT64/6to4/Teredo, site-local) ;
- **revérifié à chaque connexion** : `GuardedBackend.connect_tcp` résout de nouveau le nom et se connecte à l'IP qu'il
  vient de vérifier (un nom qui bascule vers une IP privée après l'enregistrement est refusé au tour suivant, DNS
  rebinding compris) ; aucun autre hôte que celui du point d'accès, aucune redirection suivie, aucun mandataire lu
  dans l'environnement par httpx (le mandataire de sortie du noyau, s'il est posé, passe par `open_tunnel`).
"""
from __future__ import annotations

import ssl
from urllib.parse import urlsplit

import httpcore
import httpx

from nokker.previews.ssrf import (
    ProxyConnectBackend,
    SsrfBlocked,
    SsrfPolicy,
    _idna,
    resolve_and_check,
)

# Strict, aucune entrée autorisée vers le privé (la liste `ssrf.allow` de l'opérateur ne vaut pas pour un client),
# tous les ports (un vLLM auto-hébergé écoute souvent sur 8000) : https seulement, donc une poignée de main TLS au plus.
BYOK_POLICY = SsrfPolicy(strict=True, allow=frozenset(), ports=frozenset())
MAX_URL = 512


class BlockedEndpoint(ValueError):
    """`code` : `bad_base_url` (forme) ou `blocked_address` (adresse interdite, DNS en échec)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_base_url(raw: object) -> str:
    """Forme seulement (sans DNS) : `https://hôte[:port][/chemin]`, sans `/` final. `BlockedEndpoint` sinon."""
    if not isinstance(raw, str) or not raw.strip():
        raise BlockedEndpoint("bad_base_url", "base_url requise")
    url = raw.strip()
    if len(url) > MAX_URL:
        raise BlockedEndpoint("bad_base_url", f"base_url trop longue ({MAX_URL} caractères au plus)")
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 — un port illisible lève ici
    except ValueError:
        raise BlockedEndpoint("bad_base_url", "base_url illisible") from None
    if parts.scheme.lower() != "https":
        raise BlockedEndpoint("bad_base_url", "base_url : https seulement")
    if not parts.hostname:
        raise BlockedEndpoint("bad_base_url", "base_url sans hôte")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise BlockedEndpoint("bad_base_url", "base_url : pas d'identifiants dans l'adresse (la clé va dans api_key)")
    if parts.query or parts.fragment:
        raise BlockedEndpoint("bad_base_url", "base_url : ni requête ni fragment")
    return url.rstrip("/")


def check_endpoint(raw: object, *, resolver=None) -> str:
    """Forme + résolution DNS : toutes les adresses du nom doivent être publiques. Rend l'URL normalisée."""
    url = normalize_base_url(raw)
    try:
        resolve_and_check(url, resolver=resolver, policy=BYOK_POLICY)
    except SsrfBlocked as e:
        raise BlockedEndpoint("blocked_address", str(e)) from None
    return url


class GuardedBackend(httpcore.SyncBackend):
    """Chaque connexion TCP : l'hôte doit être celui du point d'accès, le nom est RÉSOLU DE NOUVEAU et vérifié, puis la
    connexion part vers l'IP vérifiée (TLS et SNI sur le vrai nom, lus par httpcore à part). `inner` : le backend qui
    ouvre vraiment le socket (injectable pour les tests)."""

    def __init__(self, base_url: str, *, resolver=None, inner: httpcore.NetworkBackend | None = None) -> None:
        super().__init__()
        parts = urlsplit(base_url)
        self._host = _idna(parts.hostname or "")
        self._port = parts.port or 443
        self._resolver = resolver
        self._inner = inner or httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if _idna(str(host)) != self._host or port != self._port:
            raise httpcore.ConnectError(f"connexion refusée : {host}:{port} n'est pas le point d'accès enregistré")
        try:
            hop = resolve_and_check(f"https://{self._host}:{self._port}/", resolver=self._resolver, policy=BYOK_POLICY)
        except SsrfBlocked as e:
            raise httpcore.ConnectError(f"adresse refusée : {e}") from None
        if hop.proxy:
            return ProxyConnectBackend(hop).connect_tcp(host, port, timeout=timeout)
        return self._inner.connect_tcp(hop.ip, port, timeout=timeout, local_address=local_address,
                                       socket_options=socket_options)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("socket Unix refusé")


class _GuardedTransport(httpx.HTTPTransport):
    """Même construction que `previews/ssrf.py::_PinnedTransport` : httpx ne laisse pas passer un `network_backend`,
    le pool httpcore est donc construit ici (`handle_request`/`close` hérités ne lisent que `self._pool`)."""

    def __init__(self, backend: httpcore.NetworkBackend) -> None:
        self._pool = httpcore.ConnectionPool(ssl_context=ssl.create_default_context(), network_backend=backend)


def guarded_client(base_url: str, timeout: httpx.Timeout | float, *, resolver=None,
                   inner: httpcore.NetworkBackend | None = None) -> httpx.Client:
    """Client httpx du point d'accès d'un locataire : garde à chaque connexion, aucune redirection, aucun mandataire
    lu dans l'environnement."""
    return httpx.Client(transport=_GuardedTransport(GuardedBackend(base_url, resolver=resolver, inner=inner)),
                        timeout=timeout, follow_redirects=False, trust_env=False)


__all__ = ["BYOK_POLICY", "BlockedEndpoint", "GuardedBackend", "check_endpoint", "guarded_client",
           "normalize_base_url"]
