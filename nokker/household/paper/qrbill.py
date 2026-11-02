"""QR-facture suisse : décodage du contenu du Swiss QR Code (texte « SPC… »), selon les Implementation Guidelines de
SIX (IG QR-bill v2.3 du 21.11.2025, en vigueur ; v2.4 du 24.02.2026, en vigueur le 14.11.2026 — vérifiées sur
six-group.com le 01.11.2026, docs/HOUSEHOLD-PAPER.md §3).

Ce module ne lit pas l'IMAGE du code QR : il reçoit le texte déjà décodé (le téléphone ou le scanner du foyer le lit,
l'API d'import le reçoit à côté du fichier). Il vérifie tout ce que la spécification permet de vérifier, sans
réseau :

- en-tête `SPC` / `0200` / `1`, trailer `EPD`, nombre de lignes (31 à 34 ; une fin de ligne finale est tolérée) ;
- IBAN CH/LI de 21 caractères, clé modulo 97 ; QR-IBAN si l'IID (positions 5 à 9) est entre 30000 et 31999 ;
- type de référence : `QRR` (27 chiffres, modulo 10 récursif, QR-IBAN obligatoire, CHF seulement depuis la v2.4),
  `SCOR` (ISO 11649, `RF` + clé modulo 97), `NON` (pas de référence, IBAN ordinaire) ;
- montant (vide, ou 0.01 à 999 999 999.99, deux décimales), monnaie CHF ou EUR ;
- adresse du créancier : type `S` (structurée). Le type `K` (combiné) est retiré depuis la v2.3 : une facture `K` est
  encore lue, avec un avertissement ;
- informations de facturation `//S1/…` (syntaxe SIX, ex-Swico, annexe D) : n° de facture /10/, date /11/ (AAMMJJ),
  conditions /40/ — l'échéance est /11/ + n jours de la condition `0:n`. Sans ces deux balises, AUCUNE échéance
  n'est déduite (la QR-facture n'a pas de champ « échéance ») : elle reste à lire dans la lettre, et à confirmer.

Rien ici n'écrit de journal : l'IBAN et le montant n'en sortent que par `masked_iban` et les objets rendus.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_PAYLOAD = 997  # octets (version 25 du code QR, correction M ; IG §6)
QR_IID = range(30000, 32000)
_MOD10 = (0, 9, 4, 6, 8, 2, 7, 1, 3, 5)
_AMOUNT_RE = re.compile(r"^\d{1,9}\.\d{2}$")
_ALNUM = re.compile(r"^[A-Za-z0-9]+$")
_CHARSET = re.compile("^[ -~ -ſȘ-ț€]*$")  # IG §4.1.1


class QrBillError(ValueError):
    """Contenu illisible ou contraire à la spécification : le message dit quoi, sans recopier l'IBAN."""


@dataclass
class Address:
    kind: str  # S | K | ""
    name: str = ""
    street: str = ""  # S : rue ; K : ligne d'adresse 1
    number: str = ""  # S : n° ; K : ligne d'adresse 2 (NPA et localité)
    postcode: str = ""
    town: str = ""
    country: str = ""

    def empty(self) -> bool:
        return not any((self.kind, self.name, self.street, self.number, self.postcode, self.town, self.country))

    def one_line(self) -> str:
        if self.kind == "K":
            parts = [self.name, self.street, self.number]
        else:
            parts = [self.name, " ".join(p for p in (self.street, self.number) if p),
                     " ".join(p for p in (self.postcode, self.town) if p)]
        return ", ".join(p for p in parts if p) + (f" ({self.country})" if self.country else "")


@dataclass
class Billing:
    """`//S1/…` lu (balises connues) ; `raw` garde la ligne telle quelle."""

    raw: str = ""
    invoice_number: str = ""
    invoice_date: date | None = None
    customer_reference: str = ""
    vat_uid: str = ""
    conditions: list[tuple[Decimal, int]] = field(default_factory=list)  # (escompte %, jours)


@dataclass
class QrBill:
    iban: str
    qr_iban: bool
    creditor: Address
    amount: Decimal | None
    currency: str
    debtor: Address | None
    reference_type: str  # QRR | SCOR | NON
    reference: str
    message: str
    billing: Billing
    alternatives: list[str]
    warnings: list[str] = field(default_factory=list)

    @property
    def due_date(self) -> date | None:
        """Échéance déduite de /11/ + /40/0:n, seulement ; None sinon (jamais devinée)."""
        if self.billing.invoice_date is None:
            return None
        for pct, days in self.billing.conditions:
            if pct == 0:
                return self.billing.invoice_date + timedelta(days=days)
        return None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["amount"] = None if self.amount is None else f"{self.amount:.2f}"
        d["billing"]["invoice_date"] = self.billing.invoice_date.isoformat() if self.billing.invoice_date else None
        d["billing"]["conditions"] = [[f"{p}", n] for p, n in self.billing.conditions]
        d["due_date"] = self.due_date.isoformat() if self.due_date else None
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> QrBill:
        b = dict(d.get("billing") or {})
        billing = Billing(raw=str(b.get("raw") or ""), invoice_number=str(b.get("invoice_number") or ""),
                          invoice_date=date.fromisoformat(b["invoice_date"]) if b.get("invoice_date") else None,
                          customer_reference=str(b.get("customer_reference") or ""), vat_uid=str(b.get("vat_uid") or ""),
                          conditions=[(Decimal(str(p)), int(n)) for p, n in b.get("conditions") or []])
        debtor = d.get("debtor")
        return cls(iban=str(d["iban"]), qr_iban=bool(d.get("qr_iban")), creditor=Address(**d["creditor"]),
                   amount=Decimal(d["amount"]) if d.get("amount") else None, currency=str(d.get("currency") or ""),
                   debtor=Address(**debtor) if debtor else None, reference_type=str(d.get("reference_type") or ""),
                   reference=str(d.get("reference") or ""), message=str(d.get("message") or ""), billing=billing,
                   alternatives=list(d.get("alternatives") or []), warnings=list(d.get("warnings") or []))

    def summary(self) -> dict[str, Any]:
        """Ce que voit le modèle et la carte : IBAN masqué, montant, créancier, référence, échéance."""
        return {"creditor": self.creditor.one_line(), "iban": masked_iban(self.iban),
                "account_kind": "QR-IBAN" if self.qr_iban else "IBAN",
                "amount": None if self.amount is None else f"{self.amount:.2f}", "currency": self.currency,
                "reference_type": self.reference_type, "reference": self.reference, "message": self.message,
                "invoice_number": self.billing.invoice_number or None,
                "due_date": self.due_date.isoformat() if self.due_date else None,
                "due_source": "facture QR (date /11/ + délai /40/0:n)" if self.due_date else None,
                "warnings": list(self.warnings)}


# ─────────────────────────────── contrôles ───────────────────────────────


def iban_mod97_ok(iban: str) -> bool:
    s = iban[4:] + iban[:4]
    digits = "".join(str(int(c, 36)) for c in s)
    return int(digits) % 97 == 1


def masked_iban(iban: str) -> str:
    """`CH44 31•• •••• •••• •901 2` → affiché `CH44 …9012` : pays + clé + 4 derniers. Assez pour reconnaître, jamais
    assez pour payer ; c'est la seule forme qui entre dans un journal ou une carte."""
    s = re.sub(r"\s+", "", iban or "")
    return f"{s[:4]} …{s[-4:]}" if len(s) >= 8 else "…"


def qr_reference_ok(ref: str) -> bool:
    if not re.fullmatch(r"\d{27}", ref):
        return False
    carry = 0
    for ch in ref[:-1]:
        carry = _MOD10[(carry + int(ch)) % 10]
    return (10 - carry) % 10 == int(ref[-1])


def creditor_reference_ok(ref: str) -> bool:
    """ISO 11649 : `RF` + 2 chiffres de clé + 1 à 21 caractères alphanumériques ; modulo 97 = 1."""
    s = ref.replace(" ", "").upper()
    if not re.fullmatch(r"RF\d{2}[A-Z0-9]{1,21}", s):
        return False
    moved = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def _yymmdd(v: str) -> date | None:
    if not re.fullmatch(r"\d{6}", v):
        return None
    try:
        return date(2000 + int(v[:2]), int(v[2:4]), int(v[4:]))
    except ValueError:
        return None


def parse_billing(raw: str) -> Billing:
    """Syntaxe SIX v1.2 (annexe D des IG) : `//S1/10/…/11/…/40/0:30` ; `\\/` et `\\\\` échappés."""
    out = Billing(raw=raw)
    if not raw.startswith("//S1/"):
        return out
    body = raw[5:]
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            buf.append(body[i + 1])
            i += 2
            continue
        if ch == "/":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    tags = {parts[k]: parts[k + 1] for k in range(0, len(parts) - 1, 2)}
    out.invoice_number = tags.get("10", "")
    out.invoice_date = _yymmdd(tags.get("11", ""))
    out.customer_reference = tags.get("20", "")
    out.vat_uid = tags.get("30", "")
    for item in (tags.get("40") or "").split(";"):
        pct, _, days = item.partition(":")
        try:
            out.conditions.append((Decimal(pct), int(days)))
        except (InvalidOperation, ValueError):
            continue
    return out


def _address(lines: list[str], *, label: str, required: bool, warnings: list[str]) -> Address | None:
    kind, name, street, number, postcode, town, country = lines
    a = Address(kind=kind, name=name, street=street, number=number, postcode=postcode, town=town, country=country)
    if a.empty():
        if required:
            raise QrBillError(f"{label} : adresse manquante")
        return None
    if kind not in ("S", "K"):
        raise QrBillError(f"{label} : type d'adresse inconnu {kind[:3]!r} (S attendu)")
    if not name or len(name) > 70:
        raise QrBillError(f"{label} : nom manquant ou trop long (70 caractères au plus)")
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise QrBillError(f"{label} : pays ISO 3166-1 à deux lettres attendu")
    if kind == "K":
        warnings.append(f"{label} : adresse combinée (type K), retirée depuis la version 2.3 des IG (21.11.2025)")
        if postcode or town:
            raise QrBillError(f"{label} : NPA et localité doivent être vides avec une adresse de type K")
    elif not postcode or not town:
        raise QrBillError(f"{label} : NPA et localité obligatoires (adresse structurée)")
    return a


def parse(payload: str | bytes) -> QrBill:
    """Texte du Swiss QR Code → `QrBill`, ou `QrBillError` (message sans IBAN)."""
    if isinstance(payload, bytes):
        if len(payload) > MAX_PAYLOAD + 8:
            raise QrBillError("contenu trop long pour un Swiss QR Code")
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise QrBillError("contenu non UTF-8") from None
    text = payload.lstrip("﻿")
    if len(text.encode("utf-8")) > MAX_PAYLOAD + 8:
        raise QrBillError("contenu trop long pour un Swiss QR Code")
    sep = "\r\n" if "\r\n" in text else "\n"
    if sep == "\n" and "\r" in text:
        raise QrBillError("fins de ligne mélangées")
    lines = text.split(sep)
    if lines and lines[-1] == "" and len(lines) > 31:
        lines = lines[:-1]  # IG §4.1.4 : pas de fin de ligne après le dernier élément ; tolérée à la lecture
    if not lines or lines[0] != "SPC":
        raise QrBillError("pas une QR-facture suisse (en-tête SPC absent)")
    if not 31 <= len(lines) <= 34:
        raise QrBillError(f"{len(lines)} lignes : une QR-facture en a de 31 à 34")
    if lines[1] != "0200":
        raise QrBillError(f"version {lines[1][:8]!r} non prise en charge (0200 attendue)")
    if lines[2] != "1":
        raise QrBillError("codage inconnu (1 attendu : UTF-8, jeu latin)")
    if lines[30] != "EPD":
        raise QrBillError("trailer EPD absent à la ligne 31")
    for ln in lines:
        if not _CHARSET.match(ln):
            raise QrBillError("caractère hors du jeu autorisé (IG §4.1.1)")
    warnings: list[str] = []

    iban = lines[3].replace(" ", "").upper()
    if len(iban) != 21 or iban[:2] not in ("CH", "LI") or not _ALNUM.match(iban) or not iban_mod97_ok(iban):
        raise QrBillError("compte : IBAN CH ou LI de 21 caractères avec une clé valide attendu")
    qr_iban = int(iban[4:9]) in QR_IID if iban[4:9].isdigit() else False

    creditor = _address(lines[4:11], label="créancier", required=True, warnings=warnings)
    assert creditor is not None
    if any(lines[11:18]):
        warnings.append("créancier final rempli (champ réservé, doit rester vide)")

    amount: Decimal | None = None
    if lines[18]:
        if not _AMOUNT_RE.match(lines[18]):
            raise QrBillError("montant illisible (chiffres, point décimal, deux décimales)")
        amount = Decimal(lines[18])
        if not Decimal("0.01") <= amount <= Decimal("999999999.99"):
            raise QrBillError("montant hors bornes (0.01 à 999 999 999.99)")
    currency = lines[19]
    if currency not in ("CHF", "EUR"):
        raise QrBillError("monnaie : CHF ou EUR seulement")

    debtor = _address(lines[20:27], label="débiteur final", required=False, warnings=warnings)

    rtype, ref = lines[27], lines[28].replace(" ", "")
    if rtype == "QRR":
        if not qr_iban:
            raise QrBillError("référence QR sans QR-IBAN")
        if not qr_reference_ok(ref):
            raise QrBillError("référence QR invalide (27 chiffres, clé modulo 10 récursif)")
        if currency != "CHF":
            warnings.append("référence QR en EUR : réservée au CHF depuis la version 2.4 des IG (14.11.2026)")
    elif rtype == "SCOR":
        if qr_iban:
            raise QrBillError("QR-IBAN sans référence QR")
        if not creditor_reference_ok(ref):
            raise QrBillError("référence créancier ISO 11649 invalide")
        ref = ref.upper()
    elif rtype == "NON":
        if qr_iban:
            raise QrBillError("QR-IBAN sans référence QR")
        if ref:
            raise QrBillError("référence donnée avec le type NON")
    else:
        raise QrBillError("type de référence : QRR, SCOR ou NON")

    message = lines[29]
    billing_raw = lines[31] if len(lines) > 31 else ""
    if len(message) + len(billing_raw) > 140:
        raise QrBillError("communication et informations de facturation : 140 caractères au plus ensemble")
    alternatives = [a for a in lines[32:34] if a]
    if any(len(a) > 100 for a in alternatives):
        raise QrBillError("procédure alternative : 100 caractères au plus")
    billing = parse_billing(billing_raw)
    if billing_raw and not billing_raw.startswith("//"):
        warnings.append("informations de facturation dans une syntaxe inconnue (seule //S1/ est lue)")
    return QrBill(iban=iban, qr_iban=qr_iban, creditor=creditor, amount=amount, currency=currency, debtor=debtor,
                  reference_type=rtype, reference=ref, message=message, billing=billing, alternatives=alternatives,
                  warnings=warnings)


__all__ = ["Address", "Billing", "QrBill", "QrBillError", "creditor_reference_ok", "iban_mod97_ok", "masked_iban",
           "parse", "parse_billing", "qr_reference_ok"]
