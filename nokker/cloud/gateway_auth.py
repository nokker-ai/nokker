"""Connexion à la passerelle (`<nom>.nokker.app`, docs/LOKKAL.md §7.3), côté nokker.ai : code d'autorisation + PKCE,
le relais en intermédiaire (il garde le jeton de renouvellement dans un cookie `__Host-nk_rt` de SA réponse : le
relais retire tout `Set-Cookie` venu de la machine, docs/GATEWAY.md §3).

1. `authorize(compte, host, challenge)` (page app.nokker.ai/gateway/authorize, session du compte) → code de 60 s,
   usage unique, lié à (compte, machine, challenge) ; retour TOUJOURS sur `https://<host>/auth/callback` (jamais une
   adresse fournie par le client).
2. `token(code, verifier, host)` (appelé par le relais) → `{access_token, expires_in, refresh_token}` ; jeton d'accès
   §7.5 lié à la machine (`machine`), 15 min.
3. `refresh(refresh_token, host)` → nouveau couple, ROTATION : l'ancien ne sert plus ; s'il resservait (vol), toute
   sa famille est révoquée. Une machine déliée invalide ses jetons (la machine est relue à chaque renouvellement).
4. `revoke(refresh_token)` → la famille entière (déconnexion).
Jamais de jeton en clair au magasin : des empreintes sha256.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

CODE_TTL_S = 60
ACCESS_TTL_S = 900
REFRESH_TTL_S = 30 * 86400


class GrantError(ValueError):
    """Code, vérificateur ou jeton de renouvellement refusé : `invalid_grant` (400), sans dire pourquoi."""


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@dataclass
class _Code:
    account: str
    tenant: str
    machine_id: str
    host: str
    challenge: str
    expires_at: float


@dataclass
class _Refresh:
    family: str
    account: str
    tenant: str
    machine_id: str
    host: str
    expires_at: float
    used: bool = False
    revoked: bool = False


class MemoryGatewayAuthStore:
    def __init__(self) -> None:
        self.codes: dict[str, _Code] = {}
        self.refresh: dict[str, _Refresh] = {}
        self.lock = threading.Lock()

    def put_code(self, sha: str, c: _Code) -> None:
        with self.lock:
            self.codes[sha] = c

    def take_code(self, sha: str) -> _Code | None:
        with self.lock:
            return self.codes.pop(sha, None)

    def put_refresh(self, sha: str, r: _Refresh) -> None:
        with self.lock:
            self.refresh[sha] = r

    def use_refresh(self, sha: str) -> _Refresh | None:
        """Le jeton tel qu'il était, marqué utilisé (une seule fois : un second appel le voit `used`)."""
        with self.lock:
            r = self.refresh.get(sha)
            if r is None:
                return None
            before = _Refresh(**r.__dict__)
            r.used = True
            return before

    def revoke_family(self, family: str) -> None:
        with self.lock:
            for r in self.refresh.values():
                if r.family == family:
                    r.revoked = True

    def family_of(self, sha: str) -> str | None:
        with self.lock:
            r = self.refresh.get(sha)
            return r.family if r else None


@dataclass
class GatewayAuth:
    links: Any  # LinkService
    signer: Any  # Signer
    store: Any = field(default_factory=MemoryGatewayAuthStore)
    clock: Any = time.time

    def _machine(self, account: str, host: str) -> Any:
        return next((m for m in self.links.machines_of(account) if m.host == host), None)

    def authorize(self, account: str, tenant: str, host: str, challenge: str) -> dict[str, Any]:
        m = self._machine(account, host)
        if m is None:
            raise PermissionError("cette machine n'est pas liée à ce compte")
        if not challenge or len(challenge) < 43:
            raise GrantError("code_challenge S256 requis")
        code = secrets.token_urlsafe(32)
        self.store.put_code(_sha(code), _Code(account, tenant, m.id, host, challenge, self.clock() + CODE_TTL_S))
        return {"code": code, "redirect": f"https://{host}/auth/callback", "expires_in": CODE_TTL_S}

    def _pair(self, account: str, tenant: str, machine_id: str, host: str, family: str) -> dict[str, Any]:
        refresh = secrets.token_urlsafe(32)
        self.store.put_refresh(_sha(refresh), _Refresh(family, account, tenant, machine_id, host,
                                                       self.clock() + REFRESH_TTL_S))
        return {"access_token": self.signer.access_token(account, tenant, machine=machine_id),
                "expires_in": ACCESS_TTL_S, "refresh_token": refresh}

    def token(self, code: str, verifier: str, host: str) -> dict[str, Any]:
        c = self.store.take_code(_sha(code or ""))  # pris = consommé, même si la suite échoue
        if (c is None or self.clock() > c.expires_at or c.host != host
                or not hmac.compare_digest(s256(verifier or ""), c.challenge)):
            raise GrantError("invalid_grant")
        m = self._machine(c.account, host)
        if m is None or m.id != c.machine_id:
            raise GrantError("invalid_grant")
        return self._pair(c.account, c.tenant, m.id, host, family=secrets.token_hex(8))

    def refresh(self, refresh_token: str, host: str) -> dict[str, Any]:
        sha = _sha(refresh_token or "")
        r = self.store.use_refresh(sha)
        if r is None:
            raise GrantError("invalid_grant")
        if r.used or r.revoked:
            if r.used and not r.revoked:  # un jeton déjà échangé qui resert : vol probable, toute la famille tombe
                self.store.revoke_family(r.family)
            raise GrantError("invalid_grant")
        m = self._machine(r.account, host)
        if self.clock() > r.expires_at or r.host != host or m is None or m.id != r.machine_id:
            raise GrantError("invalid_grant")
        return self._pair(r.account, r.tenant, m.id, host, family=r.family)

    def revoke(self, refresh_token: str) -> None:
        family = self.store.family_of(_sha(refresh_token or ""))
        if family:
            self.store.revoke_family(family)


__all__ = ["ACCESS_TTL_S", "CODE_TTL_S", "GatewayAuth", "GrantError", "MemoryGatewayAuthStore", "REFRESH_TTL_S",
           "s256"]
