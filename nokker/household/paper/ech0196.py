"""Relevé fiscal électronique (e-Steuerauszug, norme eCH-0196 v2, espace de noms `http://www.ech.ch/xmlns/eCH-0196/2`,
schéma eCH-0196-2-0.xsd et annexe « génération de codes-barres » v2.2.0 — vérifiés sur ech.ch le 01.11.2026,
docs/HOUSEHOLD-PAPER.md §5).

Le XML du relevé est dans le PDF sous forme de codes-barres PDF417 « Structured Append » compressés en ZLIB : le noyau
ne lit pas ces codes-barres (pas de décodeur d'images embarqué). Deux entrées sont donc acceptées :
- le XML eCH-0196 lui-même (exporté par l'e-banking, ou décodé par un client) : métadonnées exactes ;
- le PDF seul : reconnu par son texte (mots-clés), année lue dans le texte et marquée « à confirmer ».
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

NS = "http://www.ech.ch/xmlns/eCH-0196/2"
MAX_XML = 5 * 1024 * 1024

_KEYWORDS = re.compile(r"(?i)(steuerauszug|relev[ée] fiscal|e-?tax statement|eCH-0196|estratto fiscale)")
_PERIOD = re.compile(r"(?i)(?:steuerjahr|steuerperiode|ann[ée]e fiscale|p[ée]riode fiscale|tax year|"
                     r"steuerauszug|relev[ée] fiscal)\D{0,40}?((?:19|20)\d{2})")


class Ech0196Error(ValueError):
    pass


@dataclass
class TaxStatement:
    statement_id: str
    tax_period: int
    period_from: str
    period_to: str
    canton: str
    institution: str
    client_number: str  # gardé en base pour l'utilisateur ; jamais dans un journal ni dans le texte du modèle
    exact: bool = True  # False : reconnu par le texte d'un PDF, année à confirmer

    def summary(self) -> dict[str, Any]:
        return {"institution": self.institution, "tax_period": self.tax_period, "period": f"{self.period_from} → "
                f"{self.period_to}" if self.period_from else None, "canton": self.canton or None,
                "client": f"…{self.client_number[-4:]}" if self.client_number else None,
                "source": "XML eCH-0196" if self.exact else "texte du PDF (année à confirmer)"}

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> TaxStatement:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def parse_xml(data: bytes) -> TaxStatement:
    """XML eCH-0196 v2 → métadonnées. Analyseur sans entités, sans DTD, sans réseau."""
    from lxml import etree

    if len(data) > MAX_XML:
        raise Ech0196Error("relevé trop volumineux")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError:
        raise Ech0196Error("XML illisible") from None
    if root.tag != f"{{{NS}}}taxStatement":
        raise Ech0196Error("pas un relevé fiscal eCH-0196 v2 (élément taxStatement attendu)")
    inst = root.find(f"{{{NS}}}institution")
    client = root.find(f"{{{NS}}}client")
    try:
        period = int(str(root.get("taxPeriod") or ""))
    except ValueError:
        raise Ech0196Error("taxPeriod absente ou illisible") from None
    return TaxStatement(statement_id=str(root.get("id") or "")[:80], tax_period=period,
                        period_from=str(root.get("periodFrom") or ""), period_to=str(root.get("periodTo") or ""),
                        canton=str(root.get("canton") or "")[:2],
                        institution=str(inst.get("name") if inst is not None else "")[:120],
                        client_number=str(client.get("clientNumber") if client is not None else "")[:40])


def from_text(text: str) -> TaxStatement | None:
    """Un PDF dont le texte nomme un relevé fiscal : année lue après le mot-clé (sinon None), établissement inconnu."""
    if not _KEYWORDS.search(text or ""):
        return None
    m = _PERIOD.search(text)
    if not m:
        return None
    return TaxStatement(statement_id="", tax_period=int(m.group(1)), period_from="", period_to="", canton="",
                        institution="", client_number="", exact=False)


__all__ = ["NS", "Ech0196Error", "TaxStatement", "from_text", "parse_xml"]
