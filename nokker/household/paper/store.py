"""Magasin des papiers du foyer : documents importés, fichiers préparés (pain.001, .ics), réglages du compte.

Tout est rangé dans le locataire du COMPTE qui importe (un compte = un locataire, docs/TENANCY.md) : la RLS de
Postgres (migration 055, `tenant_isolation` forcée) fait qu'un membre du foyer ne lit jamais les papiers d'un autre.
Partager, c'est COPIER le document dans le locataire du destinataire (`shared_by` dit d'où il vient), par le service,
après une carte : il n'y a aucune politique de lecture croisée.

`MemoryPaperStore` reproduit la même séparation (un dictionnaire par locataire courant) pour les tests et Lokkal sans
Postgres. Aucun des deux n'écrit de journal avec un contenu.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from typing import Any

from nokker.tenancy.context import current_tenant

KINDS = ("facture", "fiscal", "banque", "assurance", "administration", "ecole", "sante", "autre", "inconnu")


@dataclass
class Document:
    doc_id: str
    account_id: str
    created_at: float
    filename: str
    media_type: str
    size: int
    sha256: str
    text: str = ""
    kind: str = "inconnu"
    person: str = ""
    year: int | None = None
    qr: dict[str, Any] | None = None  # QrBill.to_json() (IBAN complet : base seulement)
    tax: dict[str, Any] | None = None  # TaxStatement.to_json()
    shared_by: str | None = None  # compte qui l'a partagé (copie), None pour un import
    title: str = ""


@dataclass
class Export:
    export_id: str
    account_id: str
    created_at: float
    kind: str  # pain001 | ics
    status: str  # pending (carte posée) | ready (carte approuvée : téléchargeable)
    key: str  # empreinte des arguments de la carte : relie la carte au fichier préparé
    content: str
    sha256: str
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class Settings:
    account_id: str
    debtor_name: str = ""
    debtor_iban: str = ""
    debtor_bic: str = ""
    people: list[str] = field(default_factory=list)
    tax_sources: list[str] = field(default_factory=list)


class MemoryPaperStore:
    def __init__(self) -> None:
        self._docs: dict[str, dict[str, tuple[Document, bytes]]] = {}
        self._exports: dict[str, dict[str, Export]] = {}
        self._settings: dict[str, dict[str, Settings]] = {}
        self._lock = threading.Lock()

    def _t(self, table: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return table.setdefault(current_tenant(), {})

    def put(self, doc: Document, content: bytes) -> None:
        with self._lock:
            self._t(self._docs)[doc.doc_id] = (replace(doc), bytes(content))

    def get(self, doc_id: str) -> Document | None:
        with self._lock:
            hit = self._t(self._docs).get(doc_id)
        return replace(hit[0]) if hit else None

    def content(self, doc_id: str) -> bytes | None:
        with self._lock:
            hit = self._t(self._docs).get(doc_id)
        return hit[1] if hit else None

    def list(self, account_id: str) -> list[Document]:
        with self._lock:
            docs = [replace(d) for d, _ in self._t(self._docs).values() if d.account_id == account_id]
        return sorted(docs, key=lambda d: d.created_at, reverse=True)

    def update(self, doc: Document) -> None:
        with self._lock:
            t = self._t(self._docs)
            if doc.doc_id in t:
                t[doc.doc_id] = (replace(doc), t[doc.doc_id][1])

    def delete(self, doc_id: str) -> bool:
        with self._lock:
            return self._t(self._docs).pop(doc_id, None) is not None

    def by_sha(self, account_id: str, sha: str) -> Document | None:
        with self._lock:
            for d, _ in self._t(self._docs).values():
                if d.account_id == account_id and d.sha256 == sha:
                    return replace(d)
        return None

    def put_export(self, e: Export) -> None:
        with self._lock:
            self._t(self._exports)[e.export_id] = replace(e)

    def export(self, export_id: str) -> Export | None:
        with self._lock:
            e = self._t(self._exports).get(export_id)
        return replace(e) if e else None

    def export_by_key(self, account_id: str, key: str) -> Export | None:
        with self._lock:
            for e in self._t(self._exports).values():
                if e.account_id == account_id and e.key == key:
                    return replace(e)
        return None

    def exports(self, account_id: str) -> list[Export]:
        with self._lock:
            out = [replace(e) for e in self._t(self._exports).values() if e.account_id == account_id]
        return sorted(out, key=lambda e: e.created_at, reverse=True)

    def mark_export(self, export_id: str, status: str) -> None:
        with self._lock:
            e = self._t(self._exports).get(export_id)
            if e is not None:
                e.status = status

    def settings(self, account_id: str) -> Settings:
        with self._lock:
            s = self._t(self._settings).get(account_id)
        return replace(s) if s else Settings(account_id=account_id)

    def put_settings(self, s: Settings) -> None:
        with self._lock:
            self._t(self._settings)[s.account_id] = replace(s)


class PgPaperStore:
    """Tables `paper_documents`, `paper_exports`, `paper_settings` (migration 055), RLS forcée par locataire."""

    _DOC = ("doc_id, account_id, created_at, filename, media_type, size, sha256, text, kind, person, year, qr, tax, "
            "shared_by, title")

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):
        from nokker.store.conn import connect

        return connect(self.dsn)

    @staticmethod
    def _doc(r: Any) -> Document:
        return Document(doc_id=r[0], account_id=r[1], created_at=float(r[2]), filename=r[3], media_type=r[4],
                        size=int(r[5]), sha256=r[6], text=r[7] or "", kind=r[8], person=r[9] or "", year=r[10],
                        qr=r[11], tax=r[12], shared_by=r[13], title=r[14] or "")

    def put(self, doc: Document, content: bytes) -> None:
        from psycopg.types.json import Jsonb

        with self._conn() as conn:
            conn.execute(f"INSERT INTO paper_documents ({self._DOC}, content) VALUES "
                         "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                         (doc.doc_id, doc.account_id, doc.created_at, doc.filename, doc.media_type, doc.size,
                          doc.sha256, doc.text, doc.kind, doc.person, doc.year,
                          Jsonb(doc.qr) if doc.qr is not None else None,
                          Jsonb(doc.tax) if doc.tax is not None else None, doc.shared_by, doc.title, content))

    def get(self, doc_id: str) -> Document | None:
        with self._conn() as conn:
            r = conn.execute(f"SELECT {self._DOC} FROM paper_documents WHERE doc_id = %s", (doc_id,)).fetchone()
        return self._doc(r) if r else None

    def content(self, doc_id: str) -> bytes | None:
        with self._conn() as conn:
            r = conn.execute("SELECT content FROM paper_documents WHERE doc_id = %s", (doc_id,)).fetchone()
        return bytes(r[0]) if r else None

    def list(self, account_id: str) -> list[Document]:
        with self._conn() as conn:
            rows = conn.execute(f"SELECT {self._DOC} FROM paper_documents WHERE account_id = %s "
                                "ORDER BY created_at DESC LIMIT 1000", (account_id,)).fetchall()
        return [self._doc(r) for r in rows]

    def update(self, doc: Document) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE paper_documents SET kind = %s, person = %s, year = %s, title = %s WHERE doc_id = %s",
                         (doc.kind, doc.person, doc.year, doc.title, doc.doc_id))

    def delete(self, doc_id: str) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM paper_documents WHERE doc_id = %s", (doc_id,)).rowcount > 0

    def by_sha(self, account_id: str, sha: str) -> Document | None:
        with self._conn() as conn:
            r = conn.execute(f"SELECT {self._DOC} FROM paper_documents WHERE account_id = %s AND sha256 = %s LIMIT 1",
                             (account_id, sha)).fetchone()
        return self._doc(r) if r else None

    _EXP = "export_id, account_id, created_at, kind, status, key, content, sha256, summary"

    @staticmethod
    def _exp(r: Any) -> Export:
        return Export(export_id=r[0], account_id=r[1], created_at=float(r[2]), kind=r[3], status=r[4], key=r[5],
                      content=r[6], sha256=r[7], summary=r[8] or {})

    def put_export(self, e: Export) -> None:
        from psycopg.types.json import Jsonb

        with self._conn() as conn:
            conn.execute(f"INSERT INTO paper_exports ({self._EXP}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                         (e.export_id, e.account_id, e.created_at, e.kind, e.status, e.key, e.content, e.sha256,
                          Jsonb(e.summary)))

    def export(self, export_id: str) -> Export | None:
        with self._conn() as conn:
            r = conn.execute(f"SELECT {self._EXP} FROM paper_exports WHERE export_id = %s", (export_id,)).fetchone()
        return self._exp(r) if r else None

    def export_by_key(self, account_id: str, key: str) -> Export | None:
        with self._conn() as conn:
            r = conn.execute(f"SELECT {self._EXP} FROM paper_exports WHERE account_id = %s AND key = %s "
                             "ORDER BY created_at DESC LIMIT 1", (account_id, key)).fetchone()
        return self._exp(r) if r else None

    def exports(self, account_id: str) -> list[Export]:
        with self._conn() as conn:
            rows = conn.execute(f"SELECT {self._EXP} FROM paper_exports WHERE account_id = %s "
                                "ORDER BY created_at DESC LIMIT 200", (account_id,)).fetchall()
        return [self._exp(r) for r in rows]

    def mark_export(self, export_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE paper_exports SET status = %s WHERE export_id = %s", (status, export_id))

    def settings(self, account_id: str) -> Settings:
        with self._conn() as conn:
            r = conn.execute("SELECT debtor_name, debtor_iban, debtor_bic, people, tax_sources FROM paper_settings "
                             "WHERE account_id = %s", (account_id,)).fetchone()
        if not r:
            return Settings(account_id=account_id)
        return Settings(account_id=account_id, debtor_name=r[0] or "", debtor_iban=r[1] or "", debtor_bic=r[2] or "",
                        people=list(r[3] or []), tax_sources=list(r[4] or []))

    def put_settings(self, s: Settings) -> None:
        from psycopg.types.json import Jsonb

        with self._conn() as conn:
            conn.execute("INSERT INTO paper_settings (account_id, debtor_name, debtor_iban, debtor_bic, people, "
                         "tax_sources) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id, account_id) DO UPDATE "
                         "SET debtor_name = EXCLUDED.debtor_name, debtor_iban = EXCLUDED.debtor_iban, "
                         "debtor_bic = EXCLUDED.debtor_bic, people = EXCLUDED.people, "
                         "tax_sources = EXCLUDED.tax_sources",
                         (s.account_id, s.debtor_name, s.debtor_iban, s.debtor_bic, Jsonb(s.people),
                          Jsonb(s.tax_sources)))


__all__ = ["KINDS", "Document", "Export", "MemoryPaperStore", "PgPaperStore", "Settings"]
