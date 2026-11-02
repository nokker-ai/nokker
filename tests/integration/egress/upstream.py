"""Amont de test : HTTPS sur 443 (AC propre, écrite dans /up/ca.crt), répond {auth_ok, echo_auth} et renvoie
l'Authorization reçue dans l'en-tête X-Echo-Auth (fuite inverse que le proxy doit caviarder)."""
import json
import os
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from nokker.egress.ca import CertAuthority

ca = CertAuthority.load_or_create("/tmp/upca")
EXPECTED = os.environ["EXPECTED"]


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        auth = self.headers.get("Authorization") or ""
        body = json.dumps({"auth_ok": auth == f"Bearer {EXPECTED}", "echo_auth": auth}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Echo-Auth", auth)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.sni_callback = lambda sock, name, _c: setattr(sock, "context", ca.leaf_context(name or "api.example.test"))
# contexte par défaut (sans SNI) : feuille de api.example.test
leaf = ca.leaf_context("api.example.test")
srv = ThreadingHTTPServer(("0.0.0.0", 443), H)
srv.socket = leaf.wrap_socket(srv.socket, server_side=True)
leaf.sni_callback = ctx.sni_callback
Path("/up/ca.crt").write_bytes(ca.cert.public_bytes(serialization.Encoding.PEM))
srv.serve_forever()
