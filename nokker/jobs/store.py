"""Profils emploi et versions locales (docs/JOBS-PACK.md §4.1, §5.3, §5.4), migration 073.

- `JobsProfile` : l'état du pack pour UN membre (id `em_` + 10), dans SON locataire : activé ou non, compte partenaire
  du service emploi (opaque, créé au premier CV rattaché), CV de base, date d'effacement prévue après désactivation
  (30 jours, décision du 05.11).
- `JobsDoc` : une version retouchée chez Nokker (`cvl_` CV, `lt_` lettre) : document ou texte, PDF et son empreinte.

Mémoire (tests, noyau sans Postgres) et Postgres (RLS `tenant_isolation`, comme 069).
"""
from __future__ import annotations

import copy
import json
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

_ALPHA = "abcdefghijklmnopqrstuvwxyz0123456789"


def new_id(prefix: str) -> str:
    return prefix + "_" + "".join(secrets.choice(_ALPHA) for _ in range(10))


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class JobsProfile:
    id: str
    account_id: str
    tenant_id: str
    active: bool = False
    partner_account_id: str = ""
    base_cv_id: str = ""
    cv_name: str = ""
    created_at: datetime = field(default_factory=_now)
    activated_at: datetime | None = None
    deactivated_at: datetime | None = None
    erase_after: datetime | None = None


@dataclass
class JobsDoc:
    id: str
    profile_id: str
    kind: str  # cv | letter
    parent: str  # ref de la version d'origine (em_…|cv:…, cvl_…, lt_…) ; vide pour une première lettre
    offer: str = ""  # ref de l'offre visée
    data: dict[str, Any] = field(default_factory=dict)
    pdf: bytes = b""
    pdf_sha256: str = ""
    created_at: datetime = field(default_factory=_now)


class JobsStore(Protocol):
    def profile_of(self, account_id: str) -> JobsProfile | None: ...
    def profile(self, profile_id: str) -> JobsProfile | None: ...
    def save_profile(self, p: JobsProfile) -> JobsProfile: ...
    def delete_profile(self, profile_id: str) -> None: ...
    def add_doc(self, d: JobsDoc) -> JobsDoc: ...
    def doc(self, doc_id: str) -> JobsDoc | None: ...


class MemoryJobsStore:
    """Rangement par locataire courant, comme la RLS : un locataire ne voit jamais les lignes d'un autre."""

    def __init__(self) -> None:
        self._profiles: dict[str, JobsProfile] = {}
        self._docs: dict[str, JobsDoc] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _tid() -> str:
        from nokker.tenancy.context import current_tenant

        return current_tenant()

    def profile_of(self, account_id: str) -> JobsProfile | None:
        tid = self._tid()
        with self._lock:
            p = next((p for p in self._profiles.values() if p.account_id == account_id and p.tenant_id == tid), None)
            return copy.deepcopy(p)

    def profile(self, profile_id: str) -> JobsProfile | None:
        tid = self._tid()
        with self._lock:
            p = self._profiles.get(profile_id)
            return copy.deepcopy(p) if p is not None and p.tenant_id == tid else None

    def save_profile(self, p: JobsProfile) -> JobsProfile:
        tid = self._tid()
        if p.tenant_id != tid:
            raise PermissionError("profil d'un autre locataire")
        with self._lock:
            other = next((q for q in self._profiles.values() if q.account_id == p.account_id and q.id != p.id), None)
            if other is not None:
                raise ValueError("un seul profil emploi par compte")
            self._profiles[p.id] = copy.deepcopy(p)
        return p

    def delete_profile(self, profile_id: str) -> None:
        tid = self._tid()
        with self._lock:
            p = self._profiles.get(profile_id)
            if p is not None and p.tenant_id == tid:
                del self._profiles[profile_id]
                for k in [k for k, d in self._docs.items() if d.profile_id == profile_id]:
                    del self._docs[k]

    def add_doc(self, d: JobsDoc) -> JobsDoc:
        if self.profile(d.profile_id) is None:
            raise PermissionError("profil absent de ce locataire")
        with self._lock:
            self._docs[d.id] = copy.deepcopy(d)
        return d

    def doc(self, doc_id: str) -> JobsDoc | None:
        with self._lock:
            d = self._docs.get(doc_id)
        if d is None or self.profile(d.profile_id) is None:
            return None
        return copy.deepcopy(d)


_P_COLS = ("id, account_id, tenant_id, active, partner_account_id, base_cv_id, cv_name, created_at, activated_at, "
           "deactivated_at, erase_after")
_D_COLS = "id, profile_id, kind, parent, offer, data, pdf, pdf_sha256, created_at"


class PgJobsStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):
        from psycopg.rows import dict_row

        from nokker.store.conn import connect

        return connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _p(r: dict[str, Any] | None) -> JobsProfile | None:
        return JobsProfile(**r) if r else None

    @staticmethod
    def _d(r: dict[str, Any] | None) -> JobsDoc | None:
        if not r:
            return None
        r = dict(r)
        r["pdf"] = bytes(r["pdf"] or b"")
        r["data"] = r["data"] if isinstance(r["data"], dict) else json.loads(r["data"] or "{}")
        return JobsDoc(**r)

    def profile_of(self, account_id: str) -> JobsProfile | None:
        with self._conn() as c:
            return self._p(c.execute(f"SELECT {_P_COLS} FROM jobs_profiles WHERE account_id = %s",
                                     (account_id,)).fetchone())

    def profile(self, profile_id: str) -> JobsProfile | None:
        with self._conn() as c:
            return self._p(c.execute(f"SELECT {_P_COLS} FROM jobs_profiles WHERE id = %s", (profile_id,)).fetchone())

    def save_profile(self, p: JobsProfile) -> JobsProfile:
        with self._conn() as c:
            c.execute(
                f"INSERT INTO jobs_profiles ({_P_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET active = EXCLUDED.active, partner_account_id = "
                "EXCLUDED.partner_account_id, base_cv_id = EXCLUDED.base_cv_id, cv_name = EXCLUDED.cv_name, "
                "activated_at = EXCLUDED.activated_at, deactivated_at = EXCLUDED.deactivated_at, "
                "erase_after = EXCLUDED.erase_after",
                (p.id, p.account_id, p.tenant_id, p.active, p.partner_account_id, p.base_cv_id, p.cv_name,
                 p.created_at, p.activated_at, p.deactivated_at, p.erase_after))
        return p

    def delete_profile(self, profile_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM jobs_profiles WHERE id = %s", (profile_id,))

    def add_doc(self, d: JobsDoc) -> JobsDoc:
        from psycopg.types.json import Jsonb

        with self._conn() as c:
            c.execute(f"INSERT INTO jobs_docs ({_D_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      (d.id, d.profile_id, d.kind, d.parent, d.offer, Jsonb(d.data), d.pdf, d.pdf_sha256,
                       d.created_at))
        return d

    def doc(self, doc_id: str) -> JobsDoc | None:
        with self._conn() as c:
            return self._d(c.execute(f"SELECT {_D_COLS} FROM jobs_docs WHERE id = %s", (doc_id,)).fetchone())

    def partner_accounts(self) -> list[str]:
        """Comptes partenaires du locataire courant (effacement du compte Nokker)."""
        with self._conn() as c:
            rows = c.execute("SELECT partner_account_id FROM jobs_profiles WHERE partner_account_id <> ''").fetchall()
        return [r["partner_account_id"] for r in rows]


__all__ = ["JobsDoc", "JobsProfile", "JobsStore", "MemoryJobsStore", "PgJobsStore", "new_id"]
