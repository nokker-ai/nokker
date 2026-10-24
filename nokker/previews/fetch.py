"""nokker/previews/fetch.py — récupération HTTP gardée (SSRF + PII + limites) pour les aperçus.

Une seule primitive, `guarded_get`, réutilisée pour la page elle-même, un oEmbed JSON et une image
(og:image/favicon) : même garde SSRF (`ssrf.resolve_and_check`, re-vérifiée à CHAQUE redirection),
même garde PII (`nokker.tools.web.pii_violation`, réutilisée telle quelle — pas de 2e implémentation),
mêmes limites de temps/octets, juste un `max_bytes` différent selon l'appelant.
"""
from __future__ import annotations

import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from nokker.previews.ssrf import (
    MAX_REDIRECTS,
    TIMEOUT_S,
    ResolvedHop,
    SsrfBlocked,
    pinned_client,
    resolve_and_check,
)
from nokker.tools.web import pii_violation

_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
# ASCII strict (httpx refuse un en-tête non-ASCII sans encodage explicite — vu en test).
_USER_AGENT = "nokker-previews/1 (+https://nokker.ai; apercus pour les bulles)"

ClientFactory = Callable[[ResolvedHop, float], httpx.Client]  # (hop vérifié, timeout restant) -> client


class FetchError(RuntimeError):
    """Échec de récupération qui n'est PAS un refus de politique (timeout, transport, trop gros)."""


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    body: bytes
    redirects: int


def _default_client_factory(hop: ResolvedHop, timeout_s: float) -> httpx.Client:
    return pinned_client(hop, timeout_s=timeout_s)


def guarded_get(url: str, *, max_bytes: int, timeout_s: float = TIMEOUT_S, max_redirects: int = MAX_REDIRECTS,
                client_factory: Callable[[ResolvedHop, float], httpx.Client] | None = None,
                resolver=socket.getaddrinfo, check_pii: bool = True) -> FetchResult:
    """GET gardé : PII sur l'URL (chaque hop), SSRF (`resolve_and_check`, chaque hop, connexion
    pinglée à l'IP vérifiée), ≤ `max_redirects` redirections, ≤ `max_bytes` (vérifié dès l'en-tête
    `Content-Length` quand il est présent, ET en streaming — un serveur qui mentirait sur
    `Content-Length` n'y gagne rien), budget de temps GLOBAL `timeout_s` sur TOUS les hops (pas
    `timeout_s` par redirection : un site qui redirige 3 fois pour épuiser le délai est traité
    comme un timeout, pas comme 3×5 s).

    Lève `SsrfBlocked` (politique : schéma, IP interdite, PII, trop de redirections, redirection
    sans `Location`) ou `FetchError` (transport, délai dépassé, corps trop gros)."""
    factory = client_factory or _default_client_factory
    deadline = time.monotonic() + timeout_s
    current_url = url
    redirects = 0
    headers = {"User-Agent": _USER_AGENT, "Accept": "text/html,application/json,image/*,*/*;q=0.8"}

    while True:
        if check_pii:
            violation = pii_violation(current_url)
            if violation:
                raise SsrfBlocked(f"URL refusée : donnée personnelle ({violation})")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError(f"délai dépassé ({timeout_s:.1f}s) après {redirects} redirection(s)")

        hop = resolve_and_check(current_url, resolver=resolver)
        client = factory(hop, remaining)
        try:
            try:
                with client.stream("GET", current_url, headers=headers, timeout=remaining) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise SsrfBlocked(f"redirection {response.status_code} sans Location")
                        redirects += 1
                        if redirects > max_redirects:
                            raise SsrfBlocked(f"trop de redirections (> {max_redirects})")
                        current_url = urljoin(current_url, location)
                        continue
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        try:
                            if int(declared) > max_bytes:
                                raise FetchError(f"corps annoncé ({declared} octets) > {max_bytes}")
                        except ValueError:
                            pass
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise FetchError(f"corps > {max_bytes} octets (coupé en streaming)")
                    return FetchResult(requested_url=url, final_url=str(response.url),
                                      status_code=response.status_code,
                                      content_type=response.headers.get("content-type", ""),
                                      body=bytes(body), redirects=redirects)
            except httpx.TimeoutException as exc:
                raise FetchError(f"délai dépassé : {exc}") from exc
            except httpx.HTTPError as exc:
                raise FetchError(f"requête échouée : {exc}") from exc
        finally:
            client.close()


__all__ = ["ClientFactory", "FetchError", "FetchResult", "guarded_get"]
