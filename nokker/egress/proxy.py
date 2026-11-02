"""Proxy de sortie avec injection des secrets (docs/EGRESS-SECRETS.md) : `python -m nokker.egress serve`.

Deux écouteurs :
- **sortie** (`:3128`, le seul que voient les bacs à sable) : proxy HTTP(S) à la manière de
  `deploy/lokkal/sandboxd/egress.py` (destination résolue puis plages privées refusées AVANT la connexion, connexion à
  l'IP vérifiée). Une requête sans jeton de session, ou d'une session sans secret accordé, est relayée telle quelle
  (CONNECT en tunnel opaque) : comportement d'origine. Une session qui a des secrets accordés (jeton dans
  `Proxy-Authorization`, posé par le noyau dans l'URL du proxy) voit ses connexions TLS terminées par l'AC du proxy :
  chaque requête passe par `policy.substitute` (pose de la valeur ou refus net 403), chaque réponse par
  `policy.Redactor` (valeur remise en référence) ;
- **contrôle** (`:3129`, noyau seulement, clé partagée `Authorization: Bearer`) : `PUT /v1/sessions/<id>` (jeton,
  locataire, secrets avec valeurs : en mémoire seulement), `DELETE /v1/sessions/<id>`, `GET /v1/journal`,
  `GET /v1/health`.

Journal : une ligne JSON par substitution, refus ou caviardage (`nokker.egress.journal`), avec session, locataire, nom
du secret, hôte et emplacement — jamais une valeur. Bibliothèque standard + `cryptography` (AC).
"""
from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets as _secrets
import socket
import ssl
import time
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.egress.ca import CertAuthority
from nokker.egress.policy import (
    HOP_HEADERS,
    Event,
    Grant,
    Redactor,
    Refused,
    Request,
    SecretRule,
    substitute,
)

log = logging.getLogger("nokker.egress")
jlog = logging.getLogger("nokker.egress.journal")

BLOCKED = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
    "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
    "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8", "64:ff9b::/96", "100::/64")]
MAX_HEAD = 64 * 1024
MAX_CONTROL_BODY = 1024 * 1024
ACCEPTED_ENCODINGS = ("gzip", "x-gzip", "deflate", "identity")


class Denied(Exception):
    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status = status


def _ssrf_guard() -> Callable[[Any], str | None] | None:
    """La garde SSRF du noyau (`nokker/previews/ssrf.py::blocked_reason`) quand l'image l'embarque (image du noyau) ;
    absente d'une image minimale (sans httpx), la liste `BLOCKED` reste seule."""
    try:
        from nokker.previews.ssrf import blocked_reason
    except ImportError:
        return None
    return blocked_reason


_SSRF = _ssrf_guard()


def is_blocked(ip: str) -> bool:
    """Destination interdite : union de `BLOCKED` (proxy d'origine) et de la garde SSRF du noyau (privées, loopback,
    lien local, CGNAT, ULA, multicast, réservées, non spécifiée)."""
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    if any(a in n for n in BLOCKED if n.version == a.version):
        return True
    return _SSRF is not None and _SSRF(a) is not None


@dataclass
class ProxyConfig:
    ca: CertAuthority
    control_key: str
    listen_host: str = "0.0.0.0"
    listen_port: int = 3128
    control_host: str = "0.0.0.0"
    control_port: int = 3129
    max_scan_bytes: int = 8 * 1024 * 1024
    connect_timeout: float = 15.0
    # Tests seulement : hôte → IP imposée, sans DNS ni contrôle des plages privées (journalisé au démarrage).
    pins: dict[str, str] = field(default_factory=dict)
    upstream_ssl: ssl.SSLContext | None = None
    journal_size: int = 5000


class Sessions:
    """Sessions déclarées par le noyau. Le jeton n'est gardé que haché ; les valeurs vivent dans les `Grant`."""

    def __init__(self) -> None:
        self._by_hash: dict[str, Grant] = {}
        self._hash_of: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._hash_of)

    @staticmethod
    def _h(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def put(self, token: str, grant: Grant) -> None:
        self.drop(grant.session)
        h = self._h(token)
        self._by_hash[h] = grant
        self._hash_of[grant.session] = h

    def drop(self, session: str) -> bool:
        h = self._hash_of.pop(session, None)
        if h is None:
            return False
        self._by_hash.pop(h, None)
        return True

    def by_token(self, token: str) -> Grant | None:
        return self._by_hash.get(self._h(token))


class Journal:
    def __init__(self, size: int) -> None:
        self._items: collections.deque[dict[str, Any]] = collections.deque(maxlen=size)
        self._seq = 0

    def add(self, grant: Grant, ev: Event, count: int | None = None) -> None:
        self._seq += 1
        entry: dict[str, Any] = {"seq": self._seq, "ts": round(time.time(), 3), "session": grant.session,
                                 "tenant": grant.tenant, **ev.as_dict()}
        if count is not None:
            entry["count"] = count
        self._items.append(entry)
        jlog.info("%s", json.dumps(entry, ensure_ascii=False))

    def since(self, after: int = 0, session: str | None = None) -> dict[str, Any]:
        items = [e for e in self._items if e["seq"] > after and (session is None or e["session"] == session)]
        return {"events": items, "next": self._seq}


def _parse_head(head: bytes) -> tuple[str, list[tuple[str, str]]]:
    lines = head.decode("latin-1").split("\r\n")
    hdrs = []
    for ln in lines[1:]:
        if not ln:
            continue
        k, sep, v = ln.partition(":")
        if not sep:
            raise Denied(400, "en-tête invalide")
        hdrs.append((k.strip(), v.strip()))
    return lines[0], hdrs


def _get(headers: list[tuple[str, str]], name: str) -> str | None:
    n = name.lower()
    return next((v for k, v in headers if k.lower() == n), None)


async def _read_head(reader: asyncio.StreamReader) -> bytes | None:
    try:
        return await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError:
        return None
    except asyncio.LimitOverrunError:
        raise Denied(400, "en-têtes trop longs") from None


async def _iter_exact(reader: asyncio.StreamReader, n: int) -> AsyncIterator[bytes]:
    while n > 0:
        chunk = await reader.read(min(n, 65536))
        if not chunk:
            raise ConnectionError("corps tronqué")
        n -= len(chunk)
        yield chunk


async def _iter_chunked(reader: asyncio.StreamReader) -> AsyncIterator[bytes]:
    while True:
        line = await reader.readuntil(b"\r\n")
        size = int(line.split(b";", 1)[0].strip() or b"0", 16)
        if size == 0:
            while (await reader.readuntil(b"\r\n")) != b"\r\n":  # trailers ignorés
                pass
            return
        yield await reader.readexactly(size)
        await reader.readexactly(2)


async def _iter_close(reader: asyncio.StreamReader) -> AsyncIterator[bytes]:
    while chunk := await reader.read(65536):
        yield chunk


async def _empty() -> AsyncIterator[bytes]:
    return
    yield b""  # pragma: no cover


async def _reply(writer: asyncio.StreamWriter, status: int, msg: str, extra: str = "") -> None:
    reason = {400: "Bad Request", 403: "Forbidden", 407: "Proxy Authentication Required", 502: "Bad Gateway",
              504: "Gateway Timeout"}.get(status, "Error")
    body = (msg + "\n").encode()
    try:
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n{extra}"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()
    except (ConnectionError, RuntimeError):
        pass
    _close(writer)


def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except RuntimeError:
        pass


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter,
                redactor: Redactor | None = None) -> None:
    try:
        while chunk := await src.read(65536):
            out = redactor.feed(chunk) if redactor else chunk
            if out:
                dst.write(out)
                await dst.drain()
        if redactor:
            dst.write(redactor.finish())
            await dst.drain()
    except (ConnectionError, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        _close(dst)


def _token_of(headers: list[tuple[str, str]]) -> str | None:
    v = _get(headers, "proxy-authorization")
    if not v or v[:6].lower() != "basic ":
        return None
    try:
        user_pass = base64.b64decode(v[6:].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""
    return user_pass.partition(":")[2]


def _accept_encoding(v: str) -> str:
    keep = [t.strip() for t in v.split(",") if t.split(";")[0].strip().lower() in ACCEPTED_ENCODINGS]
    return ", ".join(keep) or "identity"


class EgressProxy:
    def __init__(self, cfg: ProxyConfig) -> None:
        if not cfg.control_key or len(cfg.control_key) < 16:
            raise ValueError("clé du canal de contrôle requise (16 caractères au moins)")
        self.cfg = cfg
        self.sessions = Sessions()
        self.journal = Journal(cfg.journal_size)
        self.boot = _secrets.token_hex(8)
        self._servers: list[asyncio.base_events.Server] = []
        if cfg.pins:
            log.warning("épinglage d'hôtes actif (tests) : %s — jamais en production", sorted(cfg.pins))

    # ── cycle de vie ──
    async def start(self) -> tuple[int, int]:
        s1 = await asyncio.start_server(self._handle, self.cfg.listen_host, self.cfg.listen_port, limit=MAX_HEAD)
        s2 = await asyncio.start_server(self._control, self.cfg.control_host, self.cfg.control_port, limit=MAX_HEAD)
        self._servers = [s1, s2]
        p1, p2 = s1.sockets[0].getsockname()[1], s2.sockets[0].getsockname()[1]
        log.info("proxy de sortie (injection des secrets) sur :%s, contrôle sur :%s", p1, p2)
        return p1, p2

    async def close(self) -> None:
        for s in self._servers:
            s.close()
            await s.wait_closed()

    # ── résolution ──
    async def resolve_safe(self, host: str) -> str:
        h = host.strip("[]").lower()
        if h in self.cfg.pins:
            return self.cfg.pins[h]
        try:
            ipaddress.ip_address(h)
            ips = [h]
        except ValueError:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(h, None, type=socket.SOCK_STREAM)
            except socket.gaierror:
                raise Denied(502, f"nom introuvable : {h}") from None
            ips = [i[4][0] for i in infos]
        if not ips:
            raise Denied(502, f"nom introuvable : {h}")
        bad = next((ip for ip in ips if is_blocked(ip)), None)
        if bad:
            raise Denied(403, f"destination interne refusée : {h} → {bad}")
        return ips[0]

    def _upstream_ssl(self) -> ssl.SSLContext:
        ctx = self.cfg.upstream_ssl or ssl.create_default_context()
        ctx.set_alpn_protocols(["http/1.1"])
        return ctx

    # ── écouteur de sortie ──
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await _read_head(reader)
            if head is None:
                _close(writer)
                return
            first, headers = _parse_head(head)
            try:
                method, target, version = first.split(" ", 2)
            except ValueError:
                raise Denied(400, "requête de proxy invalide") from None
            token = _token_of(headers)
            grant = self.sessions.by_token(token) if token else None
            if token is not None and grant is None:
                raise Denied(407, "session inconnue du proxy de sortie (jeton absent ou périmé : rouvre la session)")
            if grant is not None and not grant.secrets:
                grant = None
            if method == "CONNECT":
                host, _, port = target.rpartition(":")
                if not host or not port.isdigit():
                    raise Denied(400, "CONNECT host:port attendu")
                ip = await self.resolve_safe(host)
                if grant is None:
                    ur, uw = await asyncio.wait_for(asyncio.open_connection(ip, int(port)), self.cfg.connect_timeout)
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                    await asyncio.gather(_pipe(reader, uw), _pipe(ur, writer))
                    return
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await writer.start_tls(self.cfg.ca.leaf_context(host.strip("[]")))
                await self._exchanges(reader, writer, host.strip("[]").lower(), int(port), ip, True, grant)
                return
            u = urlsplit(target)
            if u.scheme != "http" or not u.hostname:
                raise Denied(400, "schéma refusé (http:// absolu ou CONNECT)")
            ip = await self.resolve_safe(u.hostname)
            if grant is None:
                await self._legacy_http(reader, writer, lines_head=head, method=method, u=u, version=version, ip=ip)
                return
            origin = (u.path or "/") + (f"?{u.query}" if u.query else "")
            await self._exchanges(reader, writer, u.hostname.lower(), u.port or 80, ip, False, grant,
                                  first=(method, origin, version, headers))
        except Denied as e:
            if e.status in (403, 407):
                log.warning("%s", e)
            await _reply(writer, e.status, str(e))
        except TimeoutError:
            await _reply(writer, 504, "délai de connexion dépassé")
        except ssl.SSLError as e:
            log.info("TLS refusé côté bac à sable : %s", e.reason)
            _close(writer)
        except OSError as e:
            await _reply(writer, 502, f"connexion impossible : {e.strerror or e}")

    async def _legacy_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, lines_head: bytes,
                           method: str, u: Any, version: str, ip: str) -> None:
        """Requête http:// absolue sans secret : relais d'origine (deploy/lokkal/sandboxd/egress.py)."""
        lines = lines_head.decode("latin-1").split("\r\n")
        ur, uw = await asyncio.wait_for(asyncio.open_connection(ip, u.port or 80), self.cfg.connect_timeout)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        hdrs = [ln for ln in lines[1:] if ln and ln.split(":", 1)[0].strip().lower() not in
                ("proxy-connection", "proxy-authorization", "connection")]
        uw.write((f"{method} {path} {version}\r\n" + "".join(h + "\r\n" for h in hdrs) +
                  "Connection: close\r\n\r\n").encode("latin-1"))
        await uw.drain()
        await asyncio.gather(_pipe(reader, uw), _pipe(ur, writer))

    # ── échanges d'une session avec secrets ──
    async def _read_request_body(self, reader: asyncio.StreamReader, headers: list[tuple[str, str]]
                                 ) -> tuple[bytes | None, bytes, AsyncIterator[bytes] | None, int | None]:
        """(corps entier si ≤ plafond, sinon None ; début déjà lu ; suite à diffuser ; longueur annoncée)."""
        te = (_get(headers, "transfer-encoding") or "").lower()
        if "chunked" in te:
            it = _iter_chunked(reader)
            buf: list[bytes] = []
            total = 0
            async for data in it:
                buf.append(data)
                total += len(data)
                if total > self.cfg.max_scan_bytes:
                    return None, b"".join(buf), it, None
            return b"".join(buf), b"", None, None
        cl = _get(headers, "content-length")
        if cl is None:
            return b"", b"", None, None
        if not cl.isdigit():
            raise Denied(400, "Content-Length invalide")
        n = int(cl)
        if n <= self.cfg.max_scan_bytes:
            return await reader.readexactly(n), b"", None, n
        return None, b"", _iter_exact(reader, n), n

    async def _exchanges(self, creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter, host: str, port: int,
                         ip: str, tls: bool, grant: Grant,
                         first: tuple[str, str, str, list[tuple[str, str]]] | None = None) -> None:
        up: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        once = first is not None
        try:
            while True:
                if first is not None:
                    method, target, version, headers = first
                    first = None
                else:
                    head = await _read_head(creader)
                    if head is None:
                        return
                    line, headers = _parse_head(head)
                    try:
                        method, target, version = line.split(" ", 2)
                    except ValueError:
                        raise Denied(400, "requête invalide") from None
                if (_get(headers, "expect") or "").lower() == "100-continue":
                    cwriter.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                    await cwriter.drain()
                body, prefix, rest, declared_len = await self._read_request_body(creader, headers)
                upgrade = (_get(headers, "upgrade") or "")
                fwd = [(k, v) for k, v in headers if k.lower() not in HOP_HEADERS]
                fwd = [(k, _accept_encoding(v)) if k.lower() == "accept-encoding" else (k, v) for k, v in fwd]
                req = Request(method, target, fwd, body)
                try:
                    req, events, extra = substitute(grant, host, req)
                except Refused as e:
                    self.journal.add(grant, Event("refused", e.secret, host, e.code))
                    log.warning("requête refusée (%s, secret %s, hôte %s)", e.code, e.secret, host)
                    await _reply(cwriter, 403, f"refusé par le proxy de sortie : {e}")
                    return
                for ev in events:
                    self.journal.add(grant, ev)
                if up is None:
                    ctx = self._upstream_ssl() if tls else None
                    up = await asyncio.wait_for(
                        asyncio.open_connection(ip, port, ssl=ctx, server_hostname=host if tls else None),
                        self.cfg.connect_timeout)
                ur, uw = up
                out_headers = list(req.headers)
                if upgrade:
                    out_headers += [("Connection", "Upgrade"), ("Upgrade", upgrade)]
                if req.body is not None:
                    out_headers = [(k, v) for k, v in out_headers if k.lower() != "content-length"]
                    if req.body or method.upper() in ("POST", "PUT", "PATCH") or declared_len is not None:
                        out_headers.append(("Content-Length", str(len(req.body))))
                elif declared_len is None:
                    out_headers.append(("Transfer-Encoding", "chunked"))
                uw.write((f"{method} {req.target} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in out_headers)
                          + "\r\n").encode("latin-1"))
                if req.body:
                    uw.write(req.body)
                elif rest is not None:
                    chunked = declared_len is None
                    if prefix:
                        uw.write(b"%x\r\n%s\r\n" % (len(prefix), prefix) if chunked else prefix)
                    async for data in rest:
                        uw.write(b"%x\r\n%s\r\n" % (len(data), data) if chunked else data)
                        await uw.drain()
                    if chunked:
                        uw.write(b"0\r\n\r\n")
                await uw.drain()
                keep = await self._response(ur, cwriter, method, host, grant, extra)
                if keep == "upgraded":
                    await asyncio.gather(_pipe(creader, uw), _pipe(ur, cwriter, Redactor(grant, extra)))
                    up = None
                    return
                if keep != "keep":
                    _close(uw)
                    up = None
                client_close = (_get(headers, "connection") or "").lower() == "close" or version == "HTTP/1.0"
                if once or client_close:
                    return
        except Denied as e:
            await _reply(cwriter, e.status, str(e))
        except TimeoutError:
            await _reply(cwriter, 504, "délai de connexion dépassé")
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        except OSError as e:
            await _reply(cwriter, 502, f"connexion impossible : {e.strerror or e}")
        finally:
            if up is not None:
                _close(up[1])
            _close(cwriter)

    async def _response(self, ur: asyncio.StreamReader, cwriter: asyncio.StreamWriter, method: str, host: str,
                        grant: Grant, extra: list[tuple[bytes, bytes]]) -> str:
        """Relaie la réponse en la caviardant. Rend `keep` (connexion amont réutilisable), `close` ou `upgraded`."""
        while True:
            head = await _read_head(ur)
            if head is None:
                raise ConnectionError("réponse amont vide")
            line, headers = _parse_head(head)
            parts = line.split(" ", 2)
            code = int(parts[1])
            if 100 <= code < 200 and code != 101:
                continue
            break
        redactor = Redactor(grant, extra)
        no_body = method.upper() == "HEAD" or code in (204, 304) or code == 101
        enc = (_get(headers, "content-encoding") or "identity").strip().lower()
        te = (_get(headers, "transfer-encoding") or "").lower()
        cl = _get(headers, "content-length")
        decoder = None
        if not no_body and enc not in ("identity", ""):
            if enc not in ("gzip", "x-gzip", "deflate"):
                await _reply(cwriter, 502, f"encodage de réponse {enc} non pris en charge : le proxy de sortie ne "
                                           "pourrait pas caviarder ce corps")
                return "close"
            decoder = zlib.decompressobj(47)
        drop = {"transfer-encoding", "content-length", "connection", "keep-alive"} if not no_body else \
            {"connection", "keep-alive", "transfer-encoding"}
        if decoder is not None:
            drop.add("content-encoding")
        out = [(k, v) for k, v in headers if k.lower() not in drop]
        if code == 101:
            out += [("Connection", "Upgrade")]
        elif not no_body:
            out.append(("Transfer-Encoding", "chunked"))
        head_bytes = redactor.whole((f"{parts[0]} {code} {parts[2] if len(parts) > 2 else ''}\r\n"
                                     + "".join(f"{k}: {v}\r\n" for k, v in out) + "\r\n").encode("latin-1"))
        cwriter.write(head_bytes)
        reusable = (_get(headers, "connection") or "").lower() != "close" and parts[0] == "HTTP/1.1"
        if code == 101:
            await cwriter.drain()
            self._journal_redactions(grant, host, redactor)
            return "upgraded"
        if not no_body:
            if "chunked" in te:
                body = _iter_chunked(ur)
            elif cl is not None and cl.isdigit():
                body = _iter_exact(ur, int(cl))
            else:
                body = _iter_close(ur)
                reusable = False
            async for data in body:
                if decoder is not None:
                    data = decoder.decompress(data)
                chunk = redactor.feed(data)
                if chunk:
                    cwriter.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    await cwriter.drain()
            tail = (redactor.feed(decoder.flush()) if decoder is not None else b"") + redactor.finish()
            if tail:
                cwriter.write(b"%x\r\n%s\r\n" % (len(tail), tail))
            cwriter.write(b"0\r\n\r\n")
        await cwriter.drain()
        self._journal_redactions(grant, host, redactor)
        return "keep" if reusable else "close"

    def _journal_redactions(self, grant: Grant, host: str, redactor: Redactor) -> None:
        for name, n in sorted(redactor.hits.items()):
            self.journal.add(grant, Event("redacted", name, host, "response"), count=n)

    # ── canal de contrôle (noyau) ──
    async def _control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status, payload = 500, {"error": "internal"}
        try:
            head = await _read_head(reader)
            if head is None:
                _close(writer)
                return
            line, headers = _parse_head(head)
            method, target, _ = line.split(" ", 2)
            auth = _get(headers, "authorization") or ""
            if not hmac.compare_digest(auth.encode(), f"Bearer {self.cfg.control_key}".encode()):
                status, payload = 401, {"error": "unauthorized"}
            else:
                n = int(_get(headers, "content-length") or "0")
                if n > MAX_CONTROL_BODY:
                    raise Denied(400, "corps trop long")
                raw = await reader.readexactly(n) if n else b""
                status, payload = self._route(method, target, raw)
        except (Denied, ValueError) as e:
            status, payload = 400, {"error": str(e)}
        except Exception:
            log.exception("canal de contrôle : erreur interne")
        body = json.dumps(payload, ensure_ascii=False).encode()
        try:
            writer.write(f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                         "Connection: close\r\n\r\n".encode() + body)
            await writer.drain()
        except ConnectionError:
            pass
        _close(writer)

    def _route(self, method: str, target: str, raw: bytes) -> tuple[int, dict[str, Any]]:
        u = urlsplit(target)
        parts = u.path.strip("/").split("/")
        if method == "GET" and u.path == "/v1/health":
            return 200, {"ok": True, "sessions": len(self.sessions), "boot": self.boot}
        if method == "GET" and u.path == "/v1/journal":
            q = parse_qs(u.query)
            after = int((q.get("after") or ["0"])[0])
            return 200, self.journal.since(after, (q.get("session") or [None])[0])
        if len(parts) == 3 and parts[:2] == ["v1", "sessions"] and parts[2]:
            sid = parts[2]
            if method == "DELETE":
                return 200, {"session": sid, "deleted": self.sessions.drop(sid)}
            if method == "PUT":
                d = json.loads(raw or b"{}")
                token, tenant = d.get("token"), d.get("tenant")
                if not isinstance(token, str) or len(token) < 32 or not isinstance(tenant, str) or not tenant:
                    raise ValueError("token (32 caractères au moins) et tenant requis")
                grant = Grant(session=sid, tenant=tenant)
                for s in d.get("secrets") or []:
                    rule = SecretRule.from_dict(str(s.get("name")), {k: s.get(k) for k in
                                                                     ("hosts", "headers", "query", "fields")
                                                                     if s.get(k) is not None})
                    value = s.get("value")
                    if not isinstance(value, str) or not value:
                        raise ValueError(f"secret {rule.name} : valeur requise")
                    grant.secrets[rule.name] = (rule, value)
                self.sessions.put(token, grant)
                jlog.info("%s", json.dumps({"event": "granted", "session": sid, "tenant": tenant,
                                            "secrets": {n: list(r.hosts) for n, (r, _) in grant.secrets.items()}},
                                           ensure_ascii=False))
                return 200, {"session": sid, "secrets": grant.names()}
        return 404, {"error": "not_found"}


async def serve(cfg: ProxyConfig, ready: Callable[[int, int], Awaitable[None] | None] | None = None) -> None:
    proxy = EgressProxy(cfg)
    ports = await proxy.start()
    if ready is not None:
        r = ready(*ports)
        if asyncio.iscoroutine(r):
            await r
    await asyncio.Event().wait()


__all__ = ["BLOCKED", "EgressProxy", "Journal", "ProxyConfig", "Sessions", "is_blocked", "serve"]
