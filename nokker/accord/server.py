"""ACCORD — serveur WSS sur le LAN de Lokkal (docs/ACCORD-DEVICE.md §1) et son certificat.

Éteint tant que `local.accord.enabled` est faux ; il n'écoute que sur l'adresse donnée (`NOKKER_ACCORD_BIND`,
`hôte:port`, publiée par le compose sur l'adresse LAN seulement, comme home-bridge). Certificat auto-signé tiré au
premier démarrage dans `<état>/accord/` (clé 0600) : son empreinte SHA-256 est `host_fp`, que l'appareil épingle à
l'appairage ; il ne change pas tant que le dossier d'état reste. Une connexion = un fil (websockets sync), qui relaie
les messages à `AccordHub` et relève la file toutes les `POLL_S` secondes."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import ssl
import threading
from pathlib import Path
from typing import Any

from nokker.accord import protocol as P
from nokker.accord.hub import AccordHub

logger = logging.getLogger(__name__)

POLL_S = 0.5
MAX_MESSAGE = 4096  # un message du protocole tient en quelques centaines d'octets


def ensure_cert(directory: Path) -> tuple[Path, Path, str]:
    """(cert.pem, key.pem, host_fp) : tirés une fois (Ed25519 n'est pas accepté partout en TLS : ECDSA P-256)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    cert_p, key_p = directory / "cert.pem", directory / "key.pem"
    if not (cert_p.is_file() and key_p.is_file()):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nokker-accord")])
        now = dt.datetime.now(dt.UTC)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
                .not_valid_after(now + dt.timedelta(days=3650)).sign(key, hashes.SHA256()))
        fd = os.open(key_p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
        cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    from cryptography import x509 as _x509
    from cryptography.hazmat.primitives.serialization import Encoding

    der = _x509.load_pem_x509_certificate(cert_p.read_bytes()).public_bytes(Encoding.DER)
    return cert_p, key_p, P.cert_fingerprint(der)


def tls_context(cert_p: Path, key_p: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_p, key_p)
    # Aucun ticket de session TLS 1.3 (18.11.2026, 9 % d'ouvertures perdues sur 200) : le serveur les envoie APRÈS le
    # handshake ; un client à deux fils (websockets sync : un fil lit, l'autre écrit la requête d'upgrade) les lit
    # pendant qu'il écrit sur le même SSLSocket, et la requête se perd (EOF côté serveur) ou OpenSSL lève « internal
    # error ». ACCORD ne reprend jamais de session (hello/auth frais à chaque connexion) : les tickets ne servent à rien.
    ctx.num_tickets = 0
    return ctx


def _connection(hub: AccordHub, ws: Any) -> None:
    s = hub.session()
    try:
        while not s.closed or s.revoked:
            try:
                raw = ws.recv(timeout=POLL_S)
            except TimeoutError:
                raw = None
            if raw is not None:
                try:
                    msg = json.loads(raw) if isinstance(raw, str) and len(raw) <= MAX_MESSAGE else None
                except json.JSONDecodeError:
                    msg = None
                for out in s.handle(msg):
                    ws.send(json.dumps(out, ensure_ascii=False))
            if s.revoked:  # dit à l'appareil, puis connexion fermée
                break
            for out in s.poll():
                ws.send(json.dumps(out, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 — déconnexion, TLS, appareil disparu
        logger.info("accord : connexion fermée (%s)", type(e).__name__)
    finally:
        hub.close(s)


class AccordServer:
    def __init__(self, hub: AccordHub, host: str, port: int, ctx: ssl.SSLContext) -> None:
        from websockets.sync.server import serve

        self.hub = hub
        self._server = serve(lambda ws: _connection(hub, ws), host, port, ssl=ctx, max_size=MAX_MESSAGE)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="accord-wss")

    def start(self) -> AccordServer:
        self._thread.start()
        logger.info("accord : WSS en écoute sur le port %d (empreinte %s…)", self.port, self.hub.host_fp[:8])
        return self

    def stop(self) -> None:
        self._server.shutdown()


def parse_bind(text: str) -> tuple[str, int] | None:
    host, sep, port = (text or "").strip().rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        return None
    return host.strip("[]"), int(port)
