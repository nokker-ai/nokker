"""Magasin des écrans du mur (lot V7) : en mémoire (tests, Lokkal sans base) ou Postgres (`wall_screens`, migration 083,
RLS du locataire du compte). On garde la description VALIDÉE, jamais du HTML : le noyau rend à la lecture."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass
class Screen:
    screen_id: str
    account_id: str
    tenant_id: str
    thread_id: str
    title: str
    spec: dict[str, Any]
    version: int = 1
    status: str = "draft"             # draft | published (la version publiée peut être plus ancienne que `version`)
    created_at: float = 0.0
    updated_at: float = 0.0
    published_version: int | None = None
    published_sha256: str | None = None
    published_at: float | None = None
    published_spec: dict[str, Any] | None = field(default=None, repr=False)


_COLS = ("screen_id", "account_id", "tenant_id", "thread_id", "title", "spec", "version", "status", "created_at",
         "updated_at", "published_version", "published_sha256", "published_at")


class ScreenStore(Protocol):
    def get(self, tenant_id: str, screen_id: str) -> Screen | None: ...
    def save(self, s: Screen) -> None: ...
    def publish(self, tenant_id: str, screen_id: str, version: int, sha256: str, spec: dict[str, Any],
                now: float) -> Screen | None: ...
    def published_for(self, tenant_id: str, account_id: str) -> Screen | None: ...
    def unpublish(self, tenant_id: str, screen_id: str) -> None: ...


class MemoryScreenStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], Screen] = {}
        self._lock = threading.Lock()

    def get(self, tenant_id: str, screen_id: str) -> Screen | None:
        with self._lock:
            s = self._rows.get((tenant_id, screen_id))
            return Screen(**asdict(s)) if s else None

    def save(self, s: Screen) -> None:
        with self._lock:
            old = self._rows.get((s.tenant_id, s.screen_id))
            if old is not None:  # la version publiée ne bouge pas quand on retouche le brouillon
                s.status, s.published_version, s.published_sha256 = old.status, old.published_version, old.published_sha256
                s.published_at, s.published_spec = old.published_at, old.published_spec
            self._rows[(s.tenant_id, s.screen_id)] = Screen(**asdict(s))

    def publish(self, tenant_id: str, screen_id: str, version: int, sha256: str, spec: dict[str, Any],
                now: float) -> Screen | None:
        with self._lock:
            s = self._rows.get((tenant_id, screen_id))
            if s is None or s.version != version:
                return None
            for (t, _i), other in self._rows.items():  # un seul écran publié par compte
                if t == tenant_id and other.account_id == s.account_id and other.screen_id != screen_id:
                    other.status, other.published_version, other.published_sha256 = "draft", None, None
                    other.published_at, other.published_spec = None, None
            s.status, s.published_version, s.published_sha256, s.published_at = "published", version, sha256, now
            s.published_spec = json.loads(json.dumps(spec))
            return Screen(**asdict(s))

    def unpublish(self, tenant_id: str, screen_id: str) -> None:
        with self._lock:
            s = self._rows.get((tenant_id, screen_id))
            if s is not None:
                s.status, s.published_version, s.published_sha256, s.published_at, s.published_spec = (
                    "draft", None, None, None, None)

    def published_for(self, tenant_id: str, account_id: str) -> Screen | None:
        with self._lock:
            for (t, _i), s in self._rows.items():
                if t == tenant_id and s.account_id == account_id and s.status == "published":
                    return Screen(**asdict(s))
        return None


class PgScreenStore:
    """Table `wall_screens` (083), toujours dans le locataire du compte. La version publiée garde sa description dans
    `spec` sous la clé `published` : retoucher le brouillon ne change pas ce que montre le mur."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _for(self, tenant_id: str):
        from nokker.store.conn import connect_tenant

        return connect_tenant(self.dsn, tenant_id)

    @staticmethod
    def _row(r: tuple | None) -> Screen | None:
        if not r:
            return None
        d = dict(zip(_COLS, r))
        blob = d.pop("spec") or {}
        if isinstance(blob, str):
            blob = json.loads(blob)
        return Screen(**d, spec=blob.get("draft") or {}, published_spec=blob.get("published"))

    def get(self, tenant_id: str, screen_id: str) -> Screen | None:
        with self._for(tenant_id) as conn:
            return self._row(conn.execute(f"SELECT {', '.join(_COLS)} FROM wall_screens WHERE screen_id=%s",
                                          (screen_id,)).fetchone())

    def save(self, s: Screen) -> None:
        with self._for(s.tenant_id) as conn:
            conn.execute(
                "INSERT INTO wall_screens (tenant_id, screen_id, account_id, thread_id, title, spec, version, status, "
                "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, jsonb_build_object('draft', %s::jsonb), %s, "
                "'draft', %s, %s) ON CONFLICT (tenant_id, screen_id) DO UPDATE SET title=EXCLUDED.title, "
                "spec=jsonb_set(wall_screens.spec, '{draft}', EXCLUDED.spec->'draft'), version=EXCLUDED.version, "
                "updated_at=EXCLUDED.updated_at",
                (s.tenant_id, s.screen_id, s.account_id, s.thread_id, s.title, json.dumps(s.spec), s.version,
                 s.created_at, s.updated_at))

    def publish(self, tenant_id: str, screen_id: str, version: int, sha256: str, spec: dict[str, Any],
                now: float) -> Screen | None:
        with self._for(tenant_id) as conn:
            r = conn.execute("SELECT account_id FROM wall_screens WHERE screen_id=%s AND version=%s FOR UPDATE",
                             (screen_id, version)).fetchone()
            if r is None:
                return None
            conn.execute("UPDATE wall_screens SET status='draft', published_version=NULL, published_sha256=NULL, "
                         "published_at=NULL, spec=spec - 'published' WHERE account_id=%s AND screen_id<>%s "
                         "AND status='published'", (r[0], screen_id))
            conn.execute("UPDATE wall_screens SET status='published', published_version=%s, published_sha256=%s, "
                         "published_at=%s, spec=jsonb_set(spec, '{published}', %s::jsonb) WHERE screen_id=%s",
                         (version, sha256, now, json.dumps(spec), screen_id))
        return self.get(tenant_id, screen_id)

    def unpublish(self, tenant_id: str, screen_id: str) -> None:
        with self._for(tenant_id) as conn:
            conn.execute("UPDATE wall_screens SET status='draft', published_version=NULL, published_sha256=NULL, "
                         "published_at=NULL, spec=spec - 'published' WHERE screen_id=%s", (screen_id,))

    def published_for(self, tenant_id: str, account_id: str) -> Screen | None:
        with self._for(tenant_id) as conn:
            return self._row(conn.execute(f"SELECT {', '.join(_COLS)} FROM wall_screens WHERE account_id=%s AND "
                                          "status='published' LIMIT 1", (account_id,)).fetchone())


__all__ = ["MemoryScreenStore", "PgScreenStore", "Screen", "ScreenStore"]
