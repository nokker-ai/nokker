"""Émetteur `own_card` (Lokkal seulement, docs/PAY-AGENT.md §11) : la personne paie avec SA carte, par son agent de
paiement. Aucune carte n'est émise : la carte vit dans le coffre local de la machine (OpenBao du compose, profil
`vault`, ou le coffre fichier chiffré), écrite et lue par l'agent de paiement seul.

- `CardVault` : numéro, expiration, titulaire (et le CVV si la personne a choisi de le garder) dans le coffre, sous des
  noms tirés de l'empreinte du compte ; rend seulement la marque, les 4 derniers chiffres et l'expiration.
- `Ephemeral` : CVV demandé à chaque paiement et code de la banque (3-D Secure par SMS), en mémoire, liés à UN mandat,
  avec une échéance ; lus une fois puis effacés ; jamais écrits, jamais journalisés.

Le plafond réel reste celui de la carte (conseil affiché : carte virtuelle à plafond bas) plus le mandat signé.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

PAN_RE = re.compile(r"^\d{12,19}$")
CVV_RE = re.compile(r"^\d{3,4}$")
OTP_RE = re.compile(r"^[0-9A-Za-z]{4,10}$")


class CardError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def luhn_ok(pan: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(pan)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def brand_of(pan: str) -> str:
    """Réseau d'après les premiers chiffres (IIN), pour l'affichage seulement."""
    if pan.startswith("4"):
        return "visa"
    if re.match(r"^(5[1-5]|2(2[2-9][1-9]|2[3-9]\d|[3-6]\d\d|7[01]\d|720))", pan):
        return "mastercard"
    if re.match(r"^3[47]", pan):
        return "amex"
    if re.match(r"^(50|5[6-9]|6\d)", pan):
        return "maestro" if not pan.startswith(("6011", "65")) else "discover"
    return "card"


def clean_pan(raw: Any) -> str:
    pan = re.sub(r"[\s\-]", "", str(raw or ""))
    if not PAN_RE.match(pan) or not luhn_ok(pan):
        raise CardError("number", "numéro de carte illisible")
    return pan


def clean_expiry(month: Any, year: Any, now: float) -> tuple[int, int]:
    try:
        m, y = int(month), int(year)
    except (TypeError, ValueError):
        raise CardError("expiry", "date d'expiration illisible") from None
    if y < 100:
        y += 2000
    t = time.gmtime(now)
    if not 1 <= m <= 12 or (y, m) < (t.tm_year, t.tm_mon) or y > t.tm_year + 25:
        raise CardError("expiry", "date d'expiration passée ou illisible")
    return m, y


def clean_cvv(raw: Any) -> str:
    cvv = str(raw or "").strip()
    if not CVV_RE.match(cvv):
        raise CardError("cvv", "code de sécurité illisible")
    return cvv


@dataclass(frozen=True)
class CardMeta:
    brand: str
    last4: str
    exp_month: int
    exp_year: int
    cvv_kept: bool


class CardVault:
    """La carte d'un compte dans le coffre local (`SecretsProvider` : `openbao` ou `file`), portée `system`."""

    def __init__(self, provider: Any, clock: Callable[[], float] = time.time) -> None:
        self.provider, self.clock = provider, clock

    @staticmethod
    def _base(account: str) -> str:
        return "payagent.card." + hashlib.sha256(account.encode()).hexdigest()[:24]

    def put(self, account: str, *, number: Any, exp_month: Any, exp_year: Any, holder: Any,
            cvv: Any = None) -> CardMeta:
        pan = clean_pan(number)
        m, y = clean_expiry(exp_month, exp_year, self.clock())
        name = str(holder or "").strip()
        if not 1 <= len(name) <= 100 or not name.isprintable():
            raise CardError("holder", "nom du titulaire illisible")
        b = self._base(account)
        self.provider.set(b + ".pan", pan)
        self.provider.set(b + ".exp", f"{m:02d}/{y}")
        self.provider.set(b + ".holder", name)
        if cvv is not None and str(cvv).strip():
            self.provider.set(b + ".cvv", clean_cvv(cvv))
        else:
            self.provider.delete(b + ".cvv")
        return CardMeta(brand=brand_of(pan), last4=pan[-4:], exp_month=m, exp_year=y,
                        cvv_kept=cvv is not None and bool(str(cvv).strip()))

    def set_cvv(self, account: str, cvv: Any | None) -> bool:
        """Mode du CVV : gardé dans le coffre (`cvv` donné) ou demandé à chaque paiement (None : effacé)."""
        b = self._base(account)
        if self.provider.get(b + ".pan") is None:
            raise CardError("no_card", "aucune carte enregistrée")
        if cvv is None:
            self.provider.delete(b + ".cvv")
            return False
        self.provider.set(b + ".cvv", clean_cvv(cvv))
        return True

    def fields(self, account: str) -> dict[str, str] | None:
        """Ce que l'agent tape au checkout (lu au moment de payer, jamais rendu par une route)."""
        b = self._base(account)
        pan = self.provider.get(b + ".pan")
        if pan is None:
            return None
        return {"number": pan, "expiry": self.provider.get(b + ".exp") or "",
                "holder": self.provider.get(b + ".holder") or "", "cvv": self.provider.get(b + ".cvv") or ""}

    def forget(self, account: str) -> bool:
        b = self._base(account)
        gone = False
        for k in (".pan", ".exp", ".holder", ".cvv"):
            gone = bool(self.provider.delete(b + k)) or gone
        return gone


class Ephemeral:
    """CVV et codes de banque d'un paiement : en mémoire, liés à (compte, mandat, sorte), échéance, lus une fois."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._v: dict[tuple[str, str, str], tuple[str, float]] = {}
        self._lock = threading.Lock()
        self.clock = clock

    def put(self, account: str, mandate_id: str, kind: str, value: str, ttl_s: float) -> None:
        with self._lock:
            self._v[(account, mandate_id, kind)] = (value, self.clock() + ttl_s)

    def take(self, account: str, mandate_id: str, kind: str) -> str | None:
        with self._lock:
            got = self._v.pop((account, mandate_id, kind), None)
        if got is None or got[1] < self.clock():
            return None
        return got[0]

    def wipe(self, account: str, mandate_id: str) -> None:
        with self._lock:
            for k in [k for k in self._v if k[0] == account and k[1] == mandate_id]:
                self._v.pop(k, None)


__all__ = ["CVV_RE", "CardError", "CardMeta", "CardVault", "Ephemeral", "OTP_RE", "brand_of", "clean_cvv",
           "clean_expiry", "clean_pan", "luhn_ok"]
