"""Papiers du foyer (docs/HOUSEHOLD-PAPER.md) : la boîte aux lettres qui se traite seule, sans jamais payer.

Entrées : fichier importé par le compte (scan, photo, PDF reçu par mail et déposé, XML eCH-0196), avec, pour une
QR-facture, le texte du code QR décodé par le client. ePost n'a pas de serveur MCP officiel : ses lettres arrivent
par ce même import de fichiers.

Sorties, toutes derrière une carte (outils `paper_*`, portes `nokker/household/paper/gates.py`) :
- événements d'agenda relus (programme d'école ou de club ; échéance d'une facture) → CalDAV du compte, ou fichier
  .ics à importer quand le compte n'a pas d'agenda CalDAV relié ;
- fichier `pain.001` PRÉPARÉ (carte FORTE), téléchargé et chargé par l'utilisateur dans son e-banking ;
- copie d'un papier vers un autre membre du foyer (partage explicite) ;
- rapport fiscal de l'année (lecture seule).

Cloisonnement : chaque compte a ses papiers dans SON locataire (`store.py`) ; le service ne lit que ceux du compte du
tour. Un document est une DONNÉE NON FIABLE : son texte n'arrive au modèle qu'encadré (`tools.py`), et rien de ce qu'il
contient ne choisit un compte à créditer (seul le code QR décodé le fait) ni un destinataire de partage (seul un
membre du foyer le peut). Journaux : identifiants seulement, jamais d'IBAN ni de montant.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from nokker.household.paper import dates as dates_mod
from nokker.household.paper import ech0196, pain001, qrbill
from nokker.household.paper.pdftext import extract_text
from nokker.household.paper.store import KINDS, Document, Export, Settings
from nokker.tenancy.context import current_tenant, tenant_scope

logger = logging.getLogger("nokker.household.paper")

MAX_BYTES = 10 * 1024 * 1024
MAX_PEOPLE = 12
MAX_PAYMENTS = 20
TZ = ZoneInfo("Europe/Zurich")
MEDIA = {"application/pdf": "pdf", "image/jpeg": "image", "image/png": "image", "image/webp": "image",
         "image/heic": "image", "application/xml": "xml", "text/xml": "xml", "text/plain": "text"}
_IBAN_TEXT = re.compile(r"\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?)\b")
_KIND_HINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("assurance", re.compile(r"(?i)\b(assurance|krankenkasse|versicherung|prime|police n)")),
    ("ecole", re.compile(r"(?i)\b([ée]cole|schule|coll[èe]ge|programme scolaire|club|verein|entra[îi]nement)")),
    ("banque", re.compile(r"(?i)\b(relev[ée] de compte|kontoauszug|extrait de compte|account statement)")),
    ("administration", re.compile(r"(?i)\b(commune|gemeinde|canton|kanton|administration|office cantonal|"
                                  r"service des contributions|steueramt|avs|ahv)")),
    ("sante", re.compile(r"(?i)\b(m[ée]decin|arzt|h[ôo]pital|spital|pharmacie|apotheke)")),
)


class PaperError(ValueError):
    """Demande refusée (400 / 404 côté route, texte d'erreur côté outil) : message sans IBAN ni montant."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def args_key(kind: str, arguments: dict[str, Any]) -> str:
    raw = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{kind}\n{raw}".encode()).hexdigest()


def _sniff(content: bytes, declared: str) -> str:
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    if content[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "image/heic"
    head = content[:200].lstrip(b"\xef\xbb\xbf \r\n\t")
    if head.startswith(b"<?xml") or head.startswith(b"<"):
        return "application/xml"
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return declared if declared in MEDIA and MEDIA[declared] == "image" else ""
    return "text/plain"


def _clean_name(s: Any, limit: int = 120) -> str:
    return " ".join(re.sub(r"[\x00-\x1f\x7f\[\]]", " ", str(s or "")).split())[:limit]


@dataclass
class PaymentPlan:
    payments: list[pain001.Payment] = field(default_factory=list)
    docs: list[Document] = field(default_factory=list)
    execution: date | None = None
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    debtor: pain001.Debtor | None = None

    def totals(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for p in self.payments:
            out[p.bill.currency] = out.get(p.bill.currency, Decimal(0)) + p.amount
        return out


@dataclass
class Paper:
    store: Any  # MemoryPaperStore | PgPaperStore
    # Locataire → compte (un compte = un locataire en bêta) : l'identité des papiers est celle du locataire du tour,
    # pas l'expéditeur de la décision d'une carte.
    account_of: Callable[[str], str | None]
    # Compte → membres de SON foyer [(account_id, email, tenant_id)], lui compris ; None ou [] : pas de foyer.
    household_members: Callable[[str], list[tuple[str, str, str]] | None] | None = None
    # Locataire → source d'outils agenda du compte (CalDAV : `calendar_create`) ou None.
    calendar_for: Callable[[str], Any] | None = None
    clock: Callable[[], float] = time.time

    # ─────────── identité ───────────

    def account(self) -> str:
        acc = self.account_of(current_tenant())
        if not acc:
            raise PaperError("aucun compte pour ce locataire", 403)
        return acc

    def today(self) -> date:
        return datetime.fromtimestamp(self.clock(), TZ).date()

    # ─────────── import ───────────

    def import_document(self, filename: str, media_type: str, content: bytes, *, qr_payload: str | None = None,
                        person: str | None = None) -> tuple[Document, bool]:
        """(document, nouveau). Le même fichier deux fois : le document existant, rien n'est dupliqué."""
        acc = self.account()
        if not content:
            raise PaperError("fichier vide")
        if len(content) > MAX_BYTES:
            raise PaperError(f"fichier trop volumineux ({MAX_BYTES // (1024 * 1024)} Mo au plus)")
        mt = _sniff(content, str(media_type or ""))
        if mt not in MEDIA:
            raise PaperError("type de fichier non pris en charge (PDF, JPEG, PNG, WebP, HEIC, XML, texte)")
        sha = hashlib.sha256(content).hexdigest()
        existing = self.store.by_sha(acc, sha)
        if existing is not None:
            return existing, False
        family = MEDIA[mt]
        text = ""
        bill: qrbill.QrBill | None = None
        tax: ech0196.TaxStatement | None = None
        if family == "pdf":
            text = extract_text(content)
        elif family == "text":
            text = content.decode("utf-8", "replace")[:200_000]
            if text.lstrip("﻿").startswith("SPC") and not qr_payload:
                qr_payload = text
        elif family == "xml":
            try:
                tax = ech0196.parse_xml(content)
            except ech0196.Ech0196Error as e:
                raise PaperError(f"XML : {e}") from None
        if qr_payload:
            try:
                bill = qrbill.parse(qr_payload)
            except qrbill.QrBillError as e:
                raise PaperError(f"QR-facture : {e}") from None
        if tax is None and text:
            tax = ech0196.from_text(text)
        settings = self.store.settings(acc)
        kind = "facture" if bill else "fiscal" if tax else self._guess_kind(text)
        year = tax.tax_period if tax else None
        if year is None and bill is not None:
            ref = bill.due_date or bill.billing.invoice_date
            year = ref.year if ref else None
        who = _clean_name(person, 60) if person else self._guess_person(text, settings.people)
        title = _clean_name(bill.creditor.name if bill else (tax.institution if tax and tax.institution else filename))
        doc = Document(doc_id="pd_" + uuid.uuid4().hex[:16], account_id=acc, created_at=self.clock(),
                       filename=_clean_name(filename, 160) or "document", media_type=mt, size=len(content),
                       sha256=sha, text=text, kind=kind, person=who, year=year,
                       qr=bill.to_json() if bill else None, tax=tax.to_json() if tax else None, title=title)
        self.store.put(doc, content)
        logger.info("papier importé : %s (%s, %s)", doc.doc_id, kind, mt)
        return doc, True

    @staticmethod
    def _guess_kind(text: str) -> str:
        head = (text or "")[:3000]
        for kind, rx in _KIND_HINTS:
            if rx.search(head):
                return kind
        return "inconnu" if not text else "autre"

    @staticmethod
    def _guess_person(text: str, people: list[str]) -> str:
        """La personne du foyer nommée dans l'en-tête (bloc d'adresse) ; une seule, sinon rien (jamais devinée)."""
        head = (text or "")[:800].lower()
        hits = [p for p in people if p and re.search(rf"(?<!\w){re.escape(p.lower())}(?!\w)", head)]
        return hits[0] if len(hits) == 1 else ""

    # ─────────── lecture ───────────

    def documents(self, *, kind: str | None = None, year: int | None = None, person: str | None = None) -> list[Document]:
        acc = self.account()
        out = self.store.list(acc)
        if kind:
            out = [d for d in out if d.kind == kind]
        if year:
            out = [d for d in out if d.year == year]
        if person:
            out = [d for d in out if d.person.lower() == str(person).lower()]
        return out

    def document(self, doc_id: str) -> Document:
        acc = self.account()
        doc = self.store.get(str(doc_id or ""))
        if doc is None or doc.account_id != acc:
            raise PaperError("papier inconnu", 404)
        return doc

    def content(self, doc_id: str) -> tuple[Document, bytes]:
        doc = self.document(doc_id)
        return doc, self.store.content(doc.doc_id) or b""

    @staticmethod
    def bill(doc: Document) -> qrbill.QrBill | None:
        return qrbill.QrBill.from_json(doc.qr) if doc.qr else None

    @staticmethod
    def brief(doc: Document) -> dict[str, Any]:
        """Ligne de liste (modèle et interface) : jamais d'IBAN complet ni de texte."""
        out: dict[str, Any] = {"id": doc.doc_id, "title": doc.title, "kind": doc.kind, "person": doc.person or None,
                               "year": doc.year, "file": doc.filename, "media_type": doc.media_type,
                               "imported": datetime.fromtimestamp(doc.created_at, TZ).strftime("%Y-%m-%d"),
                               "shared_by": doc.shared_by}
        if doc.qr:
            s = qrbill.QrBill.from_json(doc.qr).summary()
            out["bill"] = {k: s[k] for k in ("amount", "currency", "due_date", "reference_type")}
        if doc.tax:
            out["tax"] = ech0196.TaxStatement.from_json(doc.tax).summary()
        return out

    def file(self, doc_id: str, *, kind: str | None = None, person: str | None = None, year: Any = None,
             title: str | None = None) -> Document:
        doc = self.document(doc_id)
        if kind is not None:
            if kind not in KINDS:
                raise PaperError(f"type inconnu : {' | '.join(KINDS)}")
            doc.kind = kind
        if person is not None:
            people = self.store.settings(doc.account_id).people
            p = _clean_name(person, 60)
            if p and people and p.lower() not in {x.lower() for x in people}:
                raise PaperError("personne inconnue du foyer (réglages : personnes)")
            doc.person = p
        if year is not None:
            try:
                y = int(year)
            except (TypeError, ValueError):
                raise PaperError("année illisible") from None
            if not 1990 <= y <= 2100:
                raise PaperError("année hors bornes")
            doc.year = y
        if title is not None:
            doc.title = _clean_name(title) or doc.title
        self.store.update(doc)
        return doc

    def delete(self, doc_id: str) -> bool:
        doc = self.document(doc_id)
        return self.store.delete(doc.doc_id)

    # ─────────── réglages (route du compte seulement, jamais un outil du modèle) ───────────

    def settings(self) -> Settings:
        return self.store.settings(self.account())

    def put_settings(self, body: dict[str, Any]) -> Settings:
        acc = self.account()
        cur = self.store.settings(acc)
        if not isinstance(body, dict) or set(body) - {"debtor_name", "debtor_iban", "debtor_bic", "people",
                                                      "tax_sources"}:
            raise PaperError("corps attendu : {debtor_name?, debtor_iban?, debtor_bic?, people?, tax_sources?}")
        new = replace(cur)
        if "debtor_name" in body:
            new.debtor_name = _clean_name(body["debtor_name"], 70)
        if "debtor_iban" in body:
            iban = re.sub(r"\s+", "", str(body["debtor_iban"] or "")).upper()
            if iban and not (15 <= len(iban) <= 34 and iban[:2].isalpha() and iban.isalnum()
                             and qrbill.iban_mod97_ok(iban)):
                raise PaperError("IBAN du compte débité invalide")
            if iban[:2] in ("CH", "LI") and iban[4:9].isdigit() and int(iban[4:9]) in qrbill.QR_IID:
                raise PaperError("un QR-IBAN ne sert qu'à recevoir des paiements")
            new.debtor_iban = iban
        if "debtor_bic" in body:
            bic = str(body["debtor_bic"] or "").strip().upper()
            if bic and not (len(bic) in (8, 11) and bic.isalnum()):
                raise PaperError("BIC invalide (8 ou 11 caractères)")
            new.debtor_bic = bic
        for key in ("people", "tax_sources"):
            if key in body:
                items = body[key] or []
                if not isinstance(items, list) or len(items) > MAX_PEOPLE * 4:
                    raise PaperError(f"{key} : une liste")
                names = []
                for x in items:
                    s = _clean_name(x, 60)
                    if s and s.lower() not in {n.lower() for n in names}:
                        names.append(s)
                if key == "people" and len(names) > MAX_PEOPLE:
                    raise PaperError(f"people : {MAX_PEOPLE} personnes au plus")
                setattr(new, key, names)
        self.store.put_settings(new)
        return new

    @staticmethod
    def settings_json(s: Settings) -> dict[str, Any]:
        return {"debtor_name": s.debtor_name, "debtor_iban": qrbill.masked_iban(s.debtor_iban) if s.debtor_iban else "",
                "debtor_bic": s.debtor_bic, "people": s.people, "tax_sources": s.tax_sources}

    # ─────────── agenda : relecture puis écriture (carte standard) ───────────

    def calendar_review(self, arguments: dict[str, Any]) -> tuple[list[dates_mod.Reviewed], Document | None]:
        acc = self.account()
        doc = self.document(arguments["document_id"]) if arguments.get("document_id") else None
        people = self.store.settings(acc).people
        text = doc.text if doc is not None and doc.text else None
        try:
            reviewed = dates_mod.review(arguments.get("events"), people=people, document_text=text)
        except ValueError as e:
            raise PaperError(str(e)) from None
        return reviewed, doc

    def calendar_target(self) -> str:
        src = self.calendar_for(current_tenant()) if self.calendar_for is not None else None
        return "l'agenda CalDAV du compte" if src is not None else "un fichier .ics à importer (aucun agenda CalDAV relié)"

    def bill_due_review(self, arguments: dict[str, Any]) -> tuple[list[dates_mod.Reviewed], Document]:
        """Échéance d'une facture : celle de la QR-facture (/11/ + /40/0:n), sinon celle que le modèle a lue dans la
        lettre (`due_date` + `source`, relue comme un événement)."""
        doc = self.document(arguments.get("document_id"))
        bill = self.bill(doc)
        if bill is None:
            raise PaperError("ce papier n'a pas de QR-facture décodée")
        amount = f"{bill.currency} {bill.amount:.2f}" if bill.amount is not None else "montant libre"
        title = f"Échéance : {bill.creditor.name} ({amount})"
        if bill.due_date is not None:
            r = dates_mod.Reviewed(index=0, title=title, date=bill.due_date.isoformat(), person=doc.person,
                                   source="code QR", notes=["échéance calculée depuis la QR-facture"])
            return [r], doc
        due, source = str(arguments.get("due_date") or ""), str(arguments.get("source") or "")
        if not due:
            r = dates_mod.Reviewed(index=0, title=title, date="", source="",
                                   problems=["aucune échéance dans le code QR : lire la date dans la lettre "
                                             "(due_date et son texte source)"])
            return [r], doc
        people = self.store.settings(doc.account_id).people
        reviewed = dates_mod.review([{"title": title, "date": due, "source": source, "person": doc.person,
                                      "year_source": arguments.get("year_source")}], people=people,
                                    document_text=doc.text or None)
        return reviewed, doc

    def write_events(self, reviewed: list[dates_mod.Reviewed], doc: Document | None, key: str) -> dict[str, Any]:
        """Écrit les événements SANS problème ; les autres restent à préciser. CalDAV si relié, sinon un .ics."""
        ok = [r for r in reviewed if r.ok]
        skipped = [r.to_json() for r in reviewed if not r.ok]
        if not ok:
            return {"written": 0, "skipped": skipped, "message": "aucun événement sans ambiguïté : rien n'est écrit"}
        acc = self.account()
        src = self.calendar_for(current_tenant()) if self.calendar_for is not None else None
        origin = f"Ajouté par Nokker depuis « {doc.filename} »." if doc is not None else "Ajouté par Nokker."
        if src is not None:
            from nokker.core.ports import ToolContext

            done, failed = [], []
            for r in ok:
                start, end = _span(r)
                summary = f"[{r.person}] {r.title}" if r.person else r.title
                res = src.call("calendar_create", {"summary": summary, "start": start, "end": end,
                                                   "location": r.location or None, "description": origin},
                               ToolContext(thread_id="paper", turn_index=0, channel="paper"))
                (failed if res.is_error else done).append(r.index + 1)
            return {"written": len(done), "failed": failed, "skipped": skipped, "target": "agenda CalDAV"}
        ics = _ics(ok, origin)
        e = Export(export_id="px_" + uuid.uuid4().hex[:16], account_id=acc, created_at=self.clock(), kind="ics",
                   status="ready", key=key, content=ics, sha256=hashlib.sha256(ics.encode()).hexdigest(),
                   summary={"events": len(ok)})
        self.store.put_export(e)
        return {"written": len(ok), "skipped": skipped, "target": "fichier .ics",
                "download": f"/v1/paper/exports/{e.export_id}"}

    # ─────────── paiement : préparé, jamais payé (carte FORTE) ───────────

    def payment_plan(self, arguments: dict[str, Any]) -> PaymentPlan:
        acc = self.account()
        plan = PaymentPlan()
        items = arguments.get("items")
        if not isinstance(items, list) or not items:
            raise PaperError("items : une liste de factures [{document_id, amount?}]")
        if len(items) > MAX_PAYMENTS:
            raise PaperError(f"au plus {MAX_PAYMENTS} paiements par fichier")
        s = self.store.settings(acc)
        if not s.debtor_iban or not s.debtor_name:
            plan.problems.append("compte débité non réglé : l'utilisateur pose le nom et l'IBAN du compte qui paie "
                                 "dans Papiers > Réglages (jamais depuis un document)")
        else:
            plan.debtor = pain001.Debtor(name=s.debtor_name, iban=s.debtor_iban, bic=s.debtor_bic)
        seen: set[str] = set()
        prepared = {d for e in self.store.exports(acc) if e.kind == "pain001" and e.status == "ready"
                    for d in e.summary.get("documents", [])}
        for n, it in enumerate(items, start=1):
            it = it if isinstance(it, dict) else {}
            doc_id = str(it.get("document_id") or "")
            if doc_id in seen:
                plan.problems.append(f"{n} : même facture deux fois")
                continue
            seen.add(doc_id)
            try:
                doc = self.document(doc_id)
            except PaperError:
                plan.problems.append(f"{n} : papier inconnu")
                continue
            bill = self.bill(doc)
            if bill is None:
                plan.problems.append(f"{n} : « {doc.title} » n'a pas de QR-facture décodée")
                continue
            amount = bill.amount
            if amount is None:
                try:
                    amount = Decimal(str(it.get("amount"))).quantize(Decimal("0.01"))
                except (InvalidOperation, ValueError):
                    amount = None
                if amount is None or amount <= 0:
                    plan.problems.append(f"{n} : facture sans montant, donner `amount`")
                    continue
                plan.warnings.append(f"{n} : montant saisi (la facture n'en porte pas)")
            elif it.get("amount") not in (None, "") and str(it.get("amount")) not in (f"{amount:.2f}", str(amount)):
                plan.problems.append(f"{n} : le montant demandé diffère de celui du code QR")
                continue
            if plan.debtor is not None and bill.iban == plan.debtor.iban.replace(" ", "").upper():
                plan.problems.append(f"{n} : le créancier est le compte débité")
                continue
            for w in bill.warnings:
                plan.warnings.append(f"{n} : {w}")
            other = {re.sub(r"\s+", "", m) for m in _IBAN_TEXT.findall(doc.text or "")}
            other = {i for i in other if len(i) >= 15 and qrbill.iban_mod97_ok(i) and i != bill.iban}
            if other:
                plan.warnings.append(f"{n} : le texte de la lettre cite un autre IBAN que le code QR ; seul celui du "
                                     "code QR est utilisé")
            if doc.doc_id in prepared:
                plan.warnings.append(f"{n} : cette facture est déjà dans un fichier préparé (paiement en double ?)")
            plan.docs.append(doc)
            plan.payments.append(pain001.Payment(bill=bill, amount=amount, end_to_end=f"NK-{doc.doc_id[3:19]}"))
        today = self.today()
        raw = str(arguments.get("execution_date") or "")
        if raw:
            try:
                plan.execution = date.fromisoformat(raw)
            except ValueError:
                plan.problems.append("execution_date illisible (AAAA-MM-JJ)")
            else:
                if plan.execution < today:
                    plan.problems.append("date d'exécution dans le passé")
        else:
            dues = [p.bill.due_date for p in plan.payments if p.bill.due_date and p.bill.due_date >= today]
            plan.execution = min(dues) if dues else _next_business_day(today)
        return plan

    def payment_card(self, arguments: dict[str, Any]) -> tuple[PaymentPlan, Export | None]:
        """Relecture du paiement pour la carte FORTE. Plan valide : le fichier est construit, validé au schéma et rangé
        `pending` sous l'empreinte des arguments ; la carte en montre l'empreinte. Rien n'est téléchargeable avant
        l'approbation."""
        plan = self.payment_plan(arguments)
        if plan.problems or not plan.payments or plan.debtor is None or plan.execution is None:
            return plan, None
        acc = self.account()
        key = args_key("pain001", arguments)
        existing = self.store.export_by_key(acc, key)
        if existing is not None and existing.status == "pending":
            return plan, existing
        now = self.clock()
        msg_id = f"NOKKER-{datetime.fromtimestamp(now, TZ):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
        try:
            xml = pain001.build(plan.debtor, plan.payments, execution=plan.execution, msg_id=msg_id,
                                created=datetime.fromtimestamp(now, TZ))
        except pain001.Pain001Error as e:
            plan.problems.append(str(e))
            return plan, None
        errors = pain001.validate(xml)
        if errors:
            logger.error("pain.001 invalide au schéma SPS (%d erreurs)", len(errors))
            plan.problems.append("fichier non conforme au schéma SPS : préparation refusée")
            return plan, None
        if not pain001.schema_available():
            plan.warnings.append("fichier non validé au schéma SPS (schéma absent de cette installation) : "
                                 "l'e-banking le contrôlera au chargement")
        totals = {k: f"{v:.2f}" for k, v in plan.totals().items()}
        e = Export(export_id="px_" + uuid.uuid4().hex[:16], account_id=acc, created_at=now, kind="pain001",
                   status="pending", key=key, content=xml, sha256=pain001.sha256(xml),
                   summary={"documents": [d.doc_id for d in plan.docs], "count": len(plan.payments),
                            "totals": totals, "execution_date": plan.execution.isoformat(), "msg_id": msg_id})
        self.store.put_export(e)
        logger.info("pain.001 préparé en attente de carte : %s (%d paiements)", e.export_id, len(plan.payments))
        return plan, e

    def payment_release(self, arguments: dict[str, Any]) -> Export:
        """Carte FORTE approuvée : le fichier préparé (même empreinte d'arguments) devient téléchargeable."""
        acc = self.account()
        e = self.store.export_by_key(acc, args_key("pain001", arguments))
        if e is None:
            raise PaperError("fichier préparé introuvable : relancer la préparation")
        if e.status != "ready":
            self.store.mark_export(e.export_id, "ready")
            e.status = "ready"
        logger.info("pain.001 libéré après carte forte : %s", e.export_id)
        return e

    def exports(self) -> list[Export]:
        return self.store.exports(self.account())

    def export(self, export_id: str) -> Export:
        acc = self.account()
        e = self.store.export(str(export_id or ""))
        if e is None or e.account_id != acc:
            raise PaperError("fichier inconnu", 404)
        return e

    # ─────────── partage entre membres du foyer (carte standard) ───────────

    def share_target(self, member: str) -> tuple[str, str, str]:
        """(compte, e-mail, locataire) d'un AUTRE membre du foyer du compte, désigné par son e-mail ou son id."""
        acc = self.account()
        members = self.household_members(acc) if self.household_members is not None else None
        if not members:
            raise PaperError("pas de foyer : partager demande un foyer Nokker")
        want = str(member or "").strip().lower()
        for m in members:
            if m[0] != acc and want in (m[0].lower(), m[1].lower()):
                return m
        raise PaperError("membre inconnu de ce foyer")

    def share(self, doc_id: str, member: str) -> dict[str, Any]:
        doc, content = self.content(doc_id)
        target_acc, email, target_tenant = self.share_target(member)
        copy = replace(doc, doc_id="pd_" + uuid.uuid4().hex[:16], account_id=target_acc, created_at=self.clock(),
                       shared_by=doc.account_id)
        with tenant_scope(target_tenant):  # écrit dans le locataire du destinataire (RLS), et rien d'autre
            if self.store.by_sha(target_acc, doc.sha256) is None:
                self.store.put(copy, content)
        logger.info("papier partagé : %s → compte %s", doc.doc_id, target_acc)
        return {"shared": True, "with": email, "document": doc.title}

    # ─────────── impôts ───────────

    def tax_report(self, year: int | None = None) -> dict[str, Any]:
        """Dossier fiscal de `year` (défaut : l'année écoulée) : relevés reçus, établissements attendus qui manquent
        (ceux d'un relevé de l'année d'avant, ceux des réglages `tax_sources`), relevés à confirmer, factures de
        l'année. Lecture seule ; le remplissage de la déclaration reste humain."""
        acc = self.account()
        y = int(year) if year else self.today().year - 1
        docs = self.store.list(acc)
        received, unsure, prev = [], [], set()
        for d in docs:
            if not d.tax:
                continue
            t = ech0196.TaxStatement.from_json(d.tax)
            if t.tax_period == y:
                (received if t.exact else unsure).append({"id": d.doc_id, "title": d.title, **t.summary()})
            elif t.tax_period == y - 1 and t.institution:
                prev.add(t.institution)
        have = {r["institution"].lower() for r in received if r.get("institution")}
        expected = sorted(prev | set(self.store.settings(acc).tax_sources), key=str.lower)
        missing = [i for i in expected if i.lower() not in have]
        bills = [d for d in docs if d.kind == "facture" and d.year == y]
        return {"year": y, "received": received, "to_confirm": unsure, "missing": missing,
                "expected": expected, "bills": len(bills),
                "note": "liste préparée par le noyau ; la déclaration se remplit et se dépose à la main"}


def _span(r: dates_mod.Reviewed) -> tuple[str, str]:
    if not r.start:
        return r.date, r.date
    start = f"{r.date}T{r.start}"
    if r.end:
        return start, f"{r.date}T{r.end}"
    t = datetime.fromisoformat(start) + timedelta(hours=1)
    return start, t.strftime("%Y-%m-%dT%H:%M")


def _next_business_day(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _ics(events: list[dates_mod.Reviewed], description: str) -> str:
    from nokker.connectors.calendar import escape_text, fold

    stamp = datetime.now(TZ).astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Nokker//Papiers du foyer//FR", "CALSCALE:GREGORIAN"]
    for r in events:
        start, end = _span(r)
        lines += ["BEGIN:VEVENT", f"UID:{uuid.uuid4().hex}@nokker", f"DTSTAMP:{stamp}"]
        if "T" in start:
            lines += [f"DTSTART;TZID=Europe/Zurich:{start.replace('-', '').replace(':', '')}00",
                      f"DTEND;TZID=Europe/Zurich:{end.replace('-', '').replace(':', '')}00"]
        else:
            d = date.fromisoformat(start)
            lines += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}"]
        summary = f"[{r.person}] {r.title}" if r.person else r.title
        lines.append(fold(f"SUMMARY:{escape_text(summary)}"))
        if r.location:
            lines.append(fold(f"LOCATION:{escape_text(r.location)}"))
        lines.append(fold(f"DESCRIPTION:{escape_text(description)}"))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


__all__ = ["MAX_BYTES", "Paper", "PaperError", "PaymentPlan", "args_key"]
