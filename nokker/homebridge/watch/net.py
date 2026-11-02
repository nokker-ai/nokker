"""Accès réseau de la veille, TOUJOURS par la liste blanche de la passerelle (`config.Allowlist`) : en HTTP clair,
l'adresse résolue et vérifiée UNE fois est celle qu'on joint (pas de second tour DNS) ; en HTTPS, le certificat est
vérifié au nom de l'hôte (ou par le CA de `ca_file`, ou pas du tout sur `verify_tls: false` explicite, cas des
consoles à certificat auto-signé). Aucune redirection suivie, réponses bornées, en-têtes rendus (jeton de connexion
Frigate), flux pour les médias (vignette, direct) relayés vers l'écran local sans passer par le noyau.
"""
from __future__ import annotations

import http.client
import json
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlencode, urlsplit

from nokker.homebridge.config import BridgeConfig, BridgeConfigError, Refused

MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_MEDIA_BYTES = 8 * 1024 * 1024
_SECRET = "secret:"


class UpstreamError(RuntimeError):
    """Appareil injoignable ou réponse refusée ; le message ne contient ni secret ni corps de réponse."""

    def __init__(self, message: str, code: str = "upstream") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class WatchEnv:
    """Ce que la veille prend de la passerelle : la liste blanche et les secrets PROPRES (`secret:NOM` → fichier
    `secrets/NOM` sous le dossier de home.yaml)."""

    cfg: BridgeConfig

    def check_url(self, url: str) -> None:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https", "ws", "wss") or not parts.hostname:
            raise Refused("adresse http(s) ou ws(s) attendue")
        self.cfg.allow.check(parts.hostname, parts.port or (443 if scheme in ("https", "wss") else 80))

    def secret(self, ref: str) -> str:
        if not isinstance(ref, str) or not ref.startswith(_SECRET):
            raise BridgeConfigError("référence secret:NOM attendue")
        name = ref[len(_SECRET):]
        if not name or not all(c.isalnum() or c == "_" for c in name) or len(name) > 64:
            raise BridgeConfigError("nom de secret invalide")
        try:
            value = self.cfg.secret(f"secrets/{name}")
        except OSError:
            raise BridgeConfigError(f"secret introuvable : {name}") from None
        if not value:
            raise BridgeConfigError(f"secret vide : {name}")
        return value


class Resp:
    def __init__(self, status: int, headers: http.client.HTTPMessage, content: bytes = b"",
                 raw: http.client.HTTPResponse | None = None) -> None:
        self.status_code, self._headers, self.content, self._raw = status, headers, content, raw
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.cookies: dict[str, str] = {}
        for line in headers.get_all("Set-Cookie") or []:
            c = SimpleCookie()
            try:
                c.load(line)
            except Exception:  # noqa: BLE001 — cookie illisible : ignoré
                continue
            self.cookies.update({k: m.value for k, m in c.items()})

    def json(self) -> Any:
        return json.loads(self.content or b"null")

    def iter_bytes(self, size: int = 64 * 1024) -> Iterator[bytes]:
        if self._raw is None:
            yield self.content
            return
        while True:
            chunk = self._raw.read1(size) if hasattr(self._raw, "read1") else self._raw.read(size)
            if not chunk:
                return
            yield chunk


@dataclass
class LanHttp:
    env: WatchEnv
    verify: Any = True  # bool, ou chemin d'un CA (`ca_file`)
    timeout_s: float = 10.0

    def _conn(self, url: str) -> tuple[http.client.HTTPConnection, str]:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise Refused("adresse http(s) attendue")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        ip = self.env.cfg.allow.check(parts.hostname, port)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if parts.scheme == "https":
            ctx = ssl.create_default_context(cafile=self.verify if isinstance(self.verify, str) else None)
            if self.verify is False:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            return http.client.HTTPSConnection(parts.hostname, port, timeout=self.timeout_s, context=ctx), path
        return http.client.HTTPConnection(ip, port, timeout=self.timeout_s), path

    @staticmethod
    def _prepare(url: str, headers: dict[str, str] | None, params: Any, json_body: Any, data: Any
                 ) -> tuple[str, bytes | None, dict[str, str]]:
        h = {"Host": urlsplit(url).netloc, "Accept": "application/json", **(headers or {})}
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
        body = None
        if json_body is not None:
            body, h["Content-Type"] = json.dumps(json_body).encode("utf-8"), "application/json"
        elif data is not None:
            body, h["Content-Type"] = urlencode(data).encode("utf-8"), "application/x-www-form-urlencoded"
        return url, body, h

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None, params: Any = None,
                json_body: Any = None, data: Any = None) -> Resp:
        url, body, h = self._prepare(url, headers, params, json_body, data)
        conn, path = self._conn(url)
        try:
            conn.request(method, path, body=body, headers=h)
            r = conn.getresponse()
            content = r.read(MAX_JSON_BYTES + 1)
            if len(content) > MAX_JSON_BYTES:
                raise UpstreamError("réponse trop grande", "too_large")
            return Resp(r.status, r.headers, content)
        except (OSError, http.client.HTTPException) as e:
            raise UpstreamError(f"appareil injoignable ({type(e).__name__})", "unreachable") from None
        finally:
            conn.close()

    def json(self, method: str, url: str, **kw: Any) -> Any:
        r = self.request(method, url, **kw)
        if r.status_code in (401, 403):
            raise UpstreamError(f"accès refusé par l'appareil ({r.status_code})", "auth")
        if r.status_code >= 400:
            raise UpstreamError(f"l'appareil a répondu {r.status_code}", "status")
        try:
            return r.json()
        except ValueError:
            raise UpstreamError("réponse non JSON", "bad_json") from None

    @contextmanager
    def stream(self, url: str, *, headers: dict[str, str] | None = None) -> Iterator[Resp]:
        """Flux (vignette, MJPEG) relayé tel quel vers l'écran local ; jamais lu par le noyau."""
        url, _, h = self._prepare(url, headers, None, None, None)
        h.pop("Accept", None)
        conn, path = self._conn(url)
        try:
            conn.request("GET", path, headers=h)
            r = conn.getresponse()
            yield Resp(r.status, r.headers, raw=r)
        except (OSError, http.client.HTTPException) as e:
            raise UpstreamError(f"appareil injoignable ({type(e).__name__})", "unreachable") from None
        finally:
            conn.close()


__all__ = ["MAX_MEDIA_BYTES", "LanHttp", "Resp", "UpstreamError", "WatchEnv"]
