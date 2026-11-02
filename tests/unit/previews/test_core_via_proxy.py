"""Sortie de core par mandataire (chantier p2/core-via-proxy, docs/LOKKAL-CORE-EGRESS.md).

Lokkal ferme core au réseau : plus de DNS, plus de route, seulement `HTTP(S)_PROXY=http://egress:3128`. On simule ici :
  - un faux `egress` local qui ne parle que `CONNECT` (table de routes vers des serveurs locaux, refus façon egress) ;
  - un core sans DNS : `socket.getaddrinfo` échoue pour tout nom (seules les IP littérales passent, comme le nom du
    mandataire qu'un core réel résout par le DNS interne de Docker) ;
et on vérifie que l'aperçu, la lecture web, l'agenda (iCal sur https) et l'IMAP passent par le tunnel, TLS sur le vrai
nom ; que les refus du mandataire redonnent les refus d'avant ; et que, SANS mandataire, rien ne change.
"""
from __future__ import annotations

import http.server
import ipaddress
import os
import socket
import ssl
import subprocess
import threading
from datetime import UTC, datetime, timedelta

import pytest

from nokker.connectors import net
from nokker.connectors.calendar import CalendarConnector
from nokker.connectors.errors import ConnectorError
from nokker.connectors.mail import connect_imap
from nokker.previews import ssrf
from nokker.previews.fetch import FetchError, guarded_get
from nokker.previews.service import PreviewService
from nokker.previews.ssrf import ResolvedHop, SsrfBlocked, resolve_and_check

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy", "REQUEST_METHOD")
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CTX = ssl.create_default_context

ARTICLE = (b"<!doctype html><html lang='fr'><head><title>Page</title>"
           b"<meta property='og:title' content='Un article par le tunnel'>"
           b"<meta property='og:site_name' content='Example'></head><body><article><p>"
           + b"Contenu assez long pour la lecture directe par trafilatura. " * 20 + b"</p></article></body></html>")


def _ics() -> bytes:
    start = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y%m%dT%H%M%SZ")
    end = (datetime.now(UTC) + timedelta(days=2, hours=1)).strftime("%Y%m%dT%H%M%SZ")
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:a1@example.com\r\n"
            f"DTSTART:{start}\r\nDTEND:{end}\r\nSUMMARY:Réunion\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n").encode()


# ─────────────────────────────── serveurs locaux ───────────────────────────────


@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    d = tmp_path_factory.mktemp("tls")
    key, crt = d / "k.pem", d / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=example.com",
                    "-addext", "subjectAltName=DNS:page.example.com,DNS:cal.example.com,DNS:imap.example.com",
                    "-keyout", str(key), "-out", str(crt)], check=True, capture_output=True)
    return str(crt), str(key)


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        self.server.hosts.append(self.headers.get("Host"))
        body, ctype = (_ics(), "text/calendar") if self.path.endswith(".ics") else (ARTICLE, "text/html; charset=utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _http_server(tls_ctx=None):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.hosts = []
    if tls_ctx is not None:
        srv.socket = tls_ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _imap_server(tls_ctx):
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(4)
    seen: list[str] = []

    def serve(conn):
        with tls_ctx.wrap_socket(conn, server_side=True) as s:
            f = s.makefile("rwb")
            f.write(b"* OK IMAP4rev1 pret\r\n")
            f.flush()
            for raw in f:
                parts = raw.decode().split()
                tag, cmd = parts[0], parts[1].upper()
                seen.append(cmd)
                if cmd == "CAPABILITY":
                    f.write(f"* CAPABILITY IMAP4rev1 AUTH=PLAIN\r\n{tag} OK fait\r\n".encode())
                elif cmd == "LOGOUT":
                    f.write(f"* BYE\r\n{tag} OK fait\r\n".encode())
                    f.flush()
                    return
                else:
                    f.write(f"{tag} OK fait\r\n".encode())
                f.flush()

    def loop():
        while True:
            conn, _ = lsock.accept()
            threading.Thread(target=serve, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return lsock.getsockname()[1], seen


class FakeEgress:
    """Mandataire CONNECT seul, comme `egress` vu de core : route connue -> 200 et tuyau ; refus -> statut et corps
    d'egress ; nom inconnu -> 502 « nom introuvable »."""

    def __init__(self, routes: dict[str, int], deny: dict[str, tuple[int, str]] | None = None):
        self.routes, self.deny, self.seen = routes, deny or {}, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            conn, _ = self.sock.accept()
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _reply(conn, status, reason, msg):
        body = (msg + "\n").encode()
        conn.sendall(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        conn.close()

    def _serve(self, conn):
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            chunk = conn.recv(1)
            if not chunk:
                return
            head += chunk
        method, target, _ = head.decode("latin-1").split("\r\n")[0].split(" ", 2)
        self.seen.append(f"{method} {target}")
        if method != "CONNECT":
            return self._reply(conn, 400, "Bad Request", "schéma refusé (http:// absolu ou CONNECT)")
        if target in self.deny:
            status, msg = self.deny[target]
            return self._reply(conn, status, "Forbidden" if status == 403 else "Error", msg)
        if target not in self.routes:
            return self._reply(conn, 502, "Bad Gateway", f"nom introuvable : {target.rpartition(':')[0]}")
        up = socket.create_connection(("127.0.0.1", self.routes[target]))
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

        def pipe(a, b):
            try:
                while data := a.recv(65536):
                    b.sendall(data)
            except OSError:
                pass
            finally:
                for s in (a, b):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        threading.Thread(target=pipe, args=(up, conn), daemon=True).start()
        pipe(conn, up)


@pytest.fixture(scope="module")
def upstream(cert):
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(*cert)
    plain, tls = _http_server(), _http_server(server_ctx)
    imap_port, imap_seen = _imap_server(server_ctx)
    return {"plain": plain, "tls": tls, "imap_port": imap_port, "imap_seen": imap_seen}


@pytest.fixture
def no_proxy_env(monkeypatch):
    for k in _PROXY_VARS:
        monkeypatch.delenv(k, raising=False)
    ssrf.configure(None)
    yield
    ssrf.configure(None)


@pytest.fixture
def closed_core(monkeypatch, no_proxy_env, cert):
    """core sous Lokkal : aucun nom ne se résout ; le TLS fait confiance au certificat de test (et à lui seul)."""
    asked: list[str] = []

    def getaddrinfo(host, *args, **kwargs):
        try:
            ipaddress.ip_address(str(host).split("%", 1)[0])
        except ValueError:
            asked.append(str(host))
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution") from None
        return _REAL_GETADDRINFO(host, *args, **kwargs)

    def trusting_ctx(*args, **kwargs):
        ctx = _REAL_CTX(cafile=cert[0])
        ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        return ctx

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(ssrf.ssl, "create_default_context", trusting_ctx)
    monkeypatch.setattr(net, "tls_context", trusting_ctx)
    return asked


def _use_proxy(monkeypatch, egress: FakeEgress, no_proxy: str = "localhost,127.0.0.1,core,web"):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(k, egress.url)
    monkeypatch.setenv("NO_PROXY", no_proxy)
    monkeypatch.setenv("no_proxy", no_proxy)


# ─────────────────────────────── par le tunnel, sans DNS ───────────────────────────────


def test_preview_goes_through_proxy_without_dns(monkeypatch, closed_core, upstream, tmp_path):
    egress = FakeEgress({"page.example.com:80": upstream["plain"].server_address[1]})
    _use_proxy(monkeypatch, egress)
    svc = PreviewService(cache_dir=str(tmp_path))  # vrai client épinglé, vrai résolveur (qui échouerait)
    result = svc.preview("http://page.example.com/article")
    assert result["title"] == "Un article par le tunnel"
    assert egress.seen == ["CONNECT page.example.com:80"]
    assert "page.example.com" in upstream["plain"].hosts
    assert closed_core == []  # aucun nom résolu côté core


def test_web_direct_read_goes_through_proxy_with_tls_on_real_name(monkeypatch, closed_core, upstream):
    from nokker.tools.web import _direct_extract

    egress = FakeEgress({"page.example.com:443": upstream["tls"].server_address[1]})
    _use_proxy(monkeypatch, egress)
    doc = _direct_extract("https://page.example.com/article", 5.0)
    assert "lecture directe par trafilatura" in doc["content"]
    assert egress.seen == ["CONNECT page.example.com:443"]
    assert closed_core == []


def test_calendar_ical_goes_through_proxy(monkeypatch, closed_core, upstream):
    egress = FakeEgress({"cal.example.com:443": upstream["tls"].server_address[1]})
    _use_proxy(monkeypatch, egress)
    conn = CalendarConnector({"mode": "ical"}, "https://cal.example.com/agenda.ics")
    assert conn.test() == "agenda iCal lu : 1 événement(s) dans les 30 prochains jours"
    assert egress.seen == ["CONNECT cal.example.com:443"]
    assert closed_core == []


def test_imap_goes_through_proxy_connect_then_tls(monkeypatch, closed_core, upstream):
    egress = FakeEgress({"imap.example.com:993": upstream["imap_port"]})
    _use_proxy(monkeypatch, egress)
    m = connect_imap({"imap_host": "imap.example.com", "imap_port": 993, "username": "alice@example.com"}, "pw")
    m.logout()
    assert egress.seen == ["CONNECT imap.example.com:993"]
    assert "LOGIN" in upstream["imap_seen"]
    assert closed_core == []


def test_tls_certificate_is_checked_on_the_real_name(monkeypatch, closed_core, upstream):
    """Le tunnel ne relâche pas la vérification : un nom absent du certificat est refusé, comme en direct."""
    egress = FakeEgress({"autre.example.org:443": upstream["tls"].server_address[1]})
    _use_proxy(monkeypatch, egress)
    with pytest.raises(FetchError, match="CERTIFICATE_VERIFY_FAILED"):
        guarded_get("https://autre.example.org/", max_bytes=10_000)
    assert egress.seen == ["CONNECT autre.example.org:443"]


# ─────────────────────────────── refus du mandataire ───────────────────────────────


def _direct_refusal(url: str, ip: str) -> str:
    def resolver(host, port, proto=socket.IPPROTO_TCP):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    with pytest.raises(SsrfBlocked) as exc:
        resolve_and_check(url, resolver=resolver)
    return str(exc.value)


def test_egress_403_gives_the_same_refusal_as_the_direct_path(monkeypatch, closed_core):
    egress = FakeEgress({}, deny={"interne.example.com:443": (403, "destination interne refusée : interne.example.com → 10.1.2.3"),
                                  "tail.example.com:443": (403, "destination interne refusée : tail.example.com → 100.101.1.2")})
    _use_proxy(monkeypatch, egress)
    for host, ip in (("interne.example.com", "10.1.2.3"), ("tail.example.com", "100.101.1.2")):
        url = f"https://{host}/x"
        with pytest.raises(SsrfBlocked) as exc:
            guarded_get(url, max_bytes=1000)
        monkeypatch.delenv("HTTPS_PROXY")
        monkeypatch.delenv("https_proxy")
        assert str(exc.value) == _direct_refusal(url, ip)
        _use_proxy(monkeypatch, egress)
    assert str(exc.value) == "IP refusée (CGNAT (100.64.0.0/10, Tailscale)) pour tail.example.com : 100.101.1.2"


def test_unknown_403_and_unknown_name(monkeypatch, closed_core):
    egress = FakeEgress({}, deny={"x.example.com:443": (403, "interdit")})
    _use_proxy(monkeypatch, egress)
    with pytest.raises(SsrfBlocked, match=r"^destination refusée par le proxy de sortie \(HTTP 403\)$"):
        guarded_get("https://x.example.com/", max_bytes=1000)
    with pytest.raises(SsrfBlocked, match=r"^résolution DNS échouée pour absent\.example\.com : nom introuvable$"):
        guarded_get("https://absent.example.com/", max_bytes=1000)


def test_attachment_refusal_keeps_its_wording(monkeypatch, closed_core):
    from nokker.attachments import AttachmentError, TextAttachments

    egress = FakeEgress({}, deny={"interne.example.com:443": (403, "destination interne refusée : interne.example.com → 192.168.1.9")})
    _use_proxy(monkeypatch, egress)
    with pytest.raises(AttachmentError) as exc:
        TextAttachments().parse([{"type": "url", "url": "https://interne.example.com/doc"}])
    assert str(exc.value) == ("pièce 1 : adresse refusée (IP refusée (privée (RFC1918)) pour interne.example.com : "
                              "192.168.1.9)")


def test_imap_and_calendar_refusals_are_host_blocked(monkeypatch, closed_core):
    deny = {"imap.example.com:993": (403, "destination interne refusée : imap.example.com → 10.0.0.7"),
            "cal.example.com:443": (403, "destination interne refusée : cal.example.com → 10.0.0.8")}
    egress = FakeEgress({}, deny=deny)
    _use_proxy(monkeypatch, egress)
    with pytest.raises(ConnectorError) as exc:
        connect_imap({"imap_host": "imap.example.com", "imap_port": 993, "username": "alice@example.com"}, "pw")
    assert (exc.value.code, exc.value.message) == ("host_blocked", "IP refusée (privée (RFC1918)) pour imap.example.com : 10.0.0.7")
    with pytest.raises(ConnectorError) as exc:
        CalendarConnector({"mode": "ical"}, "https://cal.example.com/a.ics").test()
    assert exc.value.code == "host_blocked"
    assert exc.value.message == "IP refusée (privée (RFC1918)) pour cal.example.com : 10.0.0.8"


def test_sso_refusal_through_proxy_is_the_direct_wording(monkeypatch, closed_core):
    from nokker.sso.idp import IdpError, SafeHttp

    egress = FakeEgress({}, deny={"idp.example.com:443": (403, "destination interne refusée : idp.example.com → 10.0.0.9")})
    _use_proxy(monkeypatch, egress)
    with pytest.raises(IdpError) as exc:
        SafeHttp().get("https://idp.example.com/.well-known/openid-configuration")
    assert (exc.value.code, str(exc.value)) == ("unreachable", "adresse refusée")
    assert egress.seen == ["CONNECT idp.example.com:443"]


def test_other_proxy_errors_are_transport_errors(monkeypatch, closed_core):
    egress = FakeEgress({}, deny={"lent.example.com:443": (504, "délai de connexion dépassé")})
    _use_proxy(monkeypatch, egress)
    with pytest.raises(FetchError, match="proxy de sortie : HTTP 504"):
        guarded_get("https://lent.example.com/", max_bytes=1000)


# ─────────────────────────────── garde nominale du mode mandataire ───────────────────────────────


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://2130706433/", "http://0x7f.1/", "http://[::1]/",
                                 "http://10.0.0.1:8080/", "http://169.254.169.254/latest", "http://100.100.1.1/"])
def test_literal_private_ip_refused_before_the_proxy_with_direct_wording(monkeypatch, no_proxy_env, url):
    with pytest.raises(SsrfBlocked) as direct:
        resolve_and_check(url)  # sans mandataire : résolution numérique de la libc, aucun DNS
    egress = FakeEgress({})
    _use_proxy(monkeypatch, egress, no_proxy="")
    with pytest.raises(SsrfBlocked) as proxied:
        resolve_and_check(url)
    assert str(proxied.value) == str(direct.value)
    assert egress.seen == []


def test_literal_ip_with_trailing_dot_is_refused_in_proxy_mode(monkeypatch, no_proxy_env):
    """En direct, `192.168.1.1.` part au DNS (et y échoue) ; en mode mandataire il est lu comme l'IP qu'il écrit et
    refusé avant egress (plus strict, jamais plus large)."""
    egress = FakeEgress({})
    _use_proxy(monkeypatch, egress, no_proxy="")
    with pytest.raises(SsrfBlocked, match=r"^IP refusée \(privée \(RFC1918\)\) pour 192\.168\.1\.1\. : 192\.168\.1\.1$"):
        resolve_and_check("http://192.168.1.1./")
    assert egress.seen == []


def test_scheme_and_strict_ports_still_checked_in_proxy_mode(monkeypatch, no_proxy_env):
    egress = FakeEgress({})
    _use_proxy(monkeypatch, egress)
    with pytest.raises(SsrfBlocked, match="schéma refusé : ftp"):
        resolve_and_check("ftp://example.com/")
    ssrf.configure({"strict": True})
    with pytest.raises(SsrfBlocked, match=r"^port 22 refusé"):
        resolve_and_check("https://example.com:22/", enforce_ports=True)


def test_no_proxy_host_keeps_the_pinned_direct_path(monkeypatch, no_proxy_env):
    egress = FakeEgress({})
    _use_proxy(monkeypatch, egress, no_proxy="localhost,127.0.0.1,openbao,core")

    def resolver(host, port, proto=socket.IPPROTO_TCP):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.18.0.4", port))]

    with pytest.raises(SsrfBlocked, match=r"^IP refusée \(privée \(RFC1918\)\) pour openbao : 172\.18\.0\.4$"):
        resolve_and_check("http://openbao:8200/", resolver=resolver)
    hop = resolve_and_check("https://example.com/", resolver=lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    assert hop.proxy == egress.url and hop.ip == ""


def test_strict_allow_list_entry_stays_direct(monkeypatch, no_proxy_env):
    egress = FakeEgress({})
    _use_proxy(monkeypatch, egress)
    ssrf.configure({"strict": True, "allow": ["mcp-local:8080"]})

    def resolver(host, port, proto=socket.IPPROTO_TCP):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.18.0.9", port))]

    hop = resolve_and_check("http://mcp-local:8080/mcp", resolver=resolver)
    assert hop.proxy is None and hop.ip == "172.18.0.9"


def test_non_http_proxy_is_ignored(monkeypatch, no_proxy_env):
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:1080")
    assert ssrf.egress_proxy("https", "example.com") is None


# ─────────────────────────────── sans mandataire : rien ne change ───────────────────────────────


def test_without_proxy_hop_and_client_are_the_historic_ones(no_proxy_env):
    hop = resolve_and_check("https://example.com/a", resolver=lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    assert hop == ResolvedHop(url="https://example.com/a", scheme="https", hostname="example.com", port=443,
                              ip="93.184.216.34")
    assert hop.proxy is None
    client = ssrf.pinned_client(hop)
    try:
        backend = client._transport._pool._network_backend
        assert type(backend) is ssrf.PinnedBackend and backend._pinned_ip == "93.184.216.34"
    finally:
        client.close()
    with pytest.raises(SsrfBlocked, match=r"^résolution DNS échouée pour absent\.invalid : "):
        def fail(*a, **k):
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        resolve_and_check("https://absent.invalid/", resolver=fail)


def test_without_proxy_core_without_dns_fails_exactly_as_before(no_proxy_env, closed_core):
    """Témoin : sans `HTTPS_PROXY`, un core sans DNS échoue comme avant le correctif (même message)."""
    with pytest.raises(SsrfBlocked, match=r"^résolution DNS échouée pour page\.example\.com : \[Errno -3\] "
                                          r"Temporary failure in name resolution$"):
        guarded_get("http://page.example.com/", max_bytes=1000, resolver=socket.getaddrinfo)
    with pytest.raises(ConnectorError) as exc:
        net.check_endpoint("imap.example.com", 993, "imap", resolver=socket.getaddrinfo)
    assert exc.value.code == "host_blocked"
    assert closed_core == ["page.example.com", "imap.example.com"]
    assert os.environ.get("HTTPS_PROXY") is None


# ─────────────────────────────── client MCP (mode strict), git, Ollama ───────────────────────────────


def test_async_mcp_client_goes_through_proxy(monkeypatch, closed_core, upstream):
    import asyncio

    egress = FakeEgress({"page.example.com:443": upstream["tls"].server_address[1]})
    _use_proxy(monkeypatch, egress)
    hop = resolve_and_check("https://page.example.com/mcp")
    assert hop.proxy == egress.url

    async def go():
        client = ssrf.pinned_async_client_v2(hop)
        async with client:
            r = await client.get("https://page.example.com/mcp")
            return r.status_code

    assert asyncio.run(go()) == 200
    assert egress.seen == ["CONNECT page.example.com:443"]


def test_async_mcp_client_proxy_refusal(monkeypatch, closed_core):
    import asyncio

    import httpx2

    egress = FakeEgress({}, deny={"mcp.example.com:443": (403, "destination interne refusée : mcp.example.com → 10.0.0.3")})
    _use_proxy(monkeypatch, egress)
    hop = resolve_and_check("https://mcp.example.com/mcp")

    async def go():
        async with ssrf.pinned_async_client_v2(hop) as client:
            await client.get("https://mcp.example.com/mcp")

    with pytest.raises(httpx2.ProxyError, match=r"^IP refusée \(privée \(RFC1918\)\) pour mcp\.example\.com : 10\.0\.0\.3$"):
        asyncio.run(go())


def test_git_clone_env_carries_the_proxy_variables(monkeypatch, no_proxy_env):
    from nokker.code_sessions import forge

    seen: dict[str, str] = {}

    def run(argv, env, timeout):
        seen.update(env)
        return 0, ""

    forge.clone("https://example.com/alice/depot.git", "/tmp/x", None, run=run)
    assert not any(k in seen for k in _PROXY_VARS)  # sans mandataire : environnement d'avant, à l'identique
    without = dict(seen)
    for k, v in (("HTTPS_PROXY", "http://egress:3128"), ("https_proxy", "http://egress:3128"),
                 ("HTTP_PROXY", "http://egress:3128"), ("NO_PROXY", "localhost,core"), ("no_proxy", "localhost,core"),
                 ("ALL_PROXY", "socks5://x:1"), ("SECRET_TOKEN", "s")):
        monkeypatch.setenv(k, v)
    seen.clear()
    forge.clone("https://example.com/alice/depot.git", "/tmp/x", None, run=run)
    assert {k: seen[k] for k in seen if k not in without} == {
        "HTTPS_PROXY": "http://egress:3128", "https_proxy": "http://egress:3128", "HTTP_PROXY": "http://egress:3128",
        "NO_PROXY": "localhost,core", "no_proxy": "localhost,core"}
    assert {k: v for k, v in seen.items() if k in without and k != "HOME"} == \
        {k: v for k, v in without.items() if k != "HOME"}


def test_ollama_default_candidates_skip_host_docker_internal(monkeypatch):
    from nokker.local import setup

    monkeypatch.delenv(setup.OLLAMA_URLS_ENV, raising=False)
    assert setup.ollama_candidates() == ("http://127.0.0.1:11434",)
    assert not any("host.docker.internal" in u for u in setup.OLLAMA_CANDIDATES)
