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
ATTEST_TYP = "nokker-approval-attest"  # `typ` (en-tête et claim) d'une attestation de décision Telegram
ATTEST_TTL_S = 120
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


class SigningKeyError(ValueError):
    """Clé de la passerelle illisible ou d'un mauvais type. Le message dit pourquoi, JAMAIS la clé (pas d'exception
    chaînée de `cryptography` non plus)."""


def _signing_key(pem: bytes) -> Any:
    """La clé privée EC P-256 du PEM, ou `SigningKeyError` avec la cause la plus probable d'une clé mal collée."""
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    text = pem.decode("utf-8", "replace").strip()
    if text[:1] in ("\"", "'") and text[-1:] == text[:1]:
        raise SigningKeyError("guillemets autour du PEM (la valeur doit commencer par -----BEGIN)")
    if "-----BEGIN" not in text:
        raise SigningKeyError("pas un PEM (aucune ligne -----BEGIN)")
    if "PUBLIC KEY-----" in text and "PRIVATE KEY-----" not in text:
        raise SigningKeyError("clé publique au lieu de la clé privée")
    if "\\n" in text and "\n" not in text:
        raise SigningKeyError("retours à la ligne échappés (\\n) au lieu de vrais retours à la ligne")
    if "-----END" not in text:
        raise SigningKeyError("PEM tronqué (aucune ligne -----END)")
    try:
        key = load_pem_private_key(text.encode(), password=None)
    except TypeError:
        raise SigningKeyError("clé chiffrée par un mot de passe (attendu : PEM non chiffré)") from None
    except (ValueError, UnsupportedAlgorithm):
        raise SigningKeyError("PEM illisible (tronqué, corrompu ou mal recopié)") from None
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        kind = type(key).__name__.replace("PrivateKey", "").lstrip("_") or "inconnu"
        raise SigningKeyError(f"mauvais type de clé ({kind} au lieu d'EC P-256, ES256)")
    if not isinstance(key.curve, ec.SECP256R1):
        raise SigningKeyError(f"mauvaise courbe ({key.curve.name} au lieu de P-256, ES256)")
    return key


@dataclass
class Signer:
    """Clé ES256 de nokker.ai (PEM, secret du déploiement) : signe les jetons et publie le JWKS. Une clé illisible ou
    qui n'est pas une clé privée EC P-256 lève `SigningKeyError` (jamais la clé dans le message)."""

    private_pem: bytes
    kid: str = "nokker-gw-1"
    issuer: str = ISSUER  # = base_url de nokker.ai (cloud_link) ; vérifié tel quel par le relais et le noyau local

    def __post_init__(self) -> None:
        self._key = _signing_key(self.private_pem)

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

    def sign(self, claims: dict[str, Any], typ: str = "JWT") -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        head = _b64u(json.dumps({"alg": "ES256", "typ": typ, "kid": self.kid}, separators=(",", ":")).encode())
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
                         now: float | None = None, handoff: str | None = None,
                         crew: dict[str, str] | None = None) -> str:
        """Jeton de délégation (§7.5) ; `handoff` nomme la demande de relais (X21) dont la session Lokkal rendra les
        résultats suivants (après une carte décidée au téléphone) par `POST /v1/gateway/handoffs/{id}/result`.
        `crew` (X15) : `{op: run|check|move, id}` — le jeton n'ouvre alors QUE la route de cette opération sur la
        machine (`nokker/local/crew.py::allows`), jamais une session."""
        t = int(now or time.time())
        claims = {"iss": self.issuer, "aud": AUDIENCE, "sub": account, "tenant": tenant, "machine": machine,
                  "scope": "lokkal:session", "web_session": web_session, "iat": t, "exp": t + ttl_s,
                  "jti": uuid.uuid4().hex}
        if handoff:
            claims["handoff"] = handoff
        if crew:
            claims["crew"] = {"op": str(crew.get("op") or ""), "id": str(crew.get("id") or "")}
        return self.sign(claims)


    def attestation_token(self, account: str, machine: str, approval_id: str, challenge_sha256: str, decision: str,
                          device_id: str, args_sha256: str | None = None, ttl_s: int = ATTEST_TTL_S,
                          now: float | None = None, jti: str | None = None) -> str:
        """Attestation de décision Telegram (lot X10, docs/LOKKAL.md §7.3) : nokker.ai a vérifié la preuve Telegram d'un
        appareil du compte (initData, clé de l'appareil) ; la MACHINE l'accepte à la place de la preuve elle-même.
        Liée à UNE machine (`aud`), UN compte (`sub`), UNE carte (`approval_id`) et UN défi (`challenge_sha256`, empreinte
        du texte exact déposé par la machine : nonce, outil et empreinte des arguments y sont), UNE décision. 120 s au
        plus, usage unique tenu par la machine (`jti`). Jamais un jeton d'accès : `typ` dédié, `aud` = la machine."""
        t = int(now or time.time())
        claims = {"iss": self.issuer, "aud": machine, "sub": account, "typ": ATTEST_TYP, "approval_id": approval_id,
                  "challenge_sha256": challenge_sha256, "decision": decision, "via": "telegram", "device_id": device_id,
                  "iat": t, "exp": t + max(1, min(int(ttl_s), ATTEST_TTL_S)), "jti": jti or uuid.uuid4().hex}
        if args_sha256:
            claims["args_sha256"] = args_sha256
        return self.sign(claims, typ=ATTEST_TYP)


class TokenError(ValueError):
    """Jeton signé refusé (forme, signature, émetteur, audience, expiration, machine)."""


def _verify_signed(token: str, jwks: dict[str, Any]) -> tuple[dict, dict]:
    """(en-tête, claims) d'un jeton ES256 dont la signature vérifie contre le JWKS ; rien d'autre n'est contrôlé."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    try:
        head_s, body_s, sig_s = token.split(".")
        head, claims, sig = json.loads(_b64u_dec(head_s)), json.loads(_b64u_dec(body_s)), _b64u_dec(sig_s)
    except (ValueError, json.JSONDecodeError) as e:
        raise TokenError("jeton illisible") from e
    if not isinstance(head, dict) or not isinstance(claims, dict):
        raise TokenError("jeton illisible")
    if head.get("alg") != "ES256" or len(sig) != 64:
        raise TokenError("algorithme refusé")
    key = next((k for k in (jwks or {}).get("keys", []) if k.get("kid") == head.get("kid")), None)
    if key is None:
        raise TokenError("clé inconnue")
    try:
        pub = ec.EllipticCurvePublicNumbers(int.from_bytes(_b64u_dec(key["x"]), "big"),
                                            int.from_bytes(_b64u_dec(key["y"]), "big"), ec.SECP256R1()).public_key()
        pub.verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")),
                   f"{head_s}.{body_s}".encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as e:
        raise TokenError("signature invalide") from e
    except (KeyError, ValueError) as e:
        raise TokenError("clé illisible") from e
    return head, claims


def verify_token(token: str, jwks: dict[str, Any], *, machine: str | None = None, now: float | None = None,
                 issuer: str = ISSUER) -> dict:
    """Vérifie un jeton ES256 de nokker.ai contre le JWKS (noyau local et relais). `machine` : si le jeton nomme une
    machine (délégation), elle doit être celle-ci."""
    _head, claims = _verify_signed(token, jwks)
    t = now or time.time()
    if claims.get("iss") != issuer or claims.get("aud") != AUDIENCE:
        raise TokenError("émetteur ou audience refusés")
    if not isinstance(claims.get("exp"), int) or t >= claims["exp"]:
        raise TokenError("jeton expiré")
    if "machine" in claims and machine is not None and claims["machine"] != machine:
        raise TokenError("jeton d'une autre machine")
    return claims


def challenge_sha256(challenge: str) -> str:
    """Empreinte du texte EXACT d'un défi (celui que la machine a déposé) : ce que l'attestation lie."""
    return _sha(challenge)


def verify_attestation(token: str, jwks: dict[str, Any], *, machine: str, account: str, approval_id: str,
                       challenge: str, issuer: str = ISSUER, now: float | None = None) -> dict:
    """Attestation de décision Telegram (`Signer.attestation_token`), vérifiée PAR LA MACHINE : signature par le JWKS
    épinglé au lien, `typ` (en-tête ET claim), émetteur, `aud` = cette machine, `sub` = le compte lié, la carte en
    attente, le défi qu'elle a déposé, décision connue, durée de vie bornée, non expirée. Rend les claims ; l'usage unique
    (`jti`) est tenu par l'appelant."""
    head, claims = _verify_signed(token, jwks)
    t = now or time.time()
    if head.get("typ") != ATTEST_TYP or claims.get("typ") != ATTEST_TYP:
        raise TokenError("ce jeton n'est pas une attestation de décision")
    if claims.get("iss") != issuer:
        raise TokenError("émetteur refusé")
    if claims.get("aud") != machine:
        raise TokenError("attestation d'une autre machine")
    if claims.get("sub") != account or not account:
        raise TokenError("attestation d'un autre compte")
    if claims.get("approval_id") != approval_id:
        raise TokenError("attestation d'une autre carte")
    if claims.get("challenge_sha256") != _sha(challenge):
        raise TokenError("attestation d'un autre défi")
    if claims.get("decision") not in ("approve", "reject"):
        raise TokenError("décision inconnue")
    if claims.get("via") != "telegram":
        raise TokenError("attestation d'une autre voie")
    exp, iat = claims.get("exp"), claims.get("iat")
    if not isinstance(exp, int) or not isinstance(iat, int) or exp - iat > ATTEST_TTL_S or exp <= iat:
        raise TokenError("durée de l'attestation invalide")
    if t >= exp:
        raise TokenError("attestation expirée")
    if iat > t + 30:
        raise TokenError("attestation datée du futur")
    if not isinstance(claims.get("jti"), str) or not claims["jti"] or not isinstance(claims.get("device_id"), str):
        raise TokenError("attestation incomplète")
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
    # Inventaire des ressources `local` par noms (lot X21, migration 025) : posé par `machines_with_load` seulement.
    resources: dict[str, Any] | None = None
    resources_at: float | None = None
    # Présence signalée par le relais (`POST /v1/gateway/presence`) : heure de la fermeture du tunnel, None = en ligne
    # ou inconnu. Une charge reçue APRÈS cette heure vaut retour en ligne (docs/LOKKAL.md §7.19).
    offline_at: float | None = None


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
        self.resources: dict[str, tuple[str, dict[str, Any], float]] = {}  # machine → (locataire, inventaire, reçu à)
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

    def by_host(self, host: str) -> Machine | None:
        with self.lock:
            return next((m for m in self.machines.values() if m.host == host and not m.revoked), None)

    def of_account(self, account: str) -> list[Machine]:
        with self.lock:
            return [m for m in self.machines.values() if m.account == account and not m.revoked]

    def tenant_has_machine(self, tenant: str) -> bool:
        """Au moins une machine liée (jeton relevé) et non révoquée pour ce locataire."""
        with self.lock:
            return any(m.tenant == tenant and not m.revoked and m.token_sha256 for m in self.machines.values())

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

    def set_resources(self, machine_id: str, tenant: str, resources: dict[str, Any], at: float) -> None:
        with self.lock:
            self.resources[machine_id] = (tenant, resources, at)

    def resources_of(self, tenant: str, machine_ids: list[str]) -> dict[str, tuple[dict[str, Any], float]]:
        """Inventaires des machines demandées, de CE locataire seulement (comme la RLS de la migration 025)."""
        with self.lock:
            return {mid: (v[1], v[2]) for mid in machine_ids if (v := self.resources.get(mid)) and v[0] == tenant}


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
    # Présence par machine, au mieux et en mémoire du processus (le relais la renvoie à chaque ouverture ou fermeture
    # de tunnel ; la fraîcheur de la charge X5 reste l'autorité) : id de machine -> heure de la fermeture.
    _offline: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    # Appelé avec le locataire quand ses machines liées changent (jeton relevé, révocation) : `serve` y branche
    # `TenantRegistry.forget`, pour que les outils Lokkal du compte (`lokkal_session`, `lokkal_handoff`) suivent.
    on_change: Any = None
    # Borne PAR CODE contre la devinette d'un `user_code` (8 car. parmi 28) : un code exposé à ce nombre de codes faux,
    # tous comptes et IP confondus, depuis sa création meurt (la machine relance `nokker link`). P(trouvé) ≤ 1000/28^8.
    # En mémoire du processus (un seul réplica) : après un redémarrage, un code en cours repart d'un budget plein.
    max_guesses_per_code: int = 1000
    _guesses: int = field(default=0, init=False, repr=False)  # codes faux saisis depuis le démarrage (approve, pending)
    _baseline: dict[str, tuple[int, float]] = field(default_factory=dict, init=False, repr=False)  # code → (_guesses, exp.)

    def _changed(self, tenant: str) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(tenant)
        except Exception:  # noqa: BLE001 — le lien est fait ; seul le jeu d'outils du compte attendra
            import logging

            logging.getLogger("nokker.cloud.link").warning("on_change(%s) en échec", tenant, exc_info=True)

    def has_machine(self, tenant: str) -> bool:
        """Le locataire a-t-il au moins une machine liée et non révoquée ? (outils Lokkal du compte)"""
        return bool(self.store.tenant_has_machine(tenant))

    def presence(self, machine: Machine, online: bool) -> None:
        """Tunnel ouvert (`online`) ou fermé sans remplaçant, dit par le relais."""
        with self._lock:
            if online:
                self._offline.pop(machine.id, None)
            else:
                self._offline[machine.id] = self.clock()

    def device(self, machine_name: str) -> dict[str, Any]:
        device_code = secrets.token_urlsafe(32)
        user_code = "-".join("".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(4)) for _ in range(2))
        expires_at = self.clock() + self.ttl_s
        self.store.add_request(_Request(device_sha=_sha(device_code), user_code=user_code,
                                        machine_name=machine_name[:60], expires_at=expires_at))
        with self._lock:
            now = self.clock()
            for code in [k for k, (_, exp) in self._baseline.items() if exp < now]:
                del self._baseline[code]
            self._baseline[user_code] = (self._guesses, expires_at)
        return {"device_code": device_code, "user_code": user_code, "verification_uri": f"{self.base_url}/link",
                "expires_in": self.ttl_s, "interval": 5}

    def _lookup(self, user_code: str) -> _Request | None:
        """La demande en attente pour ce code, sous `_lock`. Un code inconnu ou expiré compte comme un essai faux ; un
        code exposé à `max_guesses_per_code` essais faux depuis sa création est retiré (même réponse qu'inconnu)."""
        code = user_code.strip().upper()
        req = self.store.pending_by_code(code) if code else None
        if req is None or self.clock() > req.expires_at:
            self._guesses += 1
            return None
        start = self._baseline.setdefault(code, (self._guesses, req.expires_at))[0]
        if self._guesses - start >= self.max_guesses_per_code:
            self.store.drop_request(req.device_sha)
            self._baseline.pop(code, None)
            return None
        return req

    def approve(self, user_code: str, account: str, tenant: str, decision: str = "approve") -> dict[str, Any]:
        with self._lock:
            req = self._lookup(user_code)
            if req is None:
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
        with self._lock:
            req = self._lookup(user_code)
        if req is None:
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
        self._changed(m.tenant)
        return {"state": "approved", "api_key": token, "machine_token": token, "account": m.account,
                "machine_id": m.id, "relay_host": m.host, "inference_base": f"{self.base_url}/v1/inference"}

    def machine_for_token(self, token: str) -> Machine | None:
        return self.store.by_token(_sha(token or "")) if token else None

    def revoke(self, token: str) -> bool:
        m = self.machine_for_token(token)
        if m is None:
            return False
        self.store.revoke(m.id)
        self._changed(m.tenant)
        return True

    def machines_of(self, account: str) -> list[Machine]:
        return self.store.of_account(account)

    def machine_of(self, account: str, machine_id: str) -> Machine | None:
        """La machine non révoquée de CE compte, sinon None (sans dire si elle existe ailleurs)."""
        return next((m for m in self.store.of_account(account) if m.id == machine_id), None)

    def machine_by_host(self, host: str) -> Machine | None:
        """La machine NON révoquée qui porte ce nom de passerelle (`<nom>.nokker.app`), sinon None. Pour le relais
        seulement (pages servies depuis le miroir, machine éteinte) : l'appelant vérifie ensuite le locataire."""
        if not host:
            return None
        m = self.store.by_host(host.lower())
        return m if m is not None and not m.revoked else None

    def report_load(self, machine: Machine, raw: Any) -> dict[str, Any] | None:
        """Charge envoyée par la machine (`POST /v1/gateway/machine/load`), nettoyée puis gardée (une ligne courante
        par machine, l'heure est celle de nokker.ai). None si le corps n'est pas une charge."""
        from nokker.local.load import clean_load

        load = clean_load(raw)
        if load is None:
            return None
        self.store.set_load(machine.id, machine.tenant, load, self.clock())
        return load

    def report_resources(self, machine: Machine, raw: Any) -> dict[str, Any] | None:
        """Inventaire envoyé par la machine (`POST /v1/gateway/machine/resources`, lot X21) : noms seulement, nettoyés
        (un chemin, un contenu sont jetés), une ligne courante par machine. None si le corps n'est pas un inventaire."""
        from nokker.local.resources import clean_resources

        res = clean_resources(raw)
        if res is None:
            return None
        self.store.set_resources(machine.id, machine.tenant, res, self.clock())
        return res

    def machines_with_load(self, account: str, tenant: str) -> list[Machine]:
        """Les machines du compte (« mes machines »), chacune avec sa dernière charge et son inventaire connus."""
        machines = self.store.of_account(account)
        ids = [m.id for m in machines]
        loads = self.store.loads_of(tenant, ids) if machines else {}
        res_of = getattr(self.store, "resources_of", None)
        inventories = res_of(tenant, ids) if machines and res_of is not None else {}
        from dataclasses import replace

        out = []
        for m in machines:
            if m.id in loads:
                m = replace(m, load=loads[m.id][0], load_at=loads[m.id][1])
            if m.id in inventories:
                m = replace(m, resources=inventories[m.id][0], resources_at=inventories[m.id][1])
            if m.id in self._offline:
                m = replace(m, offline_at=self._offline[m.id])
            out.append(m)
        return out

    def revoke_machine(self, account: str, machine_id: str) -> bool:
        m = self.machine_of(account, machine_id)
        if m is None:
            return False
        self.store.revoke(m.id)
        self._changed(m.tenant)
        return True


__all__ = ["AUDIENCE", "ISSUER", "LinkService", "Machine", "MemoryLinkStore", "Signer", "SigningKeyError", "TokenError",
           "slug", "verify_attestation", "verify_token", "challenge_sha256", "ATTEST_TYP", "ATTEST_TTL_S"]
