"""Export des données d'un compte (« Mon compte », docs/CLIENT-API.md) : droit d'accès et de portabilité (conditions de
la bêta, RGPD art. 15 et 20).

`POST /v1/account/export` construit l'export tout de suite (les comptes de la bêta sont petits), le garde 24 h dans la
table `account_exports` (migration 016, sous RLS : le locataire du compte seulement) et rend son identifiant ; une
demande par 24 h et par compte. `GET /v1/account/exports/{id}` donne l'état, `…/download` le fichier JSON. Session
normale (tout appareil du compte) : un export ne change rien, il ne demande pas d'appareil fort.

Contenu : le compte (adresse, langue, dates, statut), les acceptations du règlement, l'historique des consentements,
l'usage du jour et de la semaine, et TOUTES les conversations du locataire (message, réponse, messages d'outils,
canal), sans les identifiants internes de locataire. Jamais exportés : jetons, clés, secrets de connecteurs (ils ne
sont pas des données de la personne, et un export peut fuir), journaux internes de décision.
"""
from __future__ import annotations

import gzip
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

TTL_S = 24 * 3600  # le fichier reste téléchargeable 24 h
EVERY_S = 24 * 3600  # une demande par compte et par 24 h
FORMAT = "nokker-account-export/1"


@dataclass(frozen=True)
class ExportMeta:
    id: str
    account_id: str
    created_at: float
    expires_at: float
    size: int

    def public(self, now: float) -> dict[str, Any]:
        expired = now >= self.expires_at
        return {"id": self.id, "status": "expired" if expired else "ready", "created_at": _iso(self.created_at),
                "expires_at": _iso(self.expires_at), "size": self.size,
                "download": None if expired else f"/v1/account/exports/{self.id}/download"}


class ExportLimit(Exception):
    def __init__(self, retry_at: float):
        super().__init__("export_limit")
        self.retry_at = retry_at


@dataclass
class MemoryExports:
    rows: dict[str, tuple[ExportMeta, bytes]] = field(default_factory=dict)

    def create(self, account_id: str, body: bytes, now: float) -> ExportMeta:
        meta = ExportMeta(f"ex_{uuid.uuid4().hex[:20]}", account_id, now, now + TTL_S, len(body))
        self.rows[meta.id] = (meta, body)
        return meta

    def get(self, export_id: str, account_id: str) -> tuple[ExportMeta, bytes] | None:
        row = self.rows.get(export_id)
        return row if row is not None and row[0].account_id == account_id else None

    def last_for(self, account_id: str) -> ExportMeta | None:
        mine = [m for m, _ in self.rows.values() if m.account_id == account_id]
        return max(mine, key=lambda m: m.created_at) if mine else None


class PgExports:
    """Table `account_exports` (migration 016), sous RLS : la connexion porte le locataire du compte."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):
        from nokker.store.conn import connect

        return connect(self.dsn)

    def create(self, account_id: str, body: bytes, now: float) -> ExportMeta:
        meta = ExportMeta(f"ex_{uuid.uuid4().hex[:20]}", account_id, now, now + TTL_S, len(body))
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM account_exports WHERE account_id = %s AND expires_at < %s", (account_id, now))
            cur.execute("INSERT INTO account_exports (id, account_id, created_at, expires_at, body) "
                        "VALUES (%s, %s, %s, %s, %s)", (meta.id, account_id, now, meta.expires_at, body))
        return meta

    def get(self, export_id: str, account_id: str) -> tuple[ExportMeta, bytes] | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, account_id, created_at, expires_at, body FROM account_exports "
                        "WHERE id = %s AND account_id = %s", (export_id, account_id))
            r = cur.fetchone()
        if r is None:
            return None
        body = bytes(r[4])
        return ExportMeta(r[0], r[1], float(r[2]), float(r[3]), len(body)), body

    def last_for(self, account_id: str) -> ExportMeta | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, account_id, created_at, expires_at, length(body) FROM account_exports "
                        "WHERE account_id = %s ORDER BY created_at DESC LIMIT 1", (account_id,))
            r = cur.fetchone()
        return ExportMeta(r[0], r[1], float(r[2]), float(r[3]), int(r[4])) if r else None


@dataclass
class AccountExports:
    """Construit, garde et rend les exports. Les sources sont des fonctions posées par le serveur (pas d'import du
    noyau ici) ; chacune est appelée DANS le locataire du compte (la frontière l'a fixé)."""

    store: Any  # MemoryExports | PgExports
    turns_after: Callable[[tuple[str, str, int], int], list[tuple[str, str, int, dict[str, Any]]]]  # Store.turns_after
    acceptances: Callable[[str], list[dict[str, Any]]] | None = None
    consents: Callable[[str], list[dict[str, Any]]] | None = None
    usage: Callable[[str], dict[str, Any]] | None = None
    clock: Callable[[], float] = time.time

    def request(self, account: Any, tenant_id: str) -> ExportMeta:
        now = self.clock()
        last = self.store.last_for(account.account_id)
        if last is not None and now - last.created_at < EVERY_S:
            raise ExportLimit(last.created_at + EVERY_S)
        doc = self.build(account, tenant_id, now)
        body = gzip.compress(json.dumps(doc, ensure_ascii=False, default=str).encode("utf-8"), mtime=0)
        return self.store.create(account.account_id, body, now)

    def build(self, account: Any, tenant_id: str, now: float) -> dict[str, Any]:
        prefix = f"{tenant_id}:"
        threads: dict[str, list[dict[str, Any]]] = {}
        for created, tid, idx, d in self._all_turns():
            if not tid.startswith(prefix):
                continue  # défense en profondeur : en Postgres la RLS ne rend que le locataire, pas un magasin mémoire
            name = tid[len(prefix):]
            inp = d.get("input") or {}
            threads.setdefault(name, []).append({
                "index": idx, "at": created, "channel": d.get("channel"), "message": inp.get("text"),
                "reply": d.get("reply"),
                "tool_messages": [{"name": m.get("name"), "content": m.get("content")}
                                  for m in d.get("messages") or [] if m.get("role") == "tool"]})
        return {
            "format": FORMAT, "exported_at": _iso(now),
            "account": {"id": account.account_id, "email": account.email, "lang": account.lang,
                        "created_at": _iso(account.created_at), "status": account.status},
            "terms_acceptances": _plain(self.acceptances(account.account_id)) if self.acceptances else [],
            "consents": _plain(self.consents(account.account_id)) if self.consents else [],
            "usage": self.usage(account.account_id) if self.usage else None,
            "conversations": [{"thread": t, "turns": turns} for t, turns in sorted(threads.items())],
            "not_included": ["connector credentials (secrets, never exported)", "internal decision logs"],
        }

    def get(self, export_id: str, account_id: str) -> tuple[ExportMeta, bytes] | None:
        return self.store.get(export_id, account_id)

    def _all_turns(self) -> list[tuple[str, str, int, dict[str, Any]]]:
        out: list[tuple[str, str, int, dict[str, Any]]] = []
        after: tuple[str, str, int] = ("-infinity", "", -1)
        while True:
            page = self.turns_after(after, 500)
            out.extend(page)
            if len(page) < 500:
                return out
            after = (page[-1][0], page[-1][1], page[-1][2])


def _plain(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: (_iso(v) if isinstance(v, (int, float, datetime)) and k in ("at", "accepted_at") else v)
             for k, v in r.items()} for r in rows]


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, tz=UTC)
    return value.isoformat() if isinstance(value, datetime) else str(value)


__all__ = ["AccountExports", "EVERY_S", "ExportLimit", "ExportMeta", "MemoryExports", "PgExports", "TTL_S"]
