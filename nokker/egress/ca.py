"""Autorité de certification du proxy de sortie : le proxy termine TLS pour lire les requêtes d'une session qui a des
secrets accordés (il ne peut pas poser une valeur dans un flux chiffré qu'il ne voit pas).

- `ca.key` (0600, créé sans suivre un lien) ne quitte jamais le proxy ; `ca.crt` est public : le noyau le dépose dans
  l'espace de travail de la session et y fait pointer SSL_CERT_FILE et consorts.
- Certificats feuilles : un par hôte, signé à la demande, gardé en mémoire (24 h de validité, renouvelé avant).
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import os
import ssl
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_NAME = "Nokker egress CA"
LEAF_TTL = dt.timedelta(hours=24)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _write_private(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def new_ca(days: int = 3650) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME)])
    now = _now()
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return key, cert


@dataclass
class CertAuthority:
    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate
    _leaves: dict[str, tuple[ssl.SSLContext, dt.datetime]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __repr__(self) -> str:
        return f"CertAuthority({self.cert.subject.rfc4514_string()})"

    @classmethod
    def load_or_create(cls, directory: str | Path) -> CertAuthority:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        kp, cp = d / "ca.key", d / "ca.crt"
        if kp.exists() and cp.exists():
            key = serialization.load_pem_private_key(kp.read_bytes(), password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey):
                raise ValueError("ca.key : clé EC attendue")
            return cls(key=key, cert=x509.load_pem_x509_certificate(cp.read_bytes()))
        if kp.exists() or cp.exists():
            raise ValueError(f"{d} : ca.key et ca.crt vont ensemble (un seul des deux présent)")
        key, cert = new_ca()
        _write_private(kp, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
        cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        os.chmod(cp, 0o644)
        return cls(key=key, cert=cert)

    def cert_pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)

    def leaf_context(self, host: str) -> ssl.SSLContext:
        """Contexte TLS serveur pour `host` (certificat feuille signé par l'AC, http/1.1 seulement)."""
        host = host.strip("[]").lower()
        with self._lock:
            got = self._leaves.get(host)
            if got is not None and got[1] - _now() > dt.timedelta(hours=1):
                return got[0]
            key = ec.generate_private_key(ec.SECP256R1())
            try:
                san: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(host))
            except ValueError:
                san = x509.DNSName(host)
            now = _now()
            until = now + LEAF_TTL
            cert = (x509.CertificateBuilder()
                    .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host[:64])]))
                    .issuer_name(self.cert.subject).public_key(key.public_key())
                    .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
                    .not_valid_after(until)
                    .add_extension(x509.SubjectAlternativeName([san]), critical=False)
                    .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                    .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                    .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()),
                                   critical=False)
                    .sign(self.key, hashes.SHA256()))
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.set_alpn_protocols(["http/1.1"])
            # `load_cert_chain` ne lit que des fichiers : un dossier temporaire privé, effacé aussitôt.
            with tempfile.TemporaryDirectory(prefix="nokker-egress-") as tmp:
                cf, kf = Path(tmp) / "c.pem", Path(tmp) / "k.pem"
                cf.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + self.cert_pem())
                _write_private(kf, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
                ctx.load_cert_chain(cf, kf)
            self._leaves[host] = (ctx, until)
            return ctx


__all__ = ["CA_NAME", "CertAuthority", "new_ca"]
