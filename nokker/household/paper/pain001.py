"""Fichier de paiement `pain.001` PRÉPARÉ pour l'e-banking de l'utilisateur (Swiss Payment Standards, IG Customer
Credit Transfer SPS 2026 v2.3 de SIX, schéma `pain.001.001.09.ch.03.xsd` — vérifiés sur six-group.com le 01.11.2026,
docs/HOUSEHOLD-PAPER.md §4).

Le noyau ne transmet JAMAIS ce fichier à une banque : il le construit, le valide au schéma, et l'utilisateur le
télécharge puis le charge lui-même dans son e-banking, qui lui montre chaque paiement avant sa propre signature.

Correspondance QR-facture → pain.001 (annexe B des IG SPS) :
- IBAN du QR → `CdtrAcct/Id/IBAN` ; nom et adresse structurée → `Cdtr/Nm`, `Cdtr/PstlAdr` (StrtNm, BldgNb, PstCd,
  TwnNm, Ctry) ; adresse K (retirée) → deux `AdrLine` + `Ctry` ;
- montant et monnaie → `Amt/InstdAmt[@Ccy]` ;
- `QRR` → `RmtInf/Strd/CdtrRefInf/Tp/CdOrPrtry/Prtry = QRR` + `Ref` ; `SCOR` → `Cd = SCOR`, `Issr = ISO` + `Ref` ;
  communication avec une référence → `Strd/AddtlRmtInf` ; `NON` → `RmtInf/Ustrd`.
- compte débité : `DbtrAcct/Id/IBAN` ; établissement du débiteur : BIC s'il est donné, sinon l'IID de l'IBAN suisse
  (`ClrSysMmbId`, `CHBCC`).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from nokker.household.paper.qrbill import QrBill, iban_mod97_ok

NS = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"
XSD_PATH = Path(__file__).resolve().parent / "schemas" / "pain.001.001.09.ch.03.xsd"


class Pain001Error(ValueError):
    pass


@dataclass(frozen=True)
class Debtor:
    name: str
    iban: str
    bic: str = ""

    def check(self) -> None:
        iban = self.iban.replace(" ", "").upper()
        if not self.name.strip() or len(self.name) > 70:
            raise Pain001Error("compte débité : nom manquant ou trop long")
        if not (15 <= len(iban) <= 34) or not iban[:2].isalpha() or not iban_mod97_ok(iban):
            raise Pain001Error("compte débité : IBAN invalide")
        if self.bic and not (len(self.bic) in (8, 11) and self.bic.isalnum()):
            raise Pain001Error("compte débité : BIC invalide (8 ou 11 caractères)")


@dataclass(frozen=True)
class Payment:
    bill: QrBill
    amount: Decimal  # montant du QR, ou saisi par l'utilisateur pour une facture sans montant
    end_to_end: str


def _t(tag: str, value: str) -> str:
    return f"<{tag}>{escape(value)}</{tag}>"


def _addr(bill: QrBill) -> str:
    c = bill.creditor
    if c.kind == "K":
        inner = _t("Ctry", c.country) + "".join(_t("AdrLine", x[:70]) for x in (c.street, c.number) if x)
    else:
        inner = "".join(_t(tag, v) for tag, v in (("StrtNm", c.street), ("BldgNb", c.number), ("PstCd", c.postcode),
                                                  ("TwnNm", c.town), ("Ctry", c.country)) if v)
    return f"<PstlAdr>{inner}</PstlAdr>"


def _remittance(bill: QrBill) -> str:
    if bill.reference_type == "QRR":
        tp = "<Tp><CdOrPrtry><Prtry>QRR</Prtry></CdOrPrtry></Tp>"
    elif bill.reference_type == "SCOR":
        tp = "<Tp><CdOrPrtry><Cd>SCOR</Cd></CdOrPrtry><Issr>ISO</Issr></Tp>"
    else:
        return f"<RmtInf>{_t('Ustrd', bill.message[:140])}</RmtInf>" if bill.message else ""
    extra = _t("AddtlRmtInf", bill.message[:140]) if bill.message else ""
    return f"<RmtInf><Strd><CdtrRefInf>{tp}{_t('Ref', bill.reference)}</CdtrRefInf>{extra}</Strd></RmtInf>"


def _agent(debtor: Debtor) -> str:
    if debtor.bic:
        return f"<DbtrAgt><FinInstnId>{_t('BICFI', debtor.bic.upper())}</FinInstnId></DbtrAgt>"
    iban = debtor.iban.replace(" ", "").upper()
    if iban[:2] in ("CH", "LI") and iban[4:9].isdigit():
        return ("<DbtrAgt><FinInstnId><ClrSysMmbId><ClrSysId><Cd>CHBCC</Cd></ClrSysId>"
                f"{_t('MmbId', iban[4:9])}</ClrSysMmbId></FinInstnId></DbtrAgt>")
    raise Pain001Error("compte débité hors de Suisse : le BIC de la banque est nécessaire")


def build(debtor: Debtor, payments: list[Payment], *, execution: date, msg_id: str, created: datetime) -> str:
    """XML pain.001.001.09 (SPS) : un groupe de paiements (B-level) par monnaie. `Pain001Error` si une donnée manque."""
    debtor.check()
    if not payments:
        raise Pain001Error("aucun paiement")
    if len(msg_id) > 35:
        raise Pain001Error("identifiant de message trop long")
    by_ccy: dict[str, list[Payment]] = {}
    for p in payments:
        if p.amount <= 0 or p.amount != p.amount.quantize(Decimal("0.01")):
            raise Pain001Error("montant invalide")
        by_ccy.setdefault(p.bill.currency, []).append(p)
    total = sum((p.amount for p in payments), Decimal(0))
    debtor_iban = debtor.iban.replace(" ", "").upper()
    blocks = []
    for n, (ccy, items) in enumerate(sorted(by_ccy.items()), start=1):
        txs = []
        for k, p in enumerate(items, start=1):
            b = p.bill
            txs.append(
                "<CdtTrfTxInf>"
                f"<PmtId>{_t('InstrId', f'{msg_id[:28]}-{n}-{k}')}{_t('EndToEndId', p.end_to_end[:35])}</PmtId>"
                f"<Amt><InstdAmt Ccy=\"{ccy}\">{p.amount:.2f}</InstdAmt></Amt>"
                f"<Cdtr>{_t('Nm', b.creditor.name[:70])}{_addr(b)}</Cdtr>"
                f"<CdtrAcct><Id>{_t('IBAN', b.iban)}</Id></CdtrAcct>"
                f"{_remittance(b)}"
                "</CdtTrfTxInf>")
        subtotal = sum((p.amount for p in items), Decimal(0))
        blocks.append(
            "<PmtInf>"
            f"{_t('PmtInfId', f'{msg_id[:30]}-{n}')}<PmtMtd>TRF</PmtMtd><BtchBookg>false</BtchBookg>"
            f"{_t('NbOfTxs', str(len(items)))}{_t('CtrlSum', f'{subtotal:.2f}')}"
            f"<ReqdExctnDt>{_t('Dt', execution.isoformat())}</ReqdExctnDt>"
            f"<Dbtr>{_t('Nm', debtor.name[:70])}</Dbtr>"
            f"<DbtrAcct><Id>{_t('IBAN', debtor_iban)}</Id></DbtrAcct>"
            f"{_agent(debtor)}"
            + "".join(txs) +
            "</PmtInf>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Document xmlns="{NS}"><CstmrCdtTrfInitn><GrpHdr>'
            f"{_t('MsgId', msg_id)}{_t('CreDtTm', created.strftime('%Y-%m-%dT%H:%M:%S'))}"
            f"{_t('NbOfTxs', str(len(payments)))}{_t('CtrlSum', f'{total:.2f}')}"
            f"<InitgPty>{_t('Nm', debtor.name[:70])}</InitgPty>"
            "</GrpHdr>" + "".join(blocks) + "</CstmrCdtTrfInitn></Document>\n")


@cache
def _schema(path: str) -> Any:
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    return etree.XMLSchema(etree.parse(path, parser))


def schema_available() -> bool:
    """Le schéma SPS de SIX n'est pas dans l'export public (licence à clarifier) : sans lui, la structure reste
    construite par `build`, mais le fichier n'est pas validé au schéma, et la carte le dit."""
    return XSD_PATH.is_file()


def validate(xml: str) -> list[str]:
    """Erreurs du schéma SPS (liste vide = valide). Schéma absent de l'installation : seule la lecture du XML est
    vérifiée (voir `schema_available`)."""
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        doc = etree.fromstring(xml.encode("utf-8"), parser)
    except etree.XMLSyntaxError as e:
        return [f"XML illisible : {e}"]
    if not schema_available():
        return []
    schema = _schema(str(XSD_PATH))
    if schema.validate(doc):
        return []
    return [f"ligne {e.line} : {e.message}" for e in schema.error_log][:20]


def sha256(xml: str) -> str:
    return hashlib.sha256(xml.encode("utf-8")).hexdigest()


__all__ = ["NS", "Debtor", "Pain001Error", "Payment", "build", "schema_available", "sha256", "validate"]
