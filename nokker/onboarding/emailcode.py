"""Connexion par code e-mail (écran sans mot de passe, doc interne nokker/scope-v1-2026-10.md §16).

    POST /v1/auth/email/start  {email, lang}                          → 200 {sent: true}, TOUJOURS
    POST /v1/auth/email/verify {email, code, name, public_key_spki}   → 200 {device_id, user} | 401 invalid_code

Ce que voit l'appelant ne dépend jamais de l'existence du compte : même corps, même code HTTP, même délai (l'envoi
part dans un fil à part), même 429. Le code (6 chiffres) n'est émis que pour un compte actif ; il vit 10 minutes, sert
une fois, et 5 essais faux le brûlent. front1 l'envoie avec un gabarit figé, seulement à une adresse invitée.

Un appareil né d'un code se connecte et discute, mais ne décide JAMAIS d'une approbation (`approved_via`
« email-code:… », refusé par `BaseApprovals._verify_proof`) : l'approbation forte reste à l'appareil de l'invitation
et à Telegram (décision de Nick, 21.10.2026).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from nokker.approvals.base import BaseApprovals
from nokker.approvals.crypto import load_p256_public_key_spki
from nokker.onboarding.front import Front
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.store import Tenancy

logger = logging.getLogger("nokker.onboarding.emailcode")

TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
# Limites (fenêtre glissante) : par adresse pour les envois, par IP pour tout. Un seul pod noyau en bêta : en mémoire.
START_PER_EMAIL, START_PER_IP, VERIFY_PER_IP, WINDOW_S = 3, 10, 30, 900.0


class CodeError(Exception):
    """`invalid_code` (401, uniforme), `invalid_key` (400), `rate` (429)."""

    STATUS = {"invalid_code": 401, "invalid_key": 400, "rate": 429}

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

    @property
    def status(self) -> int:
        return self.STATUS.get(self.code, 400)


def _sha(account_id: str, code: str) -> str:
    return hashlib.sha256(f"{account_id}:{code}".encode()).hexdigest()


def _norm(email: str) -> str:
    return (email or "").strip().lower()


class CodeStore(Protocol):
    def put(self, account_id: str, code_sha: str, expires_at: datetime) -> None: ...
    def attempt(self, account_id: str) -> tuple[str, datetime, int] | None: ...
    def burn(self, account_id: str) -> None: ...


@dataclass
class MemoryCodes:
    _rows: dict[str, list[Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def put(self, account_id: str, code_sha: str, expires_at: datetime) -> None:
        with self._lock:
            self._rows[account_id] = [code_sha, expires_at, 0]

    def attempt(self, account_id: str) -> tuple[str, datetime, int] | None:
        with self._lock:
            row = self._rows.get(account_id)
            if row is None:
                return None
            row[2] += 1
            return row[0], row[1], row[2]

    def burn(self, account_id: str) -> None:
        with self._lock:
            self._rows.pop(account_id, None)


class PgCodes:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):
        import psycopg
        return psycopg.connect(self.dsn)

    def put(self, account_id: str, code_sha: str, expires_at: datetime) -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO auth_email_codes (account_id, code_sha256, expires_at) VALUES (%s,%s,%s) "
                         "ON CONFLICT (account_id) DO UPDATE SET code_sha256 = EXCLUDED.code_sha256, "
                         "expires_at = EXCLUDED.expires_at, attempts = 0, created_at = now()",
                         (account_id, code_sha, expires_at))

    def attempt(self, account_id: str) -> tuple[str, datetime, int] | None:
        # Compté AVANT la comparaison, atomiquement : deux essais simultanés ne passent pas sous la limite.
        with self._conn() as conn:
            row = conn.execute("UPDATE auth_email_codes SET attempts = attempts + 1 WHERE account_id = %s "
                               "RETURNING code_sha256, expires_at, attempts", (account_id,)).fetchone()
        return (row[0], row[1], row[2]) if row else None

    def burn(self, account_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM auth_email_codes WHERE account_id = %s", (account_id,))


class _Limiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def ok(self, key: str, limit: int, now: float | None = None) -> bool:
        t = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits[key]
            while q and t - q[0] > WINDOW_S:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(t)
            return True


def _send_in_background(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="nokker-email-code", daemon=True).start()


@dataclass
class EmailCodes:
    front: Front
    tenancy: Tenancy
    approvals: BaseApprovals
    store: CodeStore
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    dispatch: Callable[[Callable[[], None]], None] = _send_in_background  # synchrone dans les tests
    _limits: _Limiter = field(default_factory=_Limiter, repr=False)

    def start(self, email: str, lang: str, ip: str | None) -> dict[str, bool]:
        email = _norm(email)
        # Les deux limites sont consommées dans tous les cas : un 429 ne dit rien du compte.
        by_email = self._limits.ok(f"start:{email}", START_PER_EMAIL)
        by_ip = self._limits.ok(f"start-ip:{ip or '?'}", START_PER_IP)
        if not (by_email and by_ip):
            raise CodeError("rate")
        account = self.tenancy.account_by_email(email) if "@" in email else None
        if account is not None and account.status == "active":
            code = f"{secrets.randbelow(10**6):06d}"
            lang = "en" if lang == "en" else "fr"

            def send() -> None:  # écriture ET envoi hors de la requête : même délai que pour une adresse inconnue
                try:
                    self.store.put(account.account_id, _sha(account.account_id, code), self.now() + TTL)
                    self.front.send_code(email, code, lang)
                except Exception:
                    logger.exception("envoi du code de connexion en échec (compte %s)", account.account_id)

            self.dispatch(send)
        return {"sent": True}

    def verify(self, email: str, code: str, name: str, public_key_spki: str, ip: str | None) -> dict[str, str]:
        if not self._limits.ok(f"verify-ip:{ip or '?'}", VERIFY_PER_IP):
            raise CodeError("rate")
        try:
            load_p256_public_key_spki(public_key_spki)
        except Exception:  # noqa: BLE001 — refus avant tout essai : ne consomme pas le code
            raise CodeError("invalid_key") from None
        account = self.tenancy.account_by_email(_norm(email))
        if account is None or account.status != "active":
            raise CodeError("invalid_code")
        row = self.store.attempt(account.account_id)
        if row is None:
            raise CodeError("invalid_code")
        code_sha, expires_at, attempts = row
        good = hmac.compare_digest(code_sha, _sha(account.account_id, (code or "").strip()))
        if attempts > MAX_ATTEMPTS or expires_at < self.now() or not good:
            if attempts >= MAX_ATTEMPTS or expires_at < self.now():
                self.store.burn(account.account_id)
            raise CodeError("invalid_code")
        self.store.burn(account.account_id)  # usage unique, avant de créer quoi que ce soit
        with tenant_scope(account.tenant_id):
            device = self.approvals.enroll_device(account.account_id, (name or "Mon appareil")[:80], public_key_spki)
            try:
                self.approvals.confirm_device_by_email_code(device.id)
                self.tenancy.bind_identity(account.account_id, "device", device.id)
            except Exception:
                self.approvals.delete_device(device.id)
                raise
        return {"device_id": device.id, "user": account.account_id}


__all__ = ["MAX_ATTEMPTS", "TTL", "CodeError", "EmailCodes", "MemoryCodes", "PgCodes"]
