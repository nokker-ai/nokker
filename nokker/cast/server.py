"""Serveur des pages d'écran (docs/SCREEN-CAST.md §5) : un écouteur à lui, sur l'adresse PRIVÉE de la machine, à une
origine séparée de l'API locale (autre port) et hors du tunnel nokker.app (qui ne relaie que l'API).

  GET /cast/<jeton>          la page (compte un usage ; le premier client fixe l'adresse autorisée)
  GET /cast/<jeton>/media    l'image brute (écrans « média seulement » : AirPlay, DLNA, receiver par défaut)
  GET /cast/<jeton>/events   flux SSE : `update` (nouvelle page), `end` (arrêt, expiration)

Refusé : client hors réseau privé, en-têtes de relais (`Forwarded`, `X-Forwarded-For`, `CF-Connecting-IP`…), `Host`
autre que l'adresse servie (rebinding DNS), jeton inconnu (404), expiré ou épuisé (410), autre adresse que celle de
l'écran (403). Le jeton n'est gardé qu'en empreinte ; il n'apparaît ni dans le journal ni dans un résultat d'outil."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import queue
import secrets
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from nokker.cast.render import Rendered, csp, page, update_event

logger = logging.getLogger("nokker.cast.server")

RELAY_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip", "cf-connecting-ip",
                 "true-client-ip", "x-nokker-relay", "via")


class NotLocal(RuntimeError):
    """L'adresse d'écoute n'est pas une adresse privée du réseau local."""


def lan_address() -> str:
    """Adresse de la machine sur le réseau local (route par défaut, sans envoyer de paquet)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    finally:
        s.close()


def is_lan(ip: str, *, loopback: bool = False) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    if a.is_loopback:
        return loopback
    return (a.is_private or a.is_link_local) and not a.is_unspecified and not a.is_multicast


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class _Entry:
    cast_id: str
    page: str
    nonce: str
    rendered: Rendered
    expires_at: float
    uses_left: int
    pin_ip: str | None
    first_ip: str | None = None
    ended: str | None = None
    subscribers: list[queue.Queue] = field(default_factory=list)


@dataclass
class CastServer:
    bind: str | None = None  # adresse privée ; None = celle de la route par défaut
    port: int = 0
    allow_loopback: bool = False  # tests seulement
    clock: Callable[[], float] = time.time
    heartbeat_s: float = 15.0
    on_end: Callable[[str, str], None] | None = None  # (cast_id, motif) : expiration vue par le serveur
    # Adresse annoncée aux écrans (`hôte:port` privé de la machine) quand l'écouteur est derrière un port publié par
    # Docker (home-bridge sur `homelan`) ; None = l'adresse d'écoute elle-même.
    advertise: str | None = None
    _entries: dict[str, _Entry] = field(default_factory=dict, repr=False)
    _by_cast: dict[str, str] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _httpd: ThreadingHTTPServer | None = field(default=None, repr=False)
    host: str = ""

    # ─────────────────────────── cycle de vie ───────────────────────────

    def start(self) -> None:
        if self._httpd is not None:
            return
        host = self.bind or lan_address()
        in_container = bool(self.advertise) and host in ("0.0.0.0", "::")  # derrière un port publié (home-bridge)
        if not in_container and not is_lan(host, loopback=self.allow_loopback):
            raise NotLocal(f"adresse d'écoute hors réseau local : {host}")
        srv = self

        class Handler(_Handler):
            server_ref = srv

        if self.advertise:
            adv_host = self.advertise.rsplit(":", 1)[0].strip("[]")
            if not is_lan(adv_host, loopback=self.allow_loopback):
                raise NotLocal(f"adresse annoncée hors réseau local : {adv_host}")
        self._httpd = ThreadingHTTPServer((host, self.port), Handler)
        self._httpd.daemon_threads = True
        self.host, self.port = host, self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True, name="nokker-cast-http").start()
        logger.info("écrans : pages servies sur le réseau local, port %d", self.port)

    def close(self) -> None:
        with self._lock:
            for e in self._entries.values():
                self._end(e, "stop")
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def authority(self) -> str:
        if self.advertise:
            return self.advertise
        h = f"[{self.host}]" if ":" in self.host else self.host
        return f"{h}:{self.port}"

    def url_for(self, token: str, suffix: str = "") -> str:
        return f"http://{self.authority}/cast/{token}{suffix}"

    # ─────────────────────────── liens ───────────────────────────

    def publish(self, cast_id: str, rendered: Rendered, *, ttl_s: float, max_uses: int,
                pin_ip: str | None = None) -> str:
        """Nouveau lien pour `cast_id` (l'ancien, s'il y en a un, est révoqué). Rend le jeton (à ne pas journaliser)."""
        self.start()
        token = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(12)
        e = _Entry(cast_id=cast_id, page=page(rendered, live=True, nonce=nonce), nonce=nonce, rendered=rendered,
                   expires_at=self.clock() + ttl_s, uses_left=max(1, max_uses),
                   pin_ip=pin_ip if pin_ip and _is_ip(pin_ip) else None)
        with self._lock:
            old = self._by_cast.pop(cast_id, None)
            if old and old in self._entries:
                self._end(self._entries.pop(old), "stop")
            d = _digest(token)
            self._entries[d] = e
            self._by_cast[cast_id] = d
        return token

    def update(self, cast_id: str, rendered: Rendered) -> bool:
        with self._lock:
            e = self._entries.get(self._by_cast.get(cast_id, ""))
            if e is None or e.ended:
                return False
            e.rendered = rendered
            e.page = page(rendered, live=True, nonce=e.nonce)
            for q in e.subscribers:
                q.put(("update", update_event(rendered)))
            return True

    def revoke(self, cast_id: str, reason: str = "stop") -> None:
        with self._lock:
            d = self._by_cast.pop(cast_id, None)
            e = self._entries.pop(d, None) if d else None
            if e is not None:
                self._end(e, reason)

    def _end(self, e: _Entry, reason: str) -> None:
        if e.ended:
            return
        e.ended = reason
        for q in e.subscribers:
            q.put(("end", json.dumps({"reason": reason})))

    def _lookup(self, token: str) -> _Entry | None:
        d = _digest(token)
        with self._lock:
            for k, e in self._entries.items():
                if hmac.compare_digest(k, d):
                    return e
        return None

    def expired(self, e: _Entry) -> bool:
        if e.ended:
            return True
        if self.clock() >= e.expires_at:
            with self._lock:
                self._end(e, "expired")
            if self.on_end is not None:
                try:
                    self.on_end(e.cast_id, "expired")
                except Exception:
                    logger.debug("on_end a levé", exc_info=True)
            return True
        return False

    def expires_at(self, cast_id: str) -> float | None:
        with self._lock:
            e = self._entries.get(self._by_cast.get(cast_id, ""))
            return e.expires_at if e is not None else None


def _is_ip(v: str) -> bool:
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return False


class _Handler(BaseHTTPRequestHandler):
    server_ref: CastServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # jamais le chemin (il porte le jeton)
        return

    def _send(self, status: int, body: str = "", ctype: str = "text/plain; charset=utf-8",
              extra: dict[str, str] | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _deny(self, status: int, text: str) -> None:
        self._send(status, text)

    def do_POST(self) -> None:
        self._deny(405, "méthode non permise")

    do_PUT = do_DELETE = do_PATCH = do_POST

    def do_GET(self) -> None:
        srv = self.server_ref
        client = self.client_address[0]
        if any(self.headers.get(h) for h in RELAY_HEADERS):
            return self._deny(403, "réseau local seulement")
        if not is_lan(client, loopback=srv.allow_loopback):
            return self._deny(403, "réseau local seulement")
        if (self.headers.get("Host") or "").lower() != srv.authority.lower():
            return self._deny(403, "hôte refusé")
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        if len(parts) not in (2, 3) or parts[0] != "cast":
            return self._deny(404, "introuvable")
        e = srv._lookup(parts[1])
        if e is None:
            return self._deny(404, "introuvable")
        if srv.expired(e):
            return self._deny(410, "lien expiré")
        if e.pin_ip and client != e.pin_ip:
            return self._deny(403, "autre écran")
        sub = parts[2] if len(parts) == 3 else ""
        if sub == "events":
            if e.first_ip is None or client != e.first_ip:
                return self._deny(403, "autre écran")
            return self._events(e)
        if sub not in ("", "media"):
            return self._deny(404, "introuvable")
        with srv._lock:
            if e.uses_left <= 0:
                return self._deny(410, "lien épuisé")
            if e.first_ip is not None and client != e.first_ip:
                return self._deny(403, "autre écran")
            e.uses_left -= 1
            e.first_ip = client
        if sub == "media":
            if e.rendered.media is None:
                return self._deny(404, "introuvable")
            data, mime = e.rendered.media
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return None
        return self._send(200, e.page, "text/html; charset=utf-8",
                          {"Content-Security-Policy": csp(e.nonce), "X-Frame-Options": "DENY"})

    def _events(self, e: _Entry) -> None:
        srv = self.server_ref
        q: queue.Queue = queue.Queue()
        with srv._lock:
            e.subscribers.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            while True:
                try:
                    name, data = q.get(timeout=srv.heartbeat_s)
                except queue.Empty:
                    if srv.expired(e):
                        name, data = "end", '{"reason": "expired"}'
                    else:
                        self.wfile.write(b": .\n\n")
                        self.wfile.flush()
                        continue
                self.wfile.write(f"event: {name}\ndata: {data}\n\n".encode())
                self.wfile.flush()
                if name == "end":
                    return
        except OSError:
            return
        finally:
            with srv._lock:
                if q in e.subscribers:
                    e.subscribers.remove(q)


__all__ = ["RELAY_HEADERS", "CastServer", "NotLocal", "is_lan", "lan_address"]
