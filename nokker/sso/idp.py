"""Client d'un fournisseur OIDC (drapeau `sso`) : découverte, JWKS, échange du code. Le réseau passe par `Http`
(injecté : les tests servent un faux fournisseur en mémoire) ; en service, `SafeHttp` :

- **https seulement**, résolution puis refus de toute adresse privée, loopback, link-local (métadonnées du nuage),
  CGNAT, ULA (la garde SSRF des aperçus, nokker/previews/ssrf.py), connexion pinglée à l'adresse vérifiée ;
- aucune redirection suivie, 5 s au plus, réponse de 1 Mio au plus.
L'issuer est saisi par le propriétaire d'une équipe : sans cette garde, il ferait parler le noyau à son réseau interne.

Caches (par instance, donc par pod) :
- découverte : 1 h par issuer ; le document doit annoncer EXACTEMENT cet issuer, et des points https ;
- JWKS : 1 h ; un `kid` inconnu recharge le JWKS (rotation des clés du fournisseur), au plus une fois toutes les 30 s.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

DISCOVERY_TTL_S = 3600.0
JWKS_TTL_S = 3600.0
JWKS_REFRESH_MIN_S = 30.0
MAX_BODY = 1024 * 1024
TIMEOUT_S = 5.0


class IdpError(Exception):
    """`code` : unreachable, bad_discovery, bad_jwks, token_endpoint. Le message ne porte jamais un secret ni la
    réponse du fournisseur (qui peut contenir un jeton)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class Http(Protocol):
    def get(self, url: str) -> tuple[int, bytes]: ...
    def post_form(self, url: str, data: dict[str, str], headers: dict[str, str]) -> tuple[int, bytes]: ...


class SafeHttp:
    def __init__(self, *, resolver: Callable[..., Any] | None = None, timeout_s: float = TIMEOUT_S) -> None:
        self.resolver = resolver
        self.timeout_s = timeout_s

    def _hop(self, url: str) -> Any:
        from nokker.previews.ssrf import SsrfBlocked, resolve_and_check

        if urlsplit(url).scheme != "https":
            raise IdpError("unreachable", "https seulement")
        try:
            return resolve_and_check(url, **({"resolver": self.resolver} if self.resolver else {}))
        except SsrfBlocked:
            raise IdpError("unreachable", "adresse refusée") from None

    def _send(self, method: str, url: str, **kw: Any) -> tuple[int, bytes]:
        import httpx

        from nokker.previews.ssrf import pinned_client

        hop = self._hop(url)
        try:
            with pinned_client(hop, timeout_s=self.timeout_s) as c, c.stream(method, url, **kw) as r:
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BODY:
                        raise IdpError("unreachable", "réponse trop grande")
                return r.status_code, bytes(body)
        except httpx.HTTPError:
            raise IdpError("unreachable", "fournisseur injoignable") from None

    def get(self, url: str) -> tuple[int, bytes]:
        return self._send("GET", url, headers={"Accept": "application/json"})

    def post_form(self, url: str, data: dict[str, str], headers: dict[str, str]) -> tuple[int, bytes]:
        return self._send("POST", url, content=urlencode(data).encode(),
                          headers={**headers, "Content-Type": "application/x-www-form-urlencoded",
                                   "Accept": "application/json"})


def _https(u: Any) -> bool:
    return isinstance(u, str) and urlsplit(u).scheme == "https" and bool(urlsplit(u).hostname)


def _load(status: int, body: bytes, code: str) -> dict[str, Any]:
    if status != 200:
        raise IdpError(code, f"HTTP {status}")
    try:
        d = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise IdpError(code, "réponse illisible") from None
    if not isinstance(d, dict):
        raise IdpError(code, "réponse illisible")
    return d


class Idp:
    def __init__(self, *, http: Http, clock: Callable[[], float] = time.time) -> None:
        self.http = http
        self.clock = clock
        self._disc: dict[str, tuple[float, dict[str, Any]]] = {}
        self._jwks: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._last_refresh: dict[str, float] = {}
        self._lock = threading.Lock()

    def discovery(self, issuer: str) -> dict[str, Any]:
        now = self.clock()
        with self._lock:
            hit = self._disc.get(issuer)
        if hit is not None and now - hit[0] < DISCOVERY_TTL_S:
            return hit[1]
        d = _load(*self.http.get(issuer.rstrip("/") + "/.well-known/openid-configuration"), "bad_discovery")
        if d.get("issuer") != issuer:
            raise IdpError("bad_discovery", "le document de découverte annonce un autre issuer")
        for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not _https(d.get(k)):
                raise IdpError("bad_discovery", f"{k} : une URL https")
        algs = d.get("id_token_signing_alg_values_supported")
        if isinstance(algs, list) and not {"RS256", "ES256"} & set(algs):
            raise IdpError("bad_discovery", "le fournisseur ne signe ni en RS256 ni en ES256")
        out = {k: d[k] for k in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")}
        with self._lock:
            self._disc[issuer] = (now, out)
        return out

    def _fetch_jwks(self, uri: str, now: float) -> list[dict[str, Any]]:
        d = _load(*self.http.get(uri), "bad_jwks")
        keys = d.get("keys")
        if not isinstance(keys, list):
            raise IdpError("bad_jwks", "JWKS sans « keys »")
        keys = [k for k in keys if isinstance(k, dict)][:50]
        with self._lock:
            self._jwks[uri] = (now, keys)
            self._last_refresh[uri] = now
        return keys

    def keys(self, disc: dict[str, Any], kid: str | None) -> list[dict[str, Any]]:
        uri, now = disc["jwks_uri"], self.clock()
        with self._lock:
            hit = self._jwks.get(uri)
            last = self._last_refresh.get(uri, float("-inf"))
        if hit is None or now - hit[0] >= JWKS_TTL_S:
            return self._fetch_jwks(uri, now)
        keys = hit[1]
        if kid is not None and all(k.get("kid") != kid for k in keys) and now - last >= JWKS_REFRESH_MIN_S:
            return self._fetch_jwks(uri, now)  # rotation : le fournisseur a publié une nouvelle clé
        return keys

    def exchange(self, disc: dict[str, Any], *, code: str, verifier: str, redirect_uri: str, client_id: str,
                 client_secret: str, token_auth: str) -> dict[str, Any]:
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "code_verifier": verifier, "client_id": client_id}
        headers: dict[str, str] = {}
        if token_auth == "client_secret_post":
            data["client_secret"] = client_secret
        else:
            from urllib.parse import quote

            pair = f"{quote(client_id, safe='')}:{quote(client_secret, safe='')}"  # RFC 6749 §2.3.1
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        status, body = self.http.post_form(disc["token_endpoint"], data, headers)
        if status != 200:
            raise IdpError("token_endpoint", f"HTTP {status}")
        d = _load(status, body, "token_endpoint")
        if not isinstance(d.get("id_token"), str):
            raise IdpError("token_endpoint", "réponse sans id_token")
        return d


__all__ = ["Http", "Idp", "IdpError", "SafeHttp"]
