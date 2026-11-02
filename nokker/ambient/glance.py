"""Vue « glance » des lunettes (docs/AMBIENT.md §6) : un jeton d'appareil de portée glance, en LECTURE SEULE.

- Même mécanisme que les écrans (`ambient_displays`, `kind = 'glance'`) : la base ne garde que l'empreinte du jeton
  (`nkg_…`), jamais le jeton. Le titulaire (le compte dont la vue lit les données) révoque ; un ADULTE de son foyer
  aussi (route des écrans du foyer).
- Appairage par code court ou QR : le téléphone connecté crée une OFFRE (`offer`) → code de 8 caractères (alphabet sans
  0/O/1/I/L/U), valable 5 minutes, à usage unique ; les lunettes le saisissent ou scannent le QR et le PRÉSENTENT
  (`claim`) → le jeton est émis une seule fois. Le code n'est gardé que par son empreinte.
- Portée tenue par le noyau : un jeton glance n'ouvre QUE `GET /v1/glance` (brief, dernier passage de l'agent Brief,
  cartes en attente, incidents en cours). Toute autre route, toute écriture, toute décision de carte : 403
  (`api/server.py::_glance_gate`).
- Échecs d'appairage bornés (20 par minute et par processus, puis 429) : 8 caractères sur 30 symboles et 5 minutes de
  vie rendent l'essai au hasard vain, la borne coupe court à une rafale.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.ambient.displays import Display, DisplayError, sha

GLANCE_PREFIX = "nkg_"
_GTOKEN = re.compile(r"^nkg_[A-Za-z0-9_-]{32,64}$")
_DISPLAY_ID = re.compile(r"^dp_[0-9a-f]{16}$")
ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # sans 0/O, 1/I/L, U
CODE_LEN = 8
OFFER_TTL_S = 300
MAX_GLANCE = 5  # vues actives ou en attente, par compte
MAX_FAILS_PER_MIN = 20
READS_PER_MIN = 2  # GET /v1/glance : par jeton (clé = id de l'appareil, jamais le jeton)
QR_PREFIX = "NOKKER-GLANCE:"


def is_glance_token(token: str) -> bool:
    return bool(token) and token.startswith(GLANCE_PREFIX)


def _norm(code: str) -> str:
    c = re.sub(r"[\s-]", "", (code or "").upper())
    if c.startswith(QR_PREFIX.replace("-", "").rstrip(":")):
        c = c.split(":", 1)[-1]
    return c


def code_sha(code: str) -> str:
    return sha("glance:" + _norm(code))


@dataclass
class Glance:
    store: Any  # DisplayStore (le même que les écrans)
    clock: Callable[[], float] = time.time
    _fails: deque = field(default_factory=deque, repr=False)
    _reads: dict[str, deque] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def offer(self, account_id: str, tenant_id: str, household_id: str = "", name: str = "") -> dict[str, Any]:
        """Offre d'appairage créée par le téléphone CONNECTÉ du titulaire."""
        live = [d for d in self.store.of_account(account_id, "glance") if d.revoked_at is None
                and (d.token_sha256 is not None or (d.setup_until or 0) > self.clock())]
        if len(live) >= MAX_GLANCE:
            raise DisplayError(409, "too_many_glance", f"{MAX_GLANCE} vues glance par compte au plus")
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
        now = self.clock()
        d = Display(id="dp_" + secrets.token_hex(8), tenant_id=tenant_id, household_id=household_id,
                    account_id=account_id, name=" ".join(str(name or "Lunettes").split())[:40] or "Lunettes",
                    model="glance", width=0, height=0, depth=1, refresh_s=60, token_sha256=None, created_at=now,
                    pair_sha256=code_sha(code), setup_until=now + OFFER_TTL_S, kind="glance")
        self.store.add(d)
        shown = f"{code[:4]}-{code[4:]}"
        return {"offer_id": d.id, "code": shown, "qr": QR_PREFIX + code, "expires_in": OFFER_TTL_S}

    def claim(self, code: str) -> dict[str, Any]:
        """Les lunettes présentent le code : jeton émis UNE fois. Code inconnu, expiré ou déjà pris : 404."""
        with self._lock:
            now = self.clock()
            while self._fails and self._fails[0] < now - 60:
                self._fails.popleft()
            if len(self._fails) >= MAX_FAILS_PER_MIN:
                raise DisplayError(429, "too_many_attempts", "trop d'essais : réessayez dans une minute")
        c = _norm(code)
        d = self.store.by_pairing_pending(code_sha(c), self.clock(), "glance") \
            if len(c) == CODE_LEN and all(ch in ALPHABET for ch in c) else None
        token = GLANCE_PREFIX + secrets.token_urlsafe(32)
        if d is None or not self.store.set_token(d.id, sha(token)):
            with self._lock:
                self._fails.append(self.clock())
            raise DisplayError(404, "unknown_code", "code inconnu ou expiré")
        return {"token": token, "device": {"id": d.id, "name": d.name}, "scope": "glance"}

    def resolve(self, token: str) -> Display | None:
        if not token or not _GTOKEN.match(token):
            return None
        d = self.store.by_token(sha(token))
        if d is None or d.kind != "glance" or not d.active:
            return None
        self.store.touch(d.id, self.clock())
        return d

    def read_allowed(self, device_id: str) -> int:
        """0 si la lecture passe (et elle est comptée) ; sinon le nombre de secondes à attendre (Retry-After).
        Fenêtre glissante de 60 s, `READS_PER_MIN` lectures par appareil, en mémoire du processus."""
        with self._lock:
            now = self.clock()
            q = self._reads.setdefault(device_id, deque())
            while q and q[0] <= now - 60:
                q.popleft()
            if len(q) >= READS_PER_MIN:
                return max(1, int(q[0] + 60 - now + 0.999))
            q.append(now)
            if len(self._reads) > 10_000:  # borne mémoire : on oublie les appareils muets
                for k in [k for k, v in self._reads.items() if not v or v[-1] <= now - 60]:
                    self._reads.pop(k, None)
            return 0

    def devices(self, account_id: str) -> dict[str, Any]:
        return {"devices": [d.to_json() for d in self.store.of_account(account_id, "glance")]}

    def revoke(self, account_id: str, device_id: str) -> dict[str, Any]:
        if not _DISPLAY_ID.match(device_id or "") or not self.store.revoke_owned(account_id, device_id, self.clock()):
            raise DisplayError(404, "not_found", "appareil inconnu")
        return {"revoked": True}


__all__ = ["GLANCE_PREFIX", "Glance", "OFFER_TTL_S", "QR_PREFIX", "code_sha", "is_glance_token"]
