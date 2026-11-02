"""Liens de conversation partageables et révocables (docs/CLIENT-API.md « Export et partage d'un fil »).

- **Jeton** : `secrets.token_urlsafe(32)` (256 bits), rendu UNE fois à la création (dans `url`), stocké seulement en
  empreinte sha256 (`thread_share_tokens`, sous RLS depuis 058 : la lecture publique, sans locataire, n'ouvre que la
  ligne de SON empreinte). Pas de sel : un jeton
  de 256 bits aléatoires ne se devine pas par dictionnaire.
- **Instantané figé** : l'export `public=True` du fil au moment du partage (`thread_shares.snapshot`, sous RLS) ; les
  tours suivants n'y entrent jamais. Les résultats d'outils secrets y sont scellés comme dans l'export.
- **Révocation immédiate** : `revoked_at` posé sur les deux tables dans la même transaction ; une lecture publique
  vérifie l'état dans les DEUX (empreinte puis instantané) — une révocation ou une suppression du fil répond 404 tout
  de suite. **Expiration** : obligatoire et bornée (`MAX_EXPIRES_S`).
- **Isolation** : la lecture publique ne prend que l'empreinte ; elle lit l'instantané par `share_id` DANS le locataire
  inscrit à côté de l'empreinte (RLS) — jamais un autre fil, jamais un autre locataire. Seul le PROPRIÉTAIRE du fil
  crée, liste et révoque (la vérification est à la frontière, `nokker/api/server.py`).
- **Débit** de la route publique : `PublicLimiter` (par IP et global, fenêtre glissante d'une minute)."""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC
from typing import Any

from nokker.tenancy.context import (
    INTERNAL_TENANT,
    current_tenant,
    current_tenant_or_none,
    tenant_scope,
)

DEFAULT_EXPIRES_S = 7 * 86400
MIN_EXPIRES_S = 60
MAX_EXPIRES_S = 90 * 86400
MAX_ACTIVE_PER_THREAD = 20
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,128}$")
PUBLIC_PER_IP = 30  # lectures par minute et par IP
PUBLIC_GLOBAL = 600  # lectures par minute, toutes IP confondues (une IP falsifiée ne contourne pas ce plafond)


class ShareError(ValueError):
    """Paramètre invalide (400)."""


class ShareNotFound(LookupError):
    """Partage inconnu dans ce locataire (404)."""


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class ShareMeta:
    share_id: str
    thread_id: str
    owner: str
    created_at: float
    expires_at: float
    include_tools: bool = False
    revoked_at: float | None = None

    def status(self, now: float) -> str:
        if self.revoked_at is not None:
            return "revoked"
        return "expired" if now >= self.expires_at else "active"


def _iso(ts: float | None) -> str | None:
    from datetime import datetime

    return datetime.fromtimestamp(ts, tz=UTC).isoformat() if ts is not None else None


def share_to_json(m: ShareMeta, now: float) -> dict[str, Any]:
    return {"share_id": m.share_id, "thread_id": m.thread_id, "created_at": _iso(m.created_at),
            "expires_at": _iso(m.expires_at), "revoked_at": _iso(m.revoked_at), "include_tools": m.include_tools,
            "status": m.status(now)}


# ─────────────────────────────── stockage ───────────────────────────────


class MemoryShares:
    """Même contrat que `PgShares`, en mémoire (tests, dev) ; le locataire courant filtre comme la RLS."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, tuple[str, ShareMeta, dict[str, Any]]] = {}  # share_id → (locataire, meta, instantané)
        self._tokens: dict[str, tuple[str, str]] = {}  # empreinte → (share_id, locataire)

    @staticmethod
    def _tenant() -> str:
        return current_tenant_or_none() or INTERNAL_TENANT

    def create(self, meta: ShareMeta, digest: str, snapshot: dict[str, Any]) -> None:
        with self._lock:
            tid = self._tenant()
            self._rows[meta.share_id] = (tid, replace(meta), snapshot)
            self._tokens[digest] = (meta.share_id, tid)

    def get(self, share_id: str) -> ShareMeta | None:
        with self._lock:
            row = self._rows.get(share_id)
            return replace(row[1]) if row is not None and row[0] == self._tenant() else None

    def list_thread(self, thread_id: str) -> list[ShareMeta]:
        with self._lock:
            tid = self._tenant()
            out = [replace(m) for t, m, _ in self._rows.values() if t == tid and m.thread_id == thread_id]
        return sorted(out, key=lambda m: m.created_at, reverse=True)

    def revoke(self, share_id: str, now: float) -> ShareMeta | None:
        with self._lock:
            row = self._rows.get(share_id)
            if row is None or row[0] != self._tenant():
                return None
            if row[1].revoked_at is None:
                row[1].revoked_at = now
            return replace(row[1])

    def revoke_thread(self, thread_id: str, now: float) -> int:
        n = 0
        with self._lock:
            tid = self._tenant()
            for t, m, _ in self._rows.values():
                if t == tid and m.thread_id == thread_id and m.revoked_at is None:
                    m.revoked_at = now
                    n += 1
        return n

    def lookup(self, digest: str, now: float) -> tuple[str, str] | None:
        """(share_id, locataire) d'une empreinte, sans locataire courant — l'état est revérifié sur l'instantané."""
        with self._lock:
            return self._tokens.get(digest)

    def snapshot(self, share_id: str) -> tuple[ShareMeta, dict[str, Any]] | None:
        with self._lock:
            row = self._rows.get(share_id)
            if row is None or row[0] != self._tenant():
                return None
            return replace(row[1]), row[2]

    def digests(self) -> list[str]:  # tests : prouver qu'aucun jeton en clair n'est gardé
        with self._lock:
            return list(self._tokens)


class PgShares:
    """Tables `thread_shares` et `thread_share_tokens` (empreintes), migration 044, toutes deux sous RLS (058) ; la
    route publique lit l'empreinte par sa clé (`connect_key`)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):
        from nokker.store.conn import connect

        return connect(self.dsn)

    def _by_key(self, digest: str):
        from nokker.store.conn import connect_key

        return connect_key(self.dsn, digest)

    @staticmethod
    def _meta(r: Any) -> ShareMeta:
        return ShareMeta(share_id=r[0], thread_id=r[1], owner=r[2], created_at=float(r[3]),
                         expires_at=float(r[4]), revoked_at=float(r[5]) if r[5] is not None else None,
                         include_tools=bool(r[6]))

    _COLS = "share_id, thread_id, owner, created_at, expires_at, revoked_at, include_tools"

    def create(self, meta: ShareMeta, digest: str, snapshot: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO thread_shares (share_id, thread_id, owner, created_at, expires_at, include_tools, "
                        "snapshot) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (meta.share_id, meta.thread_id, meta.owner, meta.created_at, meta.expires_at,
                         meta.include_tools, Jsonb(snapshot)))
            cur.execute("INSERT INTO thread_share_tokens (token_sha256, share_id, tenant_id, expires_at) "
                        "VALUES (%s, %s, %s, %s)", (digest, meta.share_id, current_tenant(), meta.expires_at))

    def get(self, share_id: str) -> ShareMeta | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._COLS} FROM thread_shares WHERE share_id = %s", (share_id,))
            r = cur.fetchone()
        return self._meta(r) if r else None

    def list_thread(self, thread_id: str) -> list[ShareMeta]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._COLS} FROM thread_shares WHERE thread_id = %s ORDER BY created_at DESC",
                        (thread_id,))
            return [self._meta(r) for r in cur.fetchall()]

    def revoke(self, share_id: str, now: float) -> ShareMeta | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE thread_shares SET revoked_at = COALESCE(revoked_at, %s) WHERE share_id = %s "
                        f"RETURNING {self._COLS}", (now, share_id))
            r = cur.fetchone()
            if r is None:  # pas dans ce locataire (RLS) : on ne touche pas à l'empreinte
                return None
            cur.execute("UPDATE thread_share_tokens SET revoked_at = COALESCE(revoked_at, %s) "
                        "WHERE share_id = %s AND tenant_id = %s", (now, share_id, current_tenant()))
        return self._meta(r)

    def revoke_thread(self, thread_id: str, now: float) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE thread_shares SET revoked_at = %s WHERE thread_id = %s AND revoked_at IS NULL "
                        "RETURNING share_id", (now, thread_id))
            ids = [r[0] for r in cur.fetchall()]
            if ids:
                cur.execute("UPDATE thread_share_tokens SET revoked_at = COALESCE(revoked_at, %s) "
                            "WHERE share_id = ANY(%s) AND tenant_id = %s", (now, ids, current_tenant()))
        return len(ids)

    def lookup(self, digest: str, now: float) -> tuple[str, str] | None:
        with self._by_key(digest) as conn, conn.cursor() as cur:
            cur.execute("SELECT share_id, tenant_id, expires_at, revoked_at FROM thread_share_tokens "
                        "WHERE token_sha256 = %s", (digest,))
            r = cur.fetchone()
        if r is None or r[3] is not None or now >= float(r[2]):
            return None
        return r[0], r[1]

    def snapshot(self, share_id: str) -> tuple[ShareMeta, dict[str, Any]] | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._COLS}, snapshot FROM thread_shares WHERE share_id = %s", (share_id,))
            r = cur.fetchone()
        return (self._meta(r), r[7]) if r else None

    def purge_expired(self, now: float, grace_s: float = 30 * 86400) -> int:
        """Empreintes expirées ou révoquées depuis plus de `grace_s` : supprimées, locataire par locataire (RLS, 058)."""
        from nokker.store.conn import connect_tenant, known_tenants

        n = 0
        for tid in known_tenants(self.dsn):
            with connect_tenant(self.dsn, tid) as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM thread_share_tokens WHERE tenant_id = %s AND (expires_at < %s OR revoked_at < %s)",
                            (tid, now - grace_s, now - grace_s))
                n += cur.rowcount
        return n


# ─────────────────────────────── débit public ───────────────────────────────


class PublicLimiter:
    def __init__(self, per_ip: int = PUBLIC_PER_IP, global_max: int = PUBLIC_GLOBAL, window_s: float = 60.0) -> None:
        self.per_ip, self.global_max, self.window_s = per_ip, global_max, window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def ok(self, ip: str, now: float | None = None) -> bool:
        t = time.monotonic() if now is None else now
        with self._lock:
            for key, cap in (("*", self.global_max), (f"ip:{ip or '?'}", self.per_ip)):
                q = self._hits[key]
                while q and t - q[0] > self.window_s:
                    q.popleft()
                if len(q) >= cap:
                    return False
            self._hits["*"].append(t)
            self._hits[f"ip:{ip or '?'}"].append(t)
            if len(self._hits) > 10000:  # IP oubliées : la mémoire reste bornée
                for k in [k for k, q in self._hits.items() if k != "*" and (not q or t - q[-1] > self.window_s)]:
                    del self._hits[k]
            return True


# ─────────────────────────────── service ───────────────────────────────


@dataclass
class ThreadShares:
    store: Any  # MemoryShares | PgShares
    base_url: str = ""  # préfixe des liens rendus (`share_base_url`) ; vide = chemin relatif
    clock: Callable[[], float] = time.time
    limiter: PublicLimiter = field(default_factory=PublicLimiter)

    def public_path(self, token: str) -> str:
        return f"/v1/public/shares/{token}.html"

    def create(self, thread_id: str, owner: str, snapshot: dict[str, Any], *, expires_in: Any = None,
               include_tools: bool = False) -> tuple[ShareMeta, str]:
        """Pose un partage ; rend (meta, jeton en clair). Le jeton n'est rendu qu'ici."""
        if expires_in is None:
            expires = DEFAULT_EXPIRES_S
        else:
            if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
                raise ShareError("expires_in : nombre de secondes")
            expires = int(expires_in)
            if not MIN_EXPIRES_S <= expires <= MAX_EXPIRES_S:
                raise ShareError(f"expires_in entre {MIN_EXPIRES_S} et {MAX_EXPIRES_S} secondes")
        now = self.clock()
        active = [m for m in self.store.list_thread(thread_id) if m.status(now) == "active"]
        if len(active) >= MAX_ACTIVE_PER_THREAD:
            raise ShareError(f"au plus {MAX_ACTIVE_PER_THREAD} partages actifs par fil : révoquez-en un")
        token = secrets.token_urlsafe(32)
        meta = ShareMeta(share_id=f"sh_{uuid.uuid4().hex[:20]}", thread_id=thread_id, owner=owner, created_at=now,
                         expires_at=now + expires, include_tools=bool(include_tools))
        self.store.create(meta, token_sha256(token), snapshot)
        return meta, token

    def url(self, token: str) -> str:
        return f"{self.base_url.rstrip('/')}{self.public_path(token)}"

    def list(self, thread_id: str) -> list[ShareMeta]:
        return self.store.list_thread(thread_id)

    def get(self, share_id: str) -> ShareMeta:
        m = self.store.get(share_id)
        if m is None:
            raise ShareNotFound(share_id)
        return m

    def revoke(self, share_id: str) -> ShareMeta:
        m = self.store.revoke(share_id, self.clock())
        if m is None:
            raise ShareNotFound(share_id)
        return m

    def revoke_thread(self, thread_id: str) -> int:
        return self.store.revoke_thread(thread_id, self.clock())

    def public(self, token: str) -> dict[str, Any] | None:
        """Instantané d'un partage actif, ou None (jeton mal formé, inconnu, révoqué, expiré) — toujours le même 404."""
        if not TOKEN_RE.match(token or ""):
            return None
        found = self.store.lookup(token_sha256(token), self.clock())
        if found is None:
            return None
        share_id, tenant = found
        with tenant_scope(tenant):
            got = self.store.snapshot(share_id)
        if got is None:
            return None
        meta, snap = got
        if meta.status(self.clock()) != "active":
            return None
        return {**snap, "share": {"created_at": _iso(meta.created_at), "expires_at": _iso(meta.expires_at)}}


__all__ = ["DEFAULT_EXPIRES_S", "MAX_ACTIVE_PER_THREAD", "MAX_EXPIRES_S", "MIN_EXPIRES_S", "MemoryShares",
           "PgShares", "PublicLimiter", "ShareError", "ShareMeta", "ShareNotFound", "ThreadShares", "share_to_json",
           "token_sha256"]
