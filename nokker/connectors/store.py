"""Identifiants de connecteurs par compte (contrat C3, docs/CONNECTORS-MULTI.md). Le secret est chiffré (Fernet) avant
d'atteindre la base ; les `settings` (hôtes, ports, adresse, URL) sont en clair et ne contiennent jamais de secret.

Plusieurs connecteurs par (compte, nature) depuis 068 : chacun a un `id` stable (`mb_…` mail, `ca_…` agenda), un
`label` modifiable (unique par compte et par nature, sans la casse) et un seul est `is_default` par nature.
L'interface par nature (`put`, `get`, `delete`, `mark_ok` avec `kind`) vise le connecteur PAR DÉFAUT : c'est celle des
appelants d'avant 068 (relève mail, livraison du brief, Lokkal)."""
from __future__ import annotations

import json
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from nokker.connectors.crypto import SecretBox
from nokker.connectors.errors import ConnectorError

KINDS = ("mail", "calendar")  # natures des routes d'avant 068 (une carte par nature, le défaut)
ALL_KINDS = ("mail", "calendar", "contacts", "files", "tasks", "pages")  # 069 (docs/CONNECTORS-OAUTH.md)
PREFIX = {"mail": "mb", "calendar": "ca", "contacts": "ct", "files": "fl", "tasks": "tk", "pages": "pg"}
ID_RE = re.compile(r"^(mb|ca|ct|fl|tk|pg)_[a-z0-9]{10}$")
MAX_PER_KIND = 10
LABEL_MAX = 40
_ID_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"


@dataclass(frozen=True)
class Status:
    """Ce que montre la carte Connecteurs : jamais le secret."""

    account_id: str
    kind: str
    settings: dict[str, Any]
    created_at: datetime
    last_ok_at: datetime | None
    id: str = ""
    label: str = ""
    is_default: bool = True


@dataclass(frozen=True)
class Credential(Status):
    secret: str = ""

    def __repr__(self) -> str:  # le secret ne sort ni dans un log ni dans une trace
        return f"Credential({self.account_id!r}, {self.kind!r}, {self.id!r}, secret=***)"


class ConnectorStore(Protocol):
    # — par nature : le connecteur par défaut (appelants d'avant 068) —
    def put(self, account_id: str, kind: str, settings: dict[str, Any], secret: str) -> Status: ...

    def get(self, account_id: str, kind: str) -> Credential | None: ...

    def statuses(self, account_id: str) -> list[Status]: ...

    def delete(self, account_id: str, kind: str) -> bool: ...

    def mark_ok(self, account_id: str, kind: str) -> None: ...


class MultiConnectorStore(ConnectorStore, Protocol):
    """Plusieurs connecteurs par nature (068). `statuses` : par nature, le défaut d'abord, puis par date d'ajout."""

    def add(self, account_id: str, kind: str, label: str, settings: dict[str, Any], secret: str) -> Status: ...

    def by_id(self, account_id: str, connector_id: str) -> Credential | None: ...

    def update(self, account_id: str, connector_id: str, *, label: str | None = None,
               make_default: bool = False) -> Status | None: ...

    def remove(self, account_id: str, connector_id: str) -> Status | None: ...

    def mark_ok_id(self, account_id: str, connector_id: str) -> None: ...


def new_id(kind: str) -> str:
    return f"{PREFIX[kind]}_" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(10))


def check_label(label: str) -> str:
    """Libellé nettoyé ; `bad_label` s'il est vide, trop long, a la forme d'un id, ou contient / | ·."""
    out = " ".join(str(label or "").split())
    if not out or len(out) > LABEL_MAX or ID_RE.match(out.lower()) or any(c in out for c in "/|·"):
        raise ConnectorError("bad_label", f"libellé de 1 à {LABEL_MAX} caractères, sans / | · ni forme d'identifiant")
    return out


def default_label(kind: str, settings: dict[str, Any], taken: set[str]) -> str:
    base = str(settings.get("address") or "").strip() if kind == "mail" else "Agenda"
    base = base[:LABEL_MAX] or ("Mail" if kind == "mail" else "Agenda")
    label, n = base, 2
    while label.lower() in taken:
        label, n = f"{base[:LABEL_MAX - 4]} {n}", n + 1
    return label


def _check(kind: str, settings: dict[str, Any], secret: str) -> None:
    if kind not in ALL_KINDS:
        raise ConnectorError("bad_settings", f"nature inconnue : {kind}")
    if not isinstance(settings, dict) or not secret:
        raise ConnectorError("bad_settings", "réglages et secret requis")
    if any(k in settings for k in ("password", "secret", "token", "app_password")):
        raise ConnectorError("bad_settings", "un secret ne va jamais dans les réglages")
    json.dumps(settings)


def _order(rows: list[Status]) -> list[Status]:
    return sorted(rows, key=lambda s: (ALL_KINDS.index(s.kind), not s.is_default, s.created_at, s.id))


class MemoryConnectorStore:
    def __init__(self, box: SecretBox) -> None:
        self._box = box
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}  # (compte, id) → ligne
        self._lock = threading.RLock()

    @staticmethod
    def _status(r: dict[str, Any]) -> Status:
        return Status(r["account_id"], r["kind"], dict(r["settings"]), r["created_at"], r["last_ok_at"], r["id"],
                      r["label"], r["is_default"])

    def _of(self, account_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        return [r for (a, _), r in self._rows.items() if a == account_id and (kind is None or r["kind"] == kind)]

    def _default(self, account_id: str, kind: str) -> dict[str, Any] | None:
        return next((r for r in self._of(account_id, kind) if r["is_default"]), None)

    # — multi —
    def add(self, account_id: str, kind: str, label: str, settings: dict[str, Any], secret: str) -> Status:
        _check(kind, settings, secret)
        with self._lock:
            mine = self._of(account_id, kind)
            if len(mine) >= MAX_PER_KIND:
                raise ConnectorError("too_many", f"au plus {MAX_PER_KIND} connecteurs par nature")
            taken = {r["label"].lower() for r in mine}
            label = check_label(label) if label else default_label(kind, settings, taken)
            if label.lower() in taken:
                raise ConnectorError("label_taken", "ce libellé est déjà pris")
            r = {"account_id": account_id, "kind": kind, "id": new_id(kind), "label": label, "is_default": not mine,
                 "settings": json.loads(json.dumps(settings)), "sealed": self._box.seal(secret),
                 "created_at": datetime.now(UTC), "last_ok_at": None}
            self._rows[(account_id, r["id"])] = r
            return self._status(r)

    def by_id(self, account_id: str, connector_id: str) -> Credential | None:
        r = self._rows.get((account_id, connector_id))
        if r is None:
            return None
        return Credential(**{**self._status(r).__dict__, "secret": self._box.open(r["sealed"])})

    def update(self, account_id: str, connector_id: str, *, label: str | None = None,
               make_default: bool = False) -> Status | None:
        with self._lock:
            r = self._rows.get((account_id, connector_id))
            if r is None:
                return None
            if label is not None:
                label = check_label(label)
                if any(o["label"].lower() == label.lower() and o is not r for o in self._of(account_id, r["kind"])):
                    raise ConnectorError("label_taken", "ce libellé est déjà pris")
                r["label"] = label
            if make_default:
                for o in self._of(account_id, r["kind"]):
                    o["is_default"] = o is r
            return self._status(r)

    def remove(self, account_id: str, connector_id: str) -> Status | None:
        with self._lock:
            r = self._rows.pop((account_id, connector_id), None)
            if r is None:
                return None
            rest = sorted(self._of(account_id, r["kind"]), key=lambda o: (o["created_at"], o["id"]))
            if r["is_default"] and rest:
                rest[0]["is_default"] = True
            return self._status(r)

    def mark_ok_id(self, account_id: str, connector_id: str) -> None:
        r = self._rows.get((account_id, connector_id))
        if r is not None:
            r["last_ok_at"] = datetime.now(UTC)

    # — par nature (le défaut) —
    def put(self, account_id: str, kind: str, settings: dict[str, Any], secret: str) -> Status:
        _check(kind, settings, secret)
        with self._lock:
            d = self._default(account_id, kind)
            if d is not None:
                d.update(settings=json.loads(json.dumps(settings)), sealed=self._box.seal(secret),
                         created_at=datetime.now(UTC), last_ok_at=None)
                return self._status(d)
            return self.add(account_id, kind, "", settings, secret)

    def get(self, account_id: str, kind: str) -> Credential | None:
        d = self._default(account_id, kind)
        return self.by_id(account_id, d["id"]) if d else None

    def statuses(self, account_id: str) -> list[Status]:
        return _order([self._status(r) for r in self._of(account_id)])

    def delete(self, account_id: str, kind: str) -> bool:
        d = self._default(account_id, kind)
        return d is not None and self.remove(account_id, d["id"]) is not None

    def mark_ok(self, account_id: str, kind: str) -> None:
        d = self._default(account_id, kind)
        if d is not None:
            self.mark_ok_id(account_id, d["id"])


class PgConnectorStore:
    """Table `connector_credentials` (005, 068). `connect` rend une connexion psycopg (dict_row) : en bêta, celle du
    noyau qui pose `app.tenant_id` (RLS) ; par défaut aussi (`nokker/store/conn.py::connect`)."""

    COLS = "account_id, kind, id, label, is_default, settings, created_at, last_ok_at"

    def __init__(self, dsn: str, box: SecretBox, connect: Callable[[], Any] | None = None) -> None:
        self.dsn, self._box, self._connect = dsn, box, connect

    def _conn(self):
        if self._connect:
            return self._connect()
        from psycopg.rows import dict_row

        from nokker.store.conn import connect

        return connect(self.dsn, row_factory=dict_row)  # table sous RLS : locataire courant (docs/TENANCY.md §4)

    @staticmethod
    def _status(r: dict[str, Any]) -> Status:
        return Status(r["account_id"], r["kind"], r["settings"], r["created_at"], r["last_ok_at"], r["id"], r["label"],
                      r["is_default"])

    # — multi —
    def add(self, account_id: str, kind: str, label: str, settings: dict[str, Any], secret: str) -> Status:
        import psycopg
        from psycopg.types.json import Jsonb

        _check(kind, settings, secret)
        if label:
            label = check_label(label)
        try:
            with self._conn() as conn, conn.transaction():
                # verrou par (compte, nature) : deux ajouts simultanés ne font ni deux défauts ni un 11e connecteur
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"connectors:{account_id}:{kind}",))
                rows = conn.execute("SELECT label FROM connector_credentials WHERE account_id = %s AND kind = %s",
                                    (account_id, kind)).fetchall()
                if len(rows) >= MAX_PER_KIND:
                    raise ConnectorError("too_many", f"au plus {MAX_PER_KIND} connecteurs par nature")
                taken = {r["label"].lower() for r in rows}
                label = label or default_label(kind, settings, taken)
                if label.lower() in taken:
                    raise ConnectorError("label_taken", "ce libellé est déjà pris")
                r = conn.execute(
                    f"INSERT INTO connector_credentials (account_id, kind, id, label, is_default, settings, secret_enc) "
                    f"VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING {self.COLS}",
                    (account_id, kind, new_id(kind), label, not rows, Jsonb(settings), self._box.seal(secret))).fetchone()
        except psycopg.errors.UniqueViolation:
            raise ConnectorError("label_taken", "ce libellé est déjà pris") from None
        return self._status(r)

    def by_id(self, account_id: str, connector_id: str) -> Credential | None:
        with self._conn() as conn:
            r = conn.execute(f"SELECT {self.COLS}, secret_enc FROM connector_credentials WHERE account_id = %s AND id = %s",
                             (account_id, connector_id)).fetchone()
        if r is None:
            return None
        return Credential(**{**self._status(r).__dict__, "secret": self._box.open(r["secret_enc"])})

    def update(self, account_id: str, connector_id: str, *, label: str | None = None,
               make_default: bool = False) -> Status | None:
        import psycopg

        if label is not None:
            label = check_label(label)
        try:
            with self._conn() as conn, conn.transaction():
                r = conn.execute("SELECT kind FROM connector_credentials WHERE account_id = %s AND id = %s FOR UPDATE",
                                 (account_id, connector_id)).fetchone()
                if r is None:
                    return None
                if label is not None:
                    conn.execute("UPDATE connector_credentials SET label = %s WHERE account_id = %s AND id = %s",
                                 (label, account_id, connector_id))
                if make_default:
                    conn.execute("UPDATE connector_credentials SET is_default = false WHERE account_id = %s AND kind = %s "
                                 "AND is_default AND id <> %s", (account_id, r["kind"], connector_id))
                    conn.execute("UPDATE connector_credentials SET is_default = true WHERE account_id = %s AND id = %s",
                                 (account_id, connector_id))
                out = conn.execute(f"SELECT {self.COLS} FROM connector_credentials WHERE account_id = %s AND id = %s",
                                   (account_id, connector_id)).fetchone()
        except psycopg.errors.UniqueViolation:
            raise ConnectorError("label_taken", "ce libellé est déjà pris") from None
        return self._status(out)

    def remove(self, account_id: str, connector_id: str) -> Status | None:
        with self._conn() as conn, conn.transaction():
            r = conn.execute(f"DELETE FROM connector_credentials WHERE account_id = %s AND id = %s RETURNING {self.COLS}",
                             (account_id, connector_id)).fetchone()
            if r is None:
                return None
            if r["is_default"]:
                conn.execute("UPDATE connector_credentials SET is_default = true WHERE account_id = %s AND id = "
                             "(SELECT id FROM connector_credentials WHERE account_id = %s AND kind = %s "
                             "ORDER BY created_at, id LIMIT 1)", (account_id, account_id, r["kind"]))
        return self._status(r)

    def mark_ok_id(self, account_id: str, connector_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE connector_credentials SET last_ok_at = now() WHERE account_id = %s AND id = %s",
                         (account_id, connector_id))

    # — par nature (le défaut) —
    def _default_id(self, account_id: str, kind: str) -> str | None:
        with self._conn() as conn:
            r = conn.execute("SELECT id FROM connector_credentials WHERE account_id = %s AND kind = %s AND is_default",
                             (account_id, kind)).fetchone()
        return r["id"] if r else None

    def put(self, account_id: str, kind: str, settings: dict[str, Any], secret: str) -> Status:
        from psycopg.types.json import Jsonb

        _check(kind, settings, secret)
        with self._conn() as conn:
            r = conn.execute(
                f"UPDATE connector_credentials SET settings = %s, secret_enc = %s, created_at = now(), last_ok_at = NULL "
                f"WHERE account_id = %s AND kind = %s AND is_default RETURNING {self.COLS}",
                (Jsonb(settings), self._box.seal(secret), account_id, kind)).fetchone()
        return self._status(r) if r else self.add(account_id, kind, "", settings, secret)

    def get(self, account_id: str, kind: str) -> Credential | None:
        cid = self._default_id(account_id, kind)
        return self.by_id(account_id, cid) if cid else None

    def statuses(self, account_id: str) -> list[Status]:
        with self._conn() as conn:
            rows = conn.execute(f"SELECT {self.COLS} FROM connector_credentials WHERE account_id = %s",
                                (account_id,)).fetchall()
        return _order([self._status(r) for r in rows])

    def delete(self, account_id: str, kind: str) -> bool:
        cid = self._default_id(account_id, kind)
        return cid is not None and self.remove(account_id, cid) is not None

    def mark_ok(self, account_id: str, kind: str) -> None:
        cid = self._default_id(account_id, kind)
        if cid:
            self.mark_ok_id(account_id, cid)


def is_multi(store: Any) -> bool:
    """Le magasin sait-il tenir plusieurs connecteurs par nature (068) ? Celui de Lokkal (fichier) : non."""
    return all(hasattr(store, m) for m in ("add", "by_id", "update", "remove", "mark_ok_id"))


__all__ = ["ALL_KINDS", "ID_RE", "KINDS", "LABEL_MAX", "MAX_PER_KIND", "ConnectorStore", "Credential", "MemoryConnectorStore",
           "MultiConnectorStore", "PgConnectorStore", "Status", "check_label", "default_label", "is_multi", "new_id"]
