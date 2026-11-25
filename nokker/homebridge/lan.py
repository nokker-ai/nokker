"""Accès au réseau du foyer, TOUJOURS par la liste blanche (`Allowlist`) : HTTP(S) vers une adresse vérifiée (l'IP
résolue une fois est celle qu'on joint : pas de second tour DNS, y compris en HTTPS où seuls le certificat et le SNI
portent le nom), TCP brut pour Snapcast. Rien d'autre ne sort."""
from __future__ import annotations

import http.client
import json
import socket
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from nokker.homebridge.config import Allowlist, Refused

MAX_REPLY = 8 * 1024 * 1024


class LanError(RuntimeError):
    pass


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS vers l'IP déjà vérifiée par la liste blanche : la connexion TCP va sur `ip` (aucune nouvelle résolution,
    donc pas de rebinding DNS entre le contrôle et la connexion), le certificat et le SNI restent vérifiés sur le nom."""

    def __init__(self, host: str, ip: str, port: int, *, timeout: float,
                 context: ssl.SSLContext | None = None) -> None:
        super().__init__(host, port, timeout=timeout, context=context or ssl.create_default_context())
        self.pinned_ip = ip
        self.tls_context: ssl.SSLContext = self._context  # type: ignore[attr-defined]

    def connect(self) -> None:
        sock = socket.create_connection((self.pinned_ip, self.port), self.timeout, self.source_address)
        try:
            self.sock = self.tls_context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


@dataclass
class Lan:
    allow: Allowlist
    timeout_s: float = 10.0

    def http(self, method: str, url: str, body: bytes | None = None,
             headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise Refused("adresse http(s) attendue")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        ip = self.allow.check(parts.hostname, port)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        hdrs = {"Host": parts.netloc, **(headers or {})}
        if parts.scheme == "https":  # TCP sur l'IP vérifiée, certificat et SNI au nom de l'hôte
            conn: http.client.HTTPConnection = PinnedHTTPSConnection(parts.hostname, ip, port, timeout=self.timeout_s)
        else:
            conn = http.client.HTTPConnection(ip, port, timeout=self.timeout_s)
        try:
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read(MAX_REPLY + 1)
            return resp.status, data
        except OSError as e:
            raise LanError(f"{parts.hostname}:{port} injoignable ({type(e).__name__})") from None
        finally:
            conn.close()

    def json(self, method: str, url: str, payload: Any = None, headers: dict[str, str] | None = None) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        status, data = self.http(method, url, body, {"Content-Type": "application/json", **(headers or {})})
        if status >= 400:
            raise LanError(f"réponse {status}")
        return json.loads(data or b"null")

    def tcp(self, host: str, port: int) -> socket.socket:
        ip = self.allow.check(host, port)
        try:
            return socket.create_connection((ip, port), timeout=self.timeout_s)
        except OSError as e:
            raise LanError(f"{host}:{port} injoignable ({type(e).__name__})") from None


__all__ = ["Lan", "LanError", "PinnedHTTPSConnection"]  # Home Assistant : nokker/homebridge/ha.py (client unique)
