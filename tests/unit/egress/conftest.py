"""Banc de l'injection des secrets à la sortie : un amont HTTPS local (certificats d'une AC de test, un par nom via SNI)
qui renvoie tout ce qu'il reçoit, le vrai proxy (`nokker.egress.proxy`) dans un fil, et un « bac à sable » = un
processus Python séparé qui ne reçoit QUE l'environnement rendu par le noyau. Aucun réseau extérieur : les noms
`*.example.test` sont épinglés sur 127.0.0.1 (option de test du proxy)."""
from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import os
import ssl
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from nokker.egress.ca import CertAuthority, new_ca
from nokker.egress.kernel import EgressSecrets, EgressSecretsConfig
from nokker.egress.proxy import EgressProxy, ProxyConfig

CONTROL_KEY = "k" * 40
HOSTS = ("api.example.test", "other.example.test", "evil.example.test")


@dataclass
class Upstream:
    port: int
    ca_pem: Path
    seen: list[dict[str, Any]] = field(default_factory=list)


class _Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[dict[str, Any]] = []

    def log_message(self, *a: Any) -> None:
        pass

    def _serve(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        rec = {"host": self.headers.get("Host"), "method": self.command, "path": self.path,
               "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
        self.seen.append(rec)
        out = json.dumps(rec).encode()
        enc = None
        if self.path.startswith("/gzip"):
            out, enc = gzip.compress(out), "gzip"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if enc:
            self.send_header("Content-Encoding", enc)
        # La valeur reçue renvoyée aussi dans un en-tête de réponse (fuite inverse par les en-têtes)
        self.send_header("X-Echo-Auth", self.headers.get("Authorization") or "-")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = _serve


@pytest.fixture
def upstream(tmp_path):
    key, cert = new_ca()
    up_ca = CertAuthority(key=key, cert=cert)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(*_leaf_files(up_ca, HOSTS[0], tmp_path))

    def sni(sock: ssl.SSLSocket, name: str | None, _c: Any) -> None:
        if name:
            sock.context = up_ca.leaf_context(name)

    ctx.sni_callback = sni
    seen: list[dict[str, Any]] = []
    handler = type("Echo", (_Echo,), {"seen": seen})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    pem = tmp_path / "upstream-ca.pem"
    pem.write_bytes(up_ca.cert_pem())
    yield Upstream(port=srv.server_address[1], ca_pem=pem, seen=seen)
    srv.shutdown()


def _leaf_files(ca: CertAuthority, host: str, d: Path) -> tuple[str, str]:
    """Certificat par défaut de l'amont (avant SNI) en fichiers."""
    k = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    c = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
         .issuer_name(ca.cert.subject).public_key(k.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(hours=1))
         .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
         .sign(ca.key, hashes.SHA256()))
    cf, kf = d / "up.crt", d / "up.key"
    cf.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    kf.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
    return str(cf), str(kf)


@dataclass
class RunningProxy:
    proxy: EgressProxy
    port: int
    control_port: int
    ca_dir: Path
    loop: asyncio.AbstractEventLoop


@pytest.fixture
def proxy(tmp_path, upstream):
    ca_dir = tmp_path / "egress-ca"
    up_ctx = ssl.create_default_context(cafile=str(upstream.ca_pem))
    cfg = ProxyConfig(ca=CertAuthority.load_or_create(ca_dir), control_key=CONTROL_KEY, listen_host="127.0.0.1",
                      listen_port=0, control_host="127.0.0.1", control_port=0,
                      pins={h: "127.0.0.1" for h in HOSTS}, upstream_ssl=up_ctx)
    p = EgressProxy(cfg)
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    ports: list[int] = []

    def run() -> None:
        asyncio.set_event_loop(loop)
        ports.extend(loop.run_until_complete(p.start()))
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(10)
    yield RunningProxy(proxy=p, port=ports[0], control_port=ports[1], ca_dir=ca_dir, loop=loop)
    asyncio.run_coroutine_threadsafe(p.close(), loop).result(10)
    loop.call_soon_threadsafe(loop.stop)
    t.join(10)


def egress_block(proxy: RunningProxy, secrets: dict[str, Any] | None = None, **over: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "enabled": True, "proxy_url": f"http://127.0.0.1:{proxy.port}",
        "control_url": f"http://127.0.0.1:{proxy.control_port}", "ca_cert": str(proxy.ca_dir / "ca.crt"),
        "secrets": secrets if secrets is not None else {
            "GH_TOKEN": {"hosts": ["api.example.test"], "headers": ["authorization", "x-api-key"],
                         "query": ["key"], "fields": ["auth.token", "token"]},
        },
    }
    block.update(over)
    return block


@pytest.fixture
def egress(proxy, monkeypatch):
    monkeypatch.setenv("NOKKER_EGRESS_CONTROL_KEY", CONTROL_KEY)
    cfg = EgressSecretsConfig.from_config(egress_block(proxy))
    assert cfg is not None
    return EgressSecrets(cfg)


SANDBOX_CLIENT = r"""
import json, os, sys, urllib.request, urllib.error
url, headers, body = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3]
req = urllib.request.Request(url, data=body.encode() if body else None, headers=headers,
                             method="POST" if body else "GET")
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        print(json.dumps({"status": r.status, "headers": dict(r.headers), "body": r.read().decode()}))
except urllib.error.HTTPError as e:
    print(json.dumps({"status": e.code, "headers": dict(e.headers), "body": e.read().decode()}))
except urllib.error.URLError as e:
    print(json.dumps({"status": None, "headers": {}, "body": str(e.reason)}))
print("ENV=" + json.dumps(dict(os.environ)))
"""


def run_sandbox(env: dict[str, str], url: str, headers: dict[str, str] | None = None,
                body: str = "") -> tuple[dict[str, Any], dict[str, str], str]:
    """Le « bac à sable » : un processus qui ne voit que `env` (+ PATH). Rend (réponse, environnement, sortie brute)."""
    full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env}
    p = subprocess.run([sys.executable, "-I", "-c", SANDBOX_CLIENT, url, json.dumps(headers or {}), body],
                       env=full, capture_output=True, text=True, timeout=60)
    out = p.stdout + p.stderr
    lines = p.stdout.splitlines()
    assert lines and lines[0].startswith("{"), out
    resp = json.loads(lines[0])
    env_line = next(ln for ln in lines if ln.startswith("ENV="))
    return resp, json.loads(env_line[4:]), out
