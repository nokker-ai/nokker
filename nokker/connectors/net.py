"""Garde réseau des connecteurs : l'utilisateur choisit l'hôte, donc SSRF. Mêmes règles que les aperçus
(`nokker/previews/ssrf.py` : aucune IP privée/loopback/CGNAT/ULA, toutes les IP du nom vérifiées) plus une liste de ports
par protocole, et connexion épinglée à l'IP vérifiée (pas de seconde résolution), le TLS portant sur le vrai nom."""
from __future__ import annotations

import logging
import re
import socket
import ssl

from nokker.connectors.errors import ConnectorError
from nokker.previews.ssrf import ResolvedHop, SsrfBlocked, pinned_client, resolve_and_check

# httpx journalise chaque requête en INFO avec l'URL complète ; or l'URL d'un agenda iCal EST le secret.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

ALLOWED_PORTS: dict[str, frozenset[int]] = {
    "imap": frozenset({993, 143}),
    "smtp": frozenset({465, 587}),
    "https": frozenset({443, 8443}),
}
IMPLICIT_TLS = {("imap", 993), ("smtp", 465)}
TIMEOUT_S = 15.0

_HOST_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")


def check_endpoint(host: str, port: int, proto: str, *, resolver=socket.getaddrinfo) -> ResolvedHop:
    """Nom d'hôte DNS (pas d'IP littérale) + port autorisé pour `proto` + toutes ses IP publiques."""
    host = (host or "").strip().rstrip(".")
    if proto not in ALLOWED_PORTS:
        raise ConnectorError("bad_settings", f"protocole inconnu : {proto}")
    if not _HOST_RE.match(host):
        raise ConnectorError("bad_settings", "hôte invalide : un nom de domaine est attendu")
    if isinstance(port, bool) or not isinstance(port, int) or port not in ALLOWED_PORTS[proto]:
        raise ConnectorError("port_not_allowed", f"port {port} refusé pour {proto} (autorisés : "
                             f"{', '.join(map(str, sorted(ALLOWED_PORTS[proto])))})")
    try:
        return resolve_and_check(f"https://{host}:{port}/", resolver=resolver)
    except SsrfBlocked as exc:
        raise ConnectorError("host_blocked", str(exc)) from None


def tls_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def open_pinned(hop: ResolvedHop, *, tls: bool, timeout_s: float = TIMEOUT_S) -> socket.socket:
    """Socket connecté à l'IP vérifiée ; enveloppé en TLS (vérification du certificat sur `hop.hostname`) si `tls`."""
    try:
        sock = socket.create_connection((hop.ip, hop.port), timeout=timeout_s)
    except OSError as exc:
        raise ConnectorError("unreachable", f"connexion à {hop.hostname}:{hop.port} impossible ({exc.__class__.__name__})") from None
    if not tls:
        return sock
    try:
        return tls_context().wrap_socket(sock, server_hostname=hop.hostname)
    except (ssl.SSLError, OSError) as exc:
        sock.close()
        raise ConnectorError("tls_failed", f"TLS refusé par {hop.hostname} ({exc.__class__.__name__})") from None


def https_client(url_host: str, port: int = 443, *, timeout_s: float = TIMEOUT_S):
    """Client httpx épinglé à l'IP vérifiée de `url_host:port` (CalDAV, iCal). Sans redirection suivie : l'appelant
    re-vérifie chaque `Location` avec `check_endpoint`."""
    hop = check_endpoint(url_host, port, "https")
    return hop, pinned_client(hop, timeout_s=timeout_s)
