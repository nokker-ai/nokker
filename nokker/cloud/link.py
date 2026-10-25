"""Côté nokker.ai du lien avec Nokker Lokkal (docs/LOKKAL.md §7.2 révisé et §7.5) : flux d'appareil, jetons de machine,
jetons signés pour la passerelle.

- **Flux d'appareil** : `device()` (sans auth, limité par IP chez l'appelant) → codes ; l'utilisateur connecté approuve
  le `user_code` (`approve`) ; Lokkal sonde `token()` et reçoit UNE fois le jeton de machine. Jetons et codes ne sont
  gardés qu'en empreinte (sha256) : une fuite de la base ne donne aucun jeton utilisable.
- **Jeton de machine** : authentifie la machine pour l'inférence relayée (`/v1/inference/*`), le tunnel et
  `/v1/gateway/machine` ; révocable depuis « mes machines ».
- **Jetons signés ES256** (JWKS publié) : accès (navigateur → `<nom>.nokker.app`, 15 min) et délégation (skill
  « sessions Lokkal », 5 min, portée `lokkal:session`) ; le relais et le noyau local les vérifient avec la clé publique.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

ISSUER = "https://app.nokker.ai"
AUDIENCE = "nokker.app"
_USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ23456789"  # sans voyelles ni caractères ambigus


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def slug(name: str) -> str:
    import unicodedata

    ascii_name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9-]+", "-", ascii_name.lower()).strip("-")[:30]
    return s or "machine"


# ─────────────────────────────── jetons ES256 ───────────────────────────────


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@dataclass
class Signer:
    """Clé ES256 de nokker.ai (PEM, secret du déploiement) : signe les jetons et publie le JWKS."""

    private_pem: bytes
    kid: str = "nokker-gw-1"
    issuer: str = ISSUER  # = base_url de nokker.ai (cloud_link) ; vérifié tel quel par le relais et le noyau local

    def __post_init__(self) -> None:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        self._key = load_pem_private_key(self.private_pem, password=None)

    @classmethod
    def generate(cls, kid: str = "nokker-gw-1") -> Signer:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        return cls(private_pem=pem, kid=kid)

    def jwks(self) -> dict[str, Any]:
        nums = self._key.public_key().public_numbers()
        return {"keys": [{"kty": "EC", "crv": "P-256", "alg": "ES256", "use": "sig", "kid": self.kid,
                          "x": _b64u(nums.x.to_bytes(32, "big")), "y": _b64u(nums.y.to_bytes(32, "big"))}]}

    def sign(self, claims: dict[str, Any]) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        head = _b64u(json.dumps({"alg": "ES256", "typ": "JWT", "kid": self.kid}, separators=(",", ":")).encode())
        body = _b64u(json.dumps(claims, separators=(",", ":")).encode())
        r, s = decode_dss_signature(self._key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
        return f"{head}.{body}.{_b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"

    def access_token(self, account: str, tenant: str, ttl_s: int = 900, now: float | None = None,
                     machine: str | None = None) -> str:
        """Jeton d'accès §7.5 ; `machine` le lie à UNE machine (connexion par la passerelle, §7.3)."""
        t = int(now or time.time())
        claims = {"iss": self.issuer, "aud": AUDIENCE, "sub": account, "tenant": tenant, "iat": t, "exp": t + ttl_s,
                  "jti": uuid.uuid4().hex}
        if machine:
            claims["machine"] = machine
        return self.sign(claims)

    def pages_token(self, account: str, tenant: str, machine: str, ttl_s: int = 60, now: float | None = None) -> str:
        """Jeton d'entrée sur `<nom>--pages.nokker.app/_auth` (docs/BUBBLES.md §10.3) : portée `pages`, 60 s, la machine
        nommée ; usage unique tenu par le relais (jti)."""
        t = int(now or time.time())
        return self.sign({"iss": self.issuer, "aud": AUDIENCE, "sub": account, "tenant": tenant, "machine": machine,
                          "scope": "pages", "iat": t, "exp": t + min(ttl_s, 60), "jti": uuid.uuid4().hex})

    def delegation_token(self, account: str, tenant: str, machine: str, web_session: str, ttl_s: int = 300,
                         now: float | None = None) -> str:
        t = int(now or time.time())
        return self.sign({"iss": self.issuer, "aud": AUDIENCE, "sub": account, "tenant": tenant, "machine": machine,
                          "scope": "lokkal:session", "web_session": web_session, "iat": t, "exp": t + ttl_s,
                          "jti": uuid.uuid4().hex})


class TokenError(ValueError):
    """Jeton signé refusé (forme, signature, émetteur, audience, expiration, machine)."""


def verify_token(token: str, jwks: dict[str, Any], *, machine: str | None = None, now: float | None = None,
                 issuer: str = ISSUER) -> dict:
    """Vérifie un jeton ES256 de nokker.ai contre le JWKS (noyau local et relais). `machine` : si le jeton nomme une
    machine (délégation), elle doit être celle-ci."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    try:
        head_s, body_s, sig_s = token.split(".")
        head, claims, sig = json.loads(_b64u_dec(head_s)), json.loads(_b64u_dec(body_s)), _b64u_dec(sig_s)
    except (ValueError, json.JSONDecodeError) as e:
        raise TokenError("jeton illisible") from e
    if head.get("alg") != "ES256" or len(sig) != 64:
        raise TokenError("algorithme refusé")
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == head.get("kid")), None)
    if key is None:
        raise TokenError("clé inconnue")
    pub = ec.EllipticCurvePublicNumbers(int.from_bytes(_b64u_dec(key["x"]), "big"),
                                        int.from_bytes(_b64u_dec(key["y"]), "big"), ec.SECP256R1()).public_key()
    try:
        pub.verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")),
                   f"{head_s}.{body_s}".encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as e:
        raise TokenError("signature invalide") from e
    t = now or time.time()
    if claims.get("iss") != issuer or claims.get("aud") != AUDIENCE:
        raise TokenError("émetteur ou audience refusés")
    if not isinstance(claims.get("exp"), int) or t >= claims["exp"]:
        raise TokenError("jeton expiré")
    if "machine" in claims and machine is not None and claims["machine"] != machine:
        raise TokenError("jeton d'une autre machine")
    return claims


# ─────────────────────────────── machines et flux d'appareil ───────────────────────────────


@dataclass
class Machine:
    id: str
    account: str
    tenant: str
    name: str
    host: str  # <nom>.nokker.app
    token_sha256: str
    created_at: float
    revoked: bool = False
    last_seen_at: float | None = None  # dernier GET /v1/gateway/machine du relais (écran « mes machines »)
    # Charge courante (lot X5) : gardée à part, une ligne par machine sous la RLS du locataire (migration 020) ;
    # posée ici seulement par `LinkService.machines_with_load`.
    load: dict[str, Any] | None = None
    load_at: float | None = None  # heure de nokker.ai à la réception (pas l'horloge de la machine)


@dataclass
class _Request:
    device_sha: str
    user_code: str
    machine_name: str
    expires_at: float
    state: str = "pending"  # pending | approved | denied
    machine_id: str | None = None


class MemoryLinkStore:
    """Magasin en mémoire (tests, dev). `PgLinkStore` (nokker/cloud/pg.py) a la même interface."""

    def __init__(self) -> None:
        self.requests: dict[str, _Request] = {}
        self.machines: dict[str, Machine] = {}
        self.loads: dict[str, tuple[str, dict[str, Any], float]] = {}  # machine → (locataire, charge, reçue à)
        self.lock = threading.Lock()

    def add_request(self, r: _Request) -> None:
        with self.lock:
            self.requests[r.device_sha] = r

    def request(self, device_sha: str) -> _Request | None:
        with self.lock:
            return self.requests.get(device_sha)

    def pending_by_code(self, user_code: str) -> _Request | None:
        with self.lock:
            return next((r for r in self.requests.values() if r.user_code == user_code and r.state == "pending"), None)

    def save_request(self, r: _Request) -> None:
        self.add_request(r)

    def drop_request(self, device_sha: str) -> None:
        with self.lock:
            self.requests.pop(device_sha, None)

    def hosts(self) -> set[str]:
        with self.lock:
            return {m.host for m in self.machines.values()}

    def add_machine(self, m: Machine) -> None:
        with self.lock:
            self.machines[m.id] = m

    def machine(self, machine_id: str) -> Machine | None:
        with self.lock:
            return self.machines.get(machine_id)

    def set_token(self, machine_id: str, token_sha: str) -> None:
        with self.lock:
            self.machines[machine_id].token_sha256 = token_sha

    def by_token(self, token_sha: str) -> Machine | None:
        with self.lock:
            return next((m for m in self.machines.values() if m.token_sha256 == token_sha and not m.revoked), None)

    def revoke(self, machine_id: str) -> None:
        with self.lock:
            self.machines[machine_id].revoked = True

    def of_account(self, account: str) -> list[Machine]:
        with self.lock:
            return [m for m in self.machines.values() if m.account == account and not m.revoked]

    def seen(self, machine_id: str, at: float) -> None:
        with self.lock:
            if machine_id in self.machines:
                self.machines[machine_id].last_seen_at = at

    def set_load(self, machine_id: str, tenant: str, load: dict[str, Any], at: float) -> None:
        with self.lock:
            self.loads[machine_id] = (tenant, load, at)

    def loads_of(self, tenant: str, machine_ids: list[str]) -> dict[str, tuple[dict[str, Any], float]]:
        """Charges des machines demandées, de CE locataire seulement (comme la RLS de la migration 020)."""
        with self.lock:
            return {mid: (v[1], v[2]) for mid in machine_ids if (v := self.loads.get(mid)) and v[0] == tenant}


@dataclass
class LinkService:
    """Flux d'appareil et registre des machines. Le jeton de machine n'existe en clair qu'une fois, dans la réponse de
    `token()` qui le crée : le magasin n'en garde jamais que l'empreinte."""

    base_url: str = ISSUER
    gateway_domain: str = "nokker.app"
    ttl_s: int = 600
    clock: Any = time.time
    store: Any = field(default_factory=MemoryLinkStore)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def device(self, machine_name: str) -> dict[str, Any]:
        device_code = secrets.token_urlsafe(32)
        user_code = "-".join("".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(4)) for _ in range(2))
        self.store.add_request(_Request(device_sha=_sha(device_code), user_code=user_code,
                                        machine_name=machine_name[:60], expires_at=self.clock() + self.ttl_s))
        return {"device_code": device_code, "user_code": user_code, "verification_uri": f"{self.base_url}/link",
                "expires_in": self.ttl_s, "interval": 5}

    def approve(self, user_code: str, account: str, tenant: str, decision: str = "approve") -> dict[str, Any]:
        with self._lock:
            req = self.store.pending_by_code(user_code.strip().upper())
            if req is None or self.clock() > req.expires_at:
                raise LookupError("code inconnu ou expiré")
            if decision != "approve":
                req.state = "denied"
                self.store.save_request(req)
                return {"state": "denied"}
            base, taken = slug(req.machine_name), self.store.hosts()
            host, n = f"{base}.{self.gateway_domain}", 2
            while host in taken:
                host, n = f"{base}-{n}.{self.gateway_domain}", n + 1
            m = Machine(id=f"mc_{uuid.uuid4().hex[:16]}", account=account, tenant=tenant, name=req.machine_name,
                        host=host, token_sha256="", created_at=self.clock())
            self.store.add_machine(m)
            req.state, req.machine_id = "approved", m.id
            self.store.save_request(req)
            return {"state": "approved", "machine_id": m.id, "host": host}

    def pending(self, user_code: str) -> dict[str, Any] | None:
        """Le flux en attente pour ce code, SANS effet de bord (page /link : montrer la machine avant d'approuver) ;
        None pour un code inconnu comme expiré (pas d'oracle)."""
        req = self.store.pending_by_code(user_code.strip().upper())
        if req is None or self.clock() > req.expires_at:
            return None
        return {"machine_name": req.machine_name, "expires_in": max(0, int(req.expires_at - self.clock()))}

    def token(self, device_code: str) -> dict[str, Any]:
        with self._lock:
            req = self.store.request(_sha(device_code))
            if req is None:
                return {"state": "expired"}
            if req.state == "pending" and self.clock() > req.expires_at:
                self.store.drop_request(req.device_sha)
                return {"state": "expired"}
            if req.state != "approved":
                return {"state": req.state}
            m = self.store.machine(req.machine_id or "")
            if m is None:
                self.store.drop_request(req.device_sha)
                return {"state": "expired"}
            token = secrets.token_urlsafe(32)  # créé ici, rendu une seule fois, gardé en empreinte
            self.store.set_token(m.id, _sha(token))
            self.store.drop_request(req.device_sha)
        return {"state": "approved", "api_key": token, "machine_token": token, "account": m.account,
                "machine_id": m.id, "relay_host": m.host, "inference_base": f"{self.base_url}/v1/inference"}

    def machine_for_token(self, token: str) -> Machine | None:
        return self.store.by_token(_sha(token or "")) if token else None

    def revoke(self, token: str) -> bool:
        m = self.machine_for_token(token)
        if m is None:
            return False
        self.store.revoke(m.id)
        return True

    def machines_of(self, account: str) -> list[Machine]:
        return self.store.of_account(account)

    def machine_of(self, account: str, machine_id: str) -> Machine | None:
        """La machine non révoquée de CE compte, sinon None (sans dire si elle existe ailleurs)."""
        return next((m for m in self.store.of_account(account) if m.id == machine_id), None)

    def report_load(self, machine: Machine, raw: Any) -> dict[str, Any] | None:
        """Charge envoyée par la machine (`POST /v1/gateway/machine/load`), nettoyée puis gardée (une ligne courante
        par machine, l'heure est celle de nokker.ai). None si le corps n'est pas une charge."""
        from nokker.local.load import clean_load

        load = clean_load(raw)
        if load is None:
            return None
        self.store.set_load(machine.id, machine.tenant, load, self.clock())
        return load

    def machines_with_load(self, account: str, tenant: str) -> list[Machine]:
        """Les machines du compte (« mes machines »), chacune avec sa dernière charge connue."""
        machines = self.store.of_account(account)
        loads = self.store.loads_of(tenant, [m.id for m in machines]) if machines else {}
        from dataclasses import replace

        return [replace(m, load=loads[m.id][0], load_at=loads[m.id][1]) if m.id in loads else m for m in machines]

    def revoke_machine(self, account: str, machine_id: str) -> bool:
        m = self.machine_of(account, machine_id)
        if m is None:
            return False
        self.store.revoke(m.id)
        return True


__all__ = ["AUDIENCE", "ISSUER", "LinkService", "Machine", "MemoryLinkStore", "Signer", "TokenError", "slug",
           "verify_token"]
