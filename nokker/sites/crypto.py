"""Sites connectés — chiffrement (docs/CONNECTED-SITES.md §5.2, §5.5, §15).

1. **Profils au repos** : AES-256-GCM, clé dérivée par HKDF-SHA256 de la clé maître (`sites-master`, coffre système)
   avec `info = "nokker-sites-profile-v1|" + compte + "|" + connecteur + "|" + génération` ; AAD = la même chaîne.
   Une nouvelle génération à chaque sauvegarde : une archive ne se déchiffre qu'avec SA génération.
2. **Saisie scellée** (mot de passe, code, lien) : la machine publie une clé publique ÉPHÉMÈRE P-256 par demande (WebCrypto
   la sait partout) ; la page génère sa propre paire, dérive la clé par ECDH + HKDF-SHA256 (`info = "nokker-sites-v1|"
   + rid`, sel vide), chiffre `{"value": …}` en AES-256-GCM et envoie `{v, epk (JWK), iv, ct}` (base64url). nokker.ai et
   le relais ne voient que du chiffré. Même algorithme dans `sealed.js` (référence de la page).
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class SealedError(ValueError):
    pass


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(text: str) -> bytes:
    text = str(text or "")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hkdf(secret: bytes, info: str, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info.encode("utf-8")).derive(secret)


# ─────────────────────────────── profils au repos ───────────────────────────────

def profile_context(account: str, connector: str, generation: int) -> str:
    return f"nokker-sites-profile-v1|{account}|{connector}|{generation}"


def seal_profile(master: bytes, account: str, connector: str, generation: int, archive: bytes) -> bytes:
    ctx = profile_context(account, connector, generation)
    nonce = os.urandom(12)
    return nonce + AESGCM(_hkdf(master, ctx)).encrypt(nonce, archive, ctx.encode("utf-8"))


def open_profile(master: bytes, account: str, connector: str, generation: int, blob: bytes) -> bytes:
    ctx = profile_context(account, connector, generation)
    return AESGCM(_hkdf(master, ctx)).decrypt(blob[:12], blob[12:], ctx.encode("utf-8"))


# ─────────────────────────────── saisie scellée ───────────────────────────────

def _pub_jwk(pub: ec.EllipticCurvePublicKey) -> dict[str, str]:
    n = pub.public_numbers()
    return {"kty": "EC", "crv": "P-256", "x": b64u(n.x.to_bytes(32, "big")), "y": b64u(n.y.to_bytes(32, "big"))}


def _jwk_pub(jwk: dict) -> ec.EllipticCurvePublicKey:
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise SealedError("clé éphémère invalide")
    x = int.from_bytes(unb64u(jwk.get("x", "")), "big")
    y = int.from_bytes(unb64u(jwk.get("y", "")), "big")
    try:
        return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    except ValueError:
        raise SealedError("clé éphémère invalide") from None


def _sealed_key(priv: ec.EllipticCurvePrivateKey, peer: ec.EllipticCurvePublicKey, rid: str) -> bytes:
    return _hkdf(priv.exchange(ec.ECDH(), peer), f"nokker-sites-v1|{rid}")


def seal_value(machine_jwk: dict, rid: str, value: str) -> dict:
    """Ce que fait la page (sealed.js) — en Python pour les tests et les clients CLI."""
    eph = ec.generate_private_key(ec.SECP256R1())
    key = _sealed_key(eph, _jwk_pub(machine_jwk), rid)
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, json.dumps({"value": value}).encode("utf-8"), rid.encode("utf-8"))
    return {"v": 1, "epk": _pub_jwk(eph.public_key()), "iv": b64u(iv), "ct": b64u(ct)}


@dataclass
class _Pending:
    priv: ec.EllipticCurvePrivateKey
    expires_at: float


class MachineKeyring:
    """Une paire P-256 éphémère par demande (`rid`), 10 min au plus, oubliée après usage : une réponse ne s'ouvre
    qu'une fois, et jamais après l'échéance."""

    def __init__(self, ttl_s: float = 600.0, clock=time.time) -> None:
        self.ttl_s = ttl_s
        self.clock = clock
        self._keys: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def public_key(self, rid: str, ttl_s: float | None = None) -> dict[str, str]:
        priv = ec.generate_private_key(ec.SECP256R1())
        with self._lock:
            self._keys[rid] = _Pending(priv, self.clock() + min(ttl_s or self.ttl_s, self.ttl_s))
        return _pub_jwk(priv.public_key())

    def open(self, rid: str, sealed: dict) -> str:
        with self._lock:
            pending = self._keys.pop(rid, None)
        if pending is None or self.clock() > pending.expires_at:
            raise SealedError("demande inconnue ou expirée")
        if not isinstance(sealed, dict) or sealed.get("v") != 1:
            raise SealedError("format de saisie inconnu")
        try:
            key = _sealed_key(pending.priv, _jwk_pub(sealed.get("epk")), rid)
            plain = AESGCM(key).decrypt(unb64u(sealed.get("iv", "")), unb64u(sealed.get("ct", "")),
                                        rid.encode("utf-8"))
            value = json.loads(plain.decode("utf-8")).get("value")
        except SealedError:
            raise
        except Exception:  # noqa: BLE001 — jamais de détail : rien sur la valeur
            raise SealedError("saisie illisible") from None
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise SealedError("saisie vide ou trop longue")
        return value

    def forget(self, rid: str) -> None:
        with self._lock:
            self._keys.pop(rid, None)


def new_master_key() -> str:
    return b64u(os.urandom(32))


def master_from_text(text: str) -> bytes:
    raw = unb64u(text)
    if len(raw) != 32:
        raise ValueError("clé maître des sites invalide")
    return raw


__all__ = ["MachineKeyring", "SealedError", "b64u", "master_from_text", "new_master_key", "open_profile",
           "seal_profile", "seal_value", "unb64u"]

